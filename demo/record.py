"""Record the demo: run a few prompts through the agent and save full traces to demo/traces.json.

    uv run --env-file ../m3_repro/.env python demo/record.py

The page (demo/build_page.py) is generated from the saved traces, so the demo never depends on
a live model call.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from nl2sql.agent import Agent, OpenRouterClient, get_model
from nl2sql.config import AgentConfig
from nl2sql.db import Database, build_schema_card
from nl2sql.eval.datasets import EHRSQL_NOW, MIMIC_DB

HERE = Path(__file__).resolve().parent
MODEL = "gemma"
CONFIG = "recommended"
RESULT_ROWS = 20

PROMPTS = [
    {"id": "er_women", "title": "Matching how values are stored",
     "question": "How many female patients were admitted through the ER?"},
    {"id": "labs", "title": "Joining and ranking",
     "question": "Which 5 lab tests were performed most often, and how many times each?"},
    {"id": "afib_icu", "title": "Several tables and date math",
     "question": "What is the average ICU stay in days for patients diagnosed with atrial fibrillation?"},
    {"id": "phone", "title": "Knowing when to say no",
     "question": "What is the phone number of the doctor who treated patient 10004235?"},
]


def final_result(db: Database, sql: str | None) -> dict | None:
    if not sql:
        return None
    try:
        res = db.execute(sql.strip().rstrip(";"), max_rows=RESULT_ROWS)
    except Exception as e:
        return {"error": str(getattr(e, "orig", e)).splitlines()[0]}
    return {"columns": res.columns, "rows": [[None if v is None else v for v in row] for row in res.rows],
            "truncated": res.truncated}


def main() -> None:
    db = Database(f"sqlite:///{MIMIC_DB}")
    schema = build_schema_card(db)
    agent = Agent(db, schema, get_model(MODEL), OpenRouterClient(), reference_time=EHRSQL_NOW,
                  config=AgentConfig.from_flags(CONFIG))
    runs = []
    for p in PROMPTS:
        print(f"→ {p['question']}", flush=True)
        r = agent.run(p["question"])
        print(f"  {r.status} · {r.tool_calls} tool calls · {r.elapsed_s}s · {r.answer}", flush=True)
        steps = [
            {"kind": s.kind, "tool": s.name, "args": s.args, "ok": s.ok, "output": s.output,
             "elapsed_ms": s.elapsed_ms, "self_check": (s.data or {}).get("self_check")}
            for s in r.steps
        ]
        runs.append({**p, "status": r.status, "answer": r.answer, "sql": r.sql, "steps": steps,
                     "result": final_result(db, r.sql), "tool_calls": r.tool_calls, "turns": r.turns,
                     "elapsed_s": r.elapsed_s, "tokens": r.prompt_tokens + r.completion_tokens,
                     "cost_usd": round(r.cost_usd, 5), "providers": r.providers, "error": r.error})
    out = {
        "recorded_at": time.strftime("%Y-%m-%d %H:%M"),
        "model": get_model(MODEL).label, "model_id": get_model(MODEL).openrouter_id,
        "config": AgentConfig.from_flags(CONFIG).label, "database": "MIMIC-IV clinical database demo (SQLite)",
        "reference_time": EHRSQL_NOW, "tables": len(schema.tables), "runs": runs,
    }
    (HERE / "traces.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"saved {HERE / 'traces.json'}")


if __name__ == "__main__":
    main()
