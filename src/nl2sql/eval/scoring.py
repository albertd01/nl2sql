"""Score agent runs.

For each result record:
  1. Unanswerable items: correct iff the agent returned status "unanswerable".
  2. Answerable items:
     a. execution match — run gold SQL and the agent's final SQL, compare result sets
        (compare.py). Deterministic, no API calls.
     b. if (a) fails, an LLM judge compares the agent's *answer text* to the gold answer.
        This catches answers that are right but whose SQL has a different shape
        (a yes/no question answered by showing both values, a list with extra context).
        The judge also labels an error type for incorrect answers.

Metrics reported: `ex` (execution match only) and `correct` (execution match OR judge).
"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..agent.openrouter import OpenRouterClient, OpenRouterError
from ..agent.models import ModelSpec
from ..db.connect import Database
from .compare import compare_results
from .datasets import EvalItem, resolve_db_url
from .runner import load_records

GOLD_TIMEOUT_S = 180
PRED_TIMEOUT_S = 60
MAX_ROWS = 100_000
GOLD_PREVIEW_ROWS = 50
JUDGE_MODEL = ModelSpec(key="judge", openrouter_id="anthropic/claude-sonnet-5", label="Claude Sonnet 5 (judge)", quantizations=())

ERROR_TYPES = [
    "wrong_value",        # filtered on a value spelled/coded differently than stored
    "wrong_column_or_table",
    "wrong_join",
    "wrong_time_logic",   # relative dates, date ranges, "this year"
    "ties_or_limit",      # top-N cut ties or wrong ranking cutoff
    "wrong_aggregation",  # count vs count distinct, avg of wrong thing, grouping
    "misread_question",
    "false_abstention",   # said unanswerable but it was answerable
    "incomplete_answer",
    "other",
]

JUDGE_PROMPT = """You are grading a text-to-SQL agent. Decide whether the agent's final answer \
is semantically equivalent to the gold answer for the question.

Rules:
- Formatting, phrasing, units shown, and ordering do not matter.
- Numbers may differ by rounding (e.g. 24.983 vs 24.98).
- Counts of elapsed time units ("how many days/hours since …"): a whole number obtained by \
truncating or rounding the gold value is correct (gold 24.983 days → 24 or 25 are both correct).
- Yes/no questions: gold "1"/"0" (or true/false) means yes/no.
- A list answer is correct only if it contains the same items as the gold list — no \
missing items and no extra items. Extra descriptive context about each item is fine.
- If the gold result is empty, the answer must clearly say there are no matching records.
- Judge the answer only against the gold answer, not against your own knowledge.

Question: {question}
{evidence}
Gold answer: {gold}

Agent's answer: {answer}

Agent's final SQL (for context only): {sql}

If INCORRECT, choose the most likely cause from: {error_types}

Respond with only a JSON object:
{{"verdict": "CORRECT" or "INCORRECT", "error_type": "<one of the causes, or null if correct>", "reason": "<one sentence>"}}"""

_CURRENT_TIME = re.compile(r"\bcurrent_time\b", re.IGNORECASE)


def gold_sql_for_execution(item: EvalItem) -> str:
    sql = item.gold_sql or ""
    if item.reference_time:
        # EHRSQL gold queries use SQLite's current_time as the fixed benchmark "now".
        sql = _CURRENT_TIME.sub(f"'{item.reference_time}'", sql)
    return sql


class Scorer:
    def __init__(self, judge: bool = True, judge_client: OpenRouterClient | None = None):
        self.judge_enabled = judge
        self.judge_client = judge_client
        self._dbs: dict[tuple[str, int], Database] = {}
        self._lock = threading.Lock()

    def _db(self, item: EvalItem, timeout: int) -> Database:
        key = (item.db, timeout)
        with self._lock:
            if key not in self._dbs:
                self._dbs[key] = Database(resolve_db_url(item.db), timeout_s=timeout)
            return self._dbs[key]

    # -- gold -------------------------------------------------------------------

    def gold_result(self, item: EvalItem) -> dict:
        """Execute the gold query. Full rows are returned but never cached: a few BIRD gold
        results are large, and holding all of them at once exhausted memory."""
        try:
            res = self._db(item, GOLD_TIMEOUT_S).execute(gold_sql_for_execution(item), max_rows=MAX_ROWS)
            return {"columns": res.columns, "rows": res.rows, "truncated": res.truncated}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {str(getattr(e, 'orig', e)).splitlines()[0][:300]}"}

    @staticmethod
    def gold_summary(gold: dict) -> dict:
        """Small, JSON-safe digest of a gold result, kept for reports and the judge."""
        if "error" in gold:
            return {"error": gold["error"]}
        return {"columns": gold["columns"], "row_count": len(gold["rows"]), "truncated": gold["truncated"],
                "preview": [[None if v is None else str(v) for v in row] for row in gold["rows"][:GOLD_PREVIEW_ROWS]]}

    @staticmethod
    def gold_answer_text(item: EvalItem, gold: dict) -> str:
        if item.gold_answer is not None:
            return item.gold_answer
        if "error" in gold:
            return "(gold query failed)"
        rows = gold["rows"]
        if not rows:
            return "(no rows)"
        text = "; ".join(", ".join("NULL" if v is None else str(v) for v in row) for row in rows[:GOLD_PREVIEW_ROWS])
        if len(rows) > GOLD_PREVIEW_ROWS:
            text += f"; … ({len(rows)} rows total)"
        return f"columns ({', '.join(gold['columns'])}): {text}"

    # -- one record -------------------------------------------------------------

    def score(self, item: EvalItem, record: dict, gold: dict | None) -> dict:
        """`gold` is the executed gold result for answerable items (None for unanswerable)."""
        status = record.get("status")
        if not item.answerable:
            ok = status == "unanswerable"
            return {"correct": ok, "ex": None, "rule": "abstained" if ok else f"did_not_abstain:{status}",
                    "judge": None, "error_type": None if ok else "false_answer"}

        if status == "failed":
            return {"correct": False, "ex": False, "rule": "agent_failed", "judge": None, "error_type": "agent_failed"}

        if "error" in gold:
            return {"correct": None, "ex": None, "rule": "gold_error", "judge": None, "error_type": None, "detail": gold["error"]}

        ex, rule, pred_error = False, "no_sql", None
        if status == "unanswerable":
            rule = "false_abstention"
        elif record.get("sql"):
            try:
                pred = self._db(item, PRED_TIMEOUT_S).execute(record["sql"].strip().rstrip(";"), max_rows=MAX_ROWS)
                m = compare_results(gold["rows"], pred.rows)
                ex, rule = m.match, m.rule
                del pred
            except Exception as e:
                rule = "pred_sql_error"
                pred_error = str(getattr(e, "orig", e)).splitlines()[0][:300]

        out = {"correct": ex, "ex": ex, "rule": rule, "judge": None, "error_type": None}
        if pred_error:
            out["detail"] = pred_error
        if ex:
            return out
        if status == "unanswerable":
            out["error_type"] = "false_abstention"
            return out
        if self.judge_enabled and record.get("answer"):
            verdict = self.judge(item, record, gold)
            out["judge"] = verdict
            if "verdict" in verdict:
                out["correct"] = verdict["verdict"] == "CORRECT"
                out["error_type"] = None if out["correct"] else verdict.get("error_type") or "other"
            else:
                out["correct"] = None  # judge failed: excluded, not counted wrong
        return out

    def judge(self, item: EvalItem, record: dict, gold: dict) -> dict:
        prompt = JUDGE_PROMPT.format(
            question=item.question,
            evidence=f"Hint given to the agent: {item.evidence}\n" if item.evidence else "",
            gold=self.gold_answer_text(item, gold)[:6000],
            answer=(record.get("answer") or "")[:6000],
            sql=(record.get("sql") or "none")[:3000],
            error_types=", ".join(ERROR_TYPES),
        )
        cost, last = 0.0, ""
        for _attempt in range(3):
            try:
                # Generous max_tokens: the judge may spend tokens on reasoning before the JSON,
                # and a tight cap returns empty content.
                data = self.judge_client.chat(JUDGE_MODEL, [{"role": "user", "content": prompt}], tools=None, max_tokens=4000)
            except OpenRouterError as e:
                last = str(e)[:300]
                continue
            cost += float((data.get("usage") or {}).get("cost") or 0)
            text = data["choices"][0]["message"].get("content") or ""
            match = re.search(r"\{.*\}", text, re.DOTALL)
            try:
                parsed = json.loads(match.group(0)) if match else {}
            except json.JSONDecodeError:
                parsed = {}
            if parsed.get("verdict") in ("CORRECT", "INCORRECT"):
                parsed["cost_usd"] = cost
                return parsed
            last = f"unparseable judge output: {text[:200]!r}"
        return {"error": last, "cost_usd": cost}


def score_run_dir(run_dir: Path, items_by_id: dict[str, EvalItem], judge: bool = True, concurrency: int = 8,
                  rescore: bool = False) -> list[Path]:
    """Score every <model>.jsonl in run_dir into <model>.scored.jsonl.

    Works item by item: the gold query runs once, every model's record for that item is
    scored against it, and the rows are dropped. Memory stays bounded by `concurrency`
    gold results instead of the whole suite.
    """
    scorer = Scorer(judge=judge, judge_client=OpenRouterClient() if judge else None)
    raw_paths = [p for p in sorted(run_dir.glob("*.jsonl")) if not p.name.endswith(".scored.jsonl")]
    models = [p.stem for p in raw_paths]
    records = {m: {r["item_id"]: r for r in load_records(p)} for m, p in zip(models, raw_paths)}
    previous: dict[str, dict] = {m: {} for m in models}
    for m, p in zip(models, raw_paths):
        out = p.with_name(p.stem + ".scored.jsonl")
        if out.exists() and not rescore:
            for line in out.read_text().splitlines():
                r = json.loads(line)
                if r.get("score", {}).get("correct") is not None:
                    previous[m][r["item_id"]] = r

    def same_run(a: dict, b: dict) -> bool:
        return all(a.get(k) == b.get(k) for k in ("status", "answer", "sql", "elapsed_s", "error"))

    item_ids = sorted({iid for per_model in records.values() for iid in per_model})

    def work(item_id: str) -> tuple[str, dict, dict | None]:
        item = items_by_id[item_id]
        scored, todo = {}, []
        for m in models:
            rec = records[m].get(item_id)
            if rec is None:
                continue
            prev = previous[m].get(item_id)
            if prev is not None and same_run(prev, rec):  # a resumed retry replaces the old record
                scored[m] = prev
            else:
                todo.append(m)
        summary = None
        if todo:
            gold = scorer.gold_result(item) if item.answerable else None
            for m in todo:
                rec = dict(records[m][item_id])
                rec["score"] = scorer.score(item, rec, gold)
                scored[m] = rec
            summary = scorer.gold_summary(gold) if gold is not None else None
            del gold
        return item_id, scored, summary

    results: dict[str, dict[str, dict]] = {m: {} for m in models}
    gold_cache_path = run_dir / "gold_summary.json"
    gold_summaries = json.loads(gold_cache_path.read_text()) if gold_cache_path.exists() else {}
    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for item_id, scored, summary in pool.map(work, item_ids):
            for m, rec in scored.items():
                results[m][item_id] = rec
            if summary is not None:
                gold_summaries[item_id] = summary
            done += 1
            if done % 25 == 0 or done == len(item_ids):
                print(f"scored {done}/{len(item_ids)} items", flush=True)

    gold_cache_path.write_text(json.dumps(gold_summaries))
    written = []
    for m, p in zip(models, raw_paths):
        out = p.with_name(p.stem + ".scored.jsonl")
        ordered = [results[m][iid] for iid in item_ids if iid in results[m]]
        out.write_text("".join(json.dumps(r, default=str) + "\n" for r in ordered))
        written.append(out)
    return written
