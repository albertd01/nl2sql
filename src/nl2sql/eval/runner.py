"""Run the agent over an eval suite, one JSONL per model, resumable."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from ..agent import Agent, OpenRouterClient, get_model
from ..config import AgentConfig
from ..db import Database, build_schema_card
from .datasets import EvalItem, resolve_db_url


def is_infra_failure(rec: dict) -> bool:
    """API outages / harness crashes, as opposed to the model failing the task."""
    err = str(rec.get("error") or "")
    return rec.get("status") == "failed" and err.startswith(("HTTP ", "Giving up", "network", "harness:", "OPENROUTER"))


def load_records(path: Path) -> list[dict]:
    """Records for one model; on duplicates (resumed retries) the last attempt wins."""
    latest = {}
    for line in path.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            latest[rec["item_id"]] = rec
    return list(latest.values())


def question_text(item: EvalItem, use_evidence: bool) -> str:
    if use_evidence and item.evidence:
        return f"{item.question}\n\nHint: {item.evidence}"
    return item.question


class SuiteRunner:
    def __init__(self, run_dir: Path, model_keys: list[str], use_evidence: bool = True, concurrency: int = 8,
                 config: AgentConfig | None = None):
        self.run_dir = run_dir
        self.config = config or AgentConfig()
        self.models = [get_model(k) for k in model_keys]
        self.use_evidence = use_evidence
        self.concurrency = concurrency
        self.client = OpenRouterClient()
        self._contexts: dict[str, tuple[Database, object]] = {}
        self._ctx_lock = threading.Lock()
        self._write_lock = threading.Lock()

    def _context(self, db_key: str):
        # Build each schema card once, before workers need it (building is not free on big DBs).
        with self._ctx_lock:
            if db_key not in self._contexts:
                db = Database(resolve_db_url(db_key), timeout_s=30)
                self._contexts[db_key] = (db, build_schema_card(db))
            return self._contexts[db_key]

    def run(self, items: list[EvalItem]) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        meta_path = self.run_dir / "meta.json"
        if meta_path.exists():
            existing = json.loads(meta_path.read_text()).get("config", AgentConfig().to_dict())
            if existing != self.config.to_dict():
                raise SystemExit(f"{self.run_dir} was created with config {existing}; refusing to resume with "
                                 f"{self.config.to_dict()}. Use a new run directory.")
        else:
            meta_path.write_text(json.dumps({
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "models": [asdict(m) for m in self.models],
                "use_evidence": self.use_evidence,
                "config": self.config.to_dict(),
                "config_label": self.config.label,
                "suites": sorted({it.suite for it in items}),
                "splits": sorted({it.split for it in items}),
                "n_items": len(items),
            }, indent=2))

        for db_key in sorted({it.db for it in items}):
            print(f"schema: {db_key}", flush=True)
            self._context(db_key)

        jobs = []
        files = {}
        for model in self.models:
            path = self.run_dir / f"{model.key}.jsonl"
            done = set()
            if path.exists():
                for line in path.read_text().splitlines():
                    if line.strip():
                        rec = json.loads(line)
                        if not is_infra_failure(rec):
                            done.add(rec["item_id"])  # on resume, retry only API/harness failures
            files[model.key] = path
            jobs += [(model, it) for it in items if it.id not in done]

        total = len(jobs)
        print(f"{total} runs to do ({len(items)} items x {len(self.models)} models, minus completed)", flush=True)
        if not total:
            return

        handles = {k: open(p, "a") for k, p in files.items()}
        completed = 0
        t0 = time.monotonic()
        try:
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                futures = {pool.submit(self._run_one, model, item): (model, item) for model, item in jobs}
                for fut in as_completed(futures):
                    # pop: finished futures hold full agent results (steps + row data); don't keep
                    # all of them alive until the pool shuts down.
                    model, item = futures.pop(fut)
                    rec = fut.result()
                    with self._write_lock:
                        handles[model.key].write(json.dumps(rec, default=str) + "\n")
                        handles[model.key].flush()
                        completed += 1
                    rate = completed / max(time.monotonic() - t0, 1e-6)
                    eta = (total - completed) / rate if rate else 0
                    print(f"[{completed}/{total}] {model.key:8s} {rec['status']:12s} {rec['tool_calls']:2d} calls "
                          f"{rec['elapsed_s']:6.1f}s  eta {eta/60:5.1f}m  {item.id}", flush=True)
        finally:
            for h in handles.values():
                h.close()

    def _run_one(self, model, item: EvalItem) -> dict:
        db, schema = self._context(item.db)
        agent = Agent(db, schema, model, self.client, reference_time=item.reference_time, config=self.config)
        try:
            result = agent.run(question_text(item, self.use_evidence))
            rec = result.to_dict()
        except Exception as e:  # never lose the whole run to one item
            rec = {"question": item.question, "model": model.key, "status": "failed", "answer": None, "sql": None,
                   "steps": [], "turns": 0, "tool_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                   "cost_usd": 0.0, "providers": [], "elapsed_s": 0.0, "error": f"harness: {type(e).__name__}: {e}"}
        rec["item_id"] = item.id
        rec["suite"] = item.suite
        rec["split"] = item.split
        return rec
