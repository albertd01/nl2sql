"""Command line entry points.

  nl2sql schema --db URL [--table NAME] [--refresh]
  nl2sql ask --db URL [--model gemma|qwen|deepseek|all] [--config flags] "question"
  nl2sql eval prepare | check-gold | run | score | report | compare
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path

from dotenv import load_dotenv

from .agent import MODELS, Agent, AgentResult, OpenRouterClient, Step, get_model
from .config import AgentConfig
from .db import Database, build_schema_card


FLAG_NAMES = ", ".join(f.name for f in fields(AgentConfig))


def _print_step(prefix: str):
    def on_step(step: Step):
        if step.kind == "tool":
            args = step.args or {}
            shown = args.get("sql") or ", ".join(f"{k}={v!r}" for k, v in args.items())
            mark = "✓" if step.ok else "✗"
            first = (step.output or "").splitlines()[0][:120] if step.output else ""
            print(f"{prefix} {mark} {step.name}: {shown[:300]}\n{prefix}     → {first}", flush=True)
        elif step.kind == "model_text":
            print(f"{prefix} 💬 {step.output[:200]}", flush=True)
        else:
            print(f"{prefix} ⚠ {step.output}", flush=True)
    return on_step


def _print_result(r: AgentResult):
    label = MODELS[r.model].label
    print(f"\n=== {label} — {r.status.upper()} ===")
    print(r.answer or f"(no answer) {r.error or ''}")
    if r.sql:
        print(f"\nSQL:\n{r.sql}")
    print(
        f"\n{r.turns} turns · {r.tool_calls} tool calls · {r.elapsed_s}s · "
        f"{r.prompt_tokens + r.completion_tokens:,} tokens · ${r.cost_usd:.4f} · provider: {', '.join(r.providers) or '?'}"
    )


def cmd_schema(args):
    db = Database(args.db)
    schema = build_schema_card(db, use_cache=not args.refresh)
    if args.table:
        card = schema.table(args.table)
        print(card.render() if card else f"Unknown table {args.table}")
    else:
        print(f"{schema.database} ({schema.dialect}), {len(schema.tables)} tables\n{schema.overview()}")


def cmd_ask(args):
    db = Database(args.db)
    schema = build_schema_card(db)
    client = OpenRouterClient()
    keys = list(MODELS) if args.model == "all" else [args.model]

    def run(key: str) -> AgentResult:
        agent = Agent(db, schema, get_model(key), client, reference_time=args.reference_time,
                      config=AgentConfig.from_flags(args.config))
        return agent.run(args.question, on_step=_print_step(f"[{key}]") if not args.quiet else None)

    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        results = list(pool.map(run, keys))

    for r in results:
        _print_result(r)
    if args.json:
        with open(args.json, "a") as f:
            for r in results:
                f.write(json.dumps(r.to_dict(), default=str) + "\n")
    return 0 if all(r.status != "failed" for r in results) else 1


def _items_by_id(suites: list[str]):
    from .eval.datasets import load_suite
    return {it.id: it for s in suites for it in load_suite(s)}


def cmd_eval_prepare(args):
    from .eval.datasets import load_suite, prepare
    for name, path in prepare(args.suite).items():
        items = load_suite(name)
        splits = Counter(it.split for it in items)
        print(f"{name}: {len(items)} items -> {path}  splits {dict(splits)}")


def cmd_eval_check_gold(args):
    """Execute every gold query and compare with the stored gold answer, if there is one."""
    from .eval.datasets import load_suite
    from .eval.scoring import Scorer
    scorer = Scorer(judge=False)
    for suite in args.suite:
        items = [it for it in load_suite(suite, split=args.split) if it.answerable]
        errors, empty, answer_mismatch = [], 0, []
        for it in items:
            g = scorer.gold_result(it)
            if "error" in g:
                errors.append((it.id, g["error"]))
                continue
            if not g["rows"]:
                empty += 1
            if it.gold_answer is not None:
                got = sorted("; ".join(str(v) for v in row) for row in g["rows"])
                want = sorted(s.strip() for s in it.gold_answer.split(";")) if it.gold_answer else []
                flat = sorted(str(v) for row in g["rows"] for v in row)
                if got != want and flat != want and not _loose_equal(flat, want):
                    answer_mismatch.append((it.id, want[:5], flat[:5]))
        print(f"{suite}: {len(items)} gold queries · {len(errors)} errors · {empty} empty results"
              + (f" · {len(answer_mismatch)} differ from stored gold answer" if suite == "mimic" else ""))
        for e in errors[:10]:
            print("  ERROR", *e)
        for m in answer_mismatch[:10]:
            print("  DIFF ", m[0], "stored:", m[1], "executed:", m[2])


def _loose_equal(a: list[str], b: list[str]) -> bool:
    from .eval.compare import normalize_value
    return sorted(map(str, map(normalize_value, a))) == sorted(map(str, map(normalize_value, b)))


def cmd_eval_run(args):
    from .eval.datasets import load_suite
    from .eval.runner import SuiteRunner
    items = [it for s in args.suite for it in load_suite(s, split=args.split, limit=args.limit)]
    keys = list(MODELS) if args.models == ["all"] else args.models
    SuiteRunner(Path(args.run_dir), keys, use_evidence=not args.no_evidence, concurrency=args.concurrency,
                config=AgentConfig.from_flags(args.config)).run(items)


def cmd_eval_score(args):
    from .eval.scoring import score_run_dir
    run_dir = Path(args.run_dir)
    meta = json.loads((run_dir / "meta.json").read_text())
    items = _items_by_id(meta["suites"])
    for path in score_run_dir(run_dir, items, judge=not args.no_judge,
                              concurrency=args.concurrency, rescore=args.rescore):
        print(f"scored -> {path}")
    if not args.no_judge:
        from .eval.labeling import label_run_dir
        print(f"error labels: LLM calls per model {label_run_dir(run_dir, items, concurrency=args.concurrency)}")


def cmd_eval_relabel(args):
    from .eval.labeling import label_run_dir
    run_dir = Path(args.run_dir)
    meta = json.loads((run_dir / "meta.json").read_text())
    calls = label_run_dir(run_dir, _items_by_id(meta["suites"]), models=args.models, relabel=args.relabel,
                          concurrency=args.concurrency)
    print(f"labeled {run_dir}: LLM label calls per model {calls}")


def cmd_eval_report(args):
    from .eval.report import build_report
    run_dir = Path(args.run_dir)
    meta = json.loads((run_dir / "meta.json").read_text())
    text = build_report(run_dir, _items_by_id(meta["suites"]))
    (run_dir / "report.md").write_text(text + "\n")
    print(text)


def cmd_prompt(args):
    from .agent.prompts import build_system_prompt
    db = Database(args.db)
    schema = build_schema_card(db)
    print(build_system_prompt(schema, args.reference_time, AgentConfig.from_flags(args.config), chat=args.chat))


def cmd_mcp(args):
    from .mcp_server import serve
    serve(args.db, host=args.host, port=args.port, name=args.name)


def cmd_eval_compare(args):
    from .eval.compare_runs import compare_runs
    run_a, run_b = Path(args.run_a), Path(args.run_b)
    suites = sorted(set(json.loads((run_a / "meta.json").read_text())["suites"])
                    | set(json.loads((run_b / "meta.json").read_text())["suites"]))
    text = compare_runs(run_a, run_b, args.model, _items_by_id(suites))
    (run_b / f"compare_vs_{run_a.name}.md").write_text(text + "\n")
    print(text)


def main(argv=None):
    load_dotenv()
    ap = argparse.ArgumentParser(prog="nl2sql")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("schema", help="Inspect a database's schema card")
    s.add_argument("--db", required=True, help="SQLAlchemy URL, e.g. sqlite:///path.db or postgresql+psycopg://user@host/db")
    s.add_argument("--table")
    s.add_argument("--refresh", action="store_true", help="Rebuild instead of using the cache")
    s.set_defaults(fn=cmd_schema)

    a = sub.add_parser("ask", help="Ask a question")
    a.add_argument("question")
    a.add_argument("--db", required=True)
    a.add_argument("--model", default="gemma", choices=[*MODELS, "all"])
    a.add_argument("--reference-time", help="Override 'now' for relative dates, e.g. '2100-12-31 23:59:00'")
    a.add_argument("--json", help="Append full results (incl. steps) to this JSONL file")
    a.add_argument("--quiet", action="store_true")
    a.add_argument("--config", default="recommended",
                   help=f"Comma-separated agent flags, or 'recommended' (default) / 'all' / 'none': {FLAG_NAMES}")
    a.set_defaults(fn=cmd_ask)

    pr = sub.add_parser("prompt", help="Print the system prompt for a database")
    pr.add_argument("--db", required=True)
    pr.add_argument("--reference-time")
    pr.add_argument("--config", default="recommended")
    pr.add_argument("--chat", action="store_true", help="Variant for chat UIs (Open WebUI + `nl2sql mcp`)")
    pr.set_defaults(fn=cmd_prompt)

    m = sub.add_parser("mcp", help="Serve the database tools over MCP (Streamable HTTP) for chat UIs")
    m.add_argument("--db", required=True)
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8765)
    m.add_argument("--name", default="nl2sql")
    m.set_defaults(fn=cmd_mcp)

    e = sub.add_parser("eval", help="Benchmark suites").add_subparsers(dest="eval_cmd", required=True)
    suites = ["mimic", "bird"]

    p = e.add_parser("prepare", help="Build normalized suites with fixed dev/test splits")
    p.add_argument("--suite", nargs="+", default=suites, choices=suites)
    p.set_defaults(fn=cmd_eval_prepare)

    p = e.add_parser("check-gold", help="Execute gold SQL and sanity-check it")
    p.add_argument("--suite", nargs="+", default=suites, choices=suites)
    p.add_argument("--split")
    p.set_defaults(fn=cmd_eval_check_gold)

    p = e.add_parser("run", help="Run models over suites (resumable)")
    p.add_argument("run_dir")
    p.add_argument("--suite", nargs="+", default=suites, choices=suites)
    p.add_argument("--split", default="dev", choices=["dev", "test", "reserve"])
    p.add_argument("--models", nargs="+", default=["all"], choices=[*MODELS, "all"])
    p.add_argument("--limit", type=int, help="First N items per suite (smoke tests)")
    p.add_argument("--no-evidence", action="store_true", help="Don't pass BIRD evidence hints")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--config", default="none", help=f"Comma-separated agent flags, 'all' or 'none': {FLAG_NAMES}")
    p.set_defaults(fn=cmd_eval_run)

    p = e.add_parser("score", help="Score a run directory")
    p.add_argument("run_dir")
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--rescore", action="store_true")
    p.add_argument("--concurrency", type=int, default=8)
    p.set_defaults(fn=cmd_eval_score)

    p = e.add_parser("compare", help="Paired comparison of two scored runs (writes compare_vs_A.md into B)")
    p.add_argument("run_a", help="Reference run, e.g. the baseline")
    p.add_argument("run_b", help="Experiment run")
    p.add_argument("--model", default="gemma", choices=list(MODELS))
    p.set_defaults(fn=cmd_eval_compare)

    p = e.add_parser("relabel", help="(Re)assign taxonomy-v2 error labels to incorrect results; verdicts unchanged")
    p.add_argument("run_dir")
    p.add_argument("--models", nargs="+", help="Only these model files (default: all)")
    p.add_argument("--relabel", action="store_true", help="Redo records that already have v2 labels")
    p.add_argument("--concurrency", type=int, default=8)
    p.set_defaults(fn=cmd_eval_relabel)

    p = e.add_parser("report", help="Write report.md for a scored run directory")
    p.add_argument("run_dir")
    p.set_defaults(fn=cmd_eval_report)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
