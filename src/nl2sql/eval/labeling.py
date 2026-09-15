"""Error-type labels for incorrect results (taxonomy v2).

Separate from the correctness verdict on purpose: verdicts come from the unchanged judge
prompt in scoring.py, so accuracy stays comparable across runs, while labels can be
re-derived at any time (`nl2sql eval relabel`).

v1 gave the judge bare label names; it read "wrong_value" as "any wrong number", which made
the Milestone-2 error table misleading. v2 gives every label a definition and shows the
labeler the gold SQL, which the verdict judge deliberately never sees.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..agent.openrouter import OpenRouterClient, OpenRouterError
from .datasets import EvalItem
from .scoring import JUDGE_MODEL, gold_sql_for_execution

TAXONOMY_VERSION = "v2"

ANSWER_ERRORS = {
    "wrong_filter_value": "A WHERE/JOIN literal or code does not match how values are stored (spelling, casing, "
                          "code format), or a value stated in the hint was not used.",
    "wrong_column_or_table": "Used a different column or table than the question needs, including substituting a "
                             "related field.",
    "wrong_join": "Missing, extra or wrong join: wrong key, or a join that duplicates or drops rows.",
    "wrong_time_logic": "Relative dates, date ranges, date arithmetic or date-format handling differ from the gold query.",
    "wrong_aggregation": "Wrong aggregate or grouping: COUNT vs COUNT(DISTINCT), wrong GROUP BY level, aggregating "
                         "the wrong set of rows.",
    "wrong_formula": "Arithmetic differs: sign or direction of a difference, ratio or percentage base, unit "
                     "conversion, or rounding that changes the result.",
    "ties_or_ordering": "Ranking differs: rows tied at a cutoff included/excluded differently, wrong sort direction, "
                        "or wrong LIMIT.",
    "misread_question": "Answered a different question: wrong entity, wrong condition, or a misunderstanding of what "
                        "is asked (e.g. 'second' read as 'second to last').",
    "incomplete_answer": "The query result is right but the answer text reports only part of it (a sample of rows, "
                         "missing requested items or columns).",
    "gold_questionable": "The agent's answer is defensible; the gold answer looks wrong, ambiguous, or contradicts "
                         "the hint.",
    "other": "None of the above.",
}

REFUSAL_ERRORS = {
    "substituted_related_field": "Answered using a different, loosely related column or table as a stand-in for "
                                 "data the database does not have.",
    "nonexistent_entity_answered": "An entity named in the question (ID, name, code) does not exist in the "
                                   "database, and the agent answered (e.g. 'no records') instead of abstaining.",
    "advice_or_action_answered": "The question asks for a recommendation, prediction or action, and the agent "
                                 "answered it.",
    "answered_beyond_data": "The database has related data but not what is asked; the agent answered as if it did, "
                            "without an identifiable substituted field.",
    "question_seems_answerable": "The question looks answerable from this schema and the agent's answer looks "
                                 "grounded in the data (possible benchmark noise).",
    "other": "None of the above.",
}

# Assigned without an LLM call.
DETERMINISTIC = {"no_usable_answer", "false_abstention"}

ANSWER_PROMPT = """You are diagnosing why a text-to-SQL agent's answer was graded INCORRECT. \
Compare the agent's SQL and answer with the gold SQL and gold answer, and pick the single \
most important cause.

Causes:
{taxonomy}

Question: {question}
{evidence}
Gold SQL: {gold_sql}
Gold answer: {gold}

Agent's SQL: {sql}
Agent's answer: {answer}
Grader's reason for INCORRECT: {grader_reason}

Respond with only a JSON object: {{"error_type": "<one cause name>", "reason": "<one sentence>"}}"""

REFUSAL_PROMPT = """This question is labeled UNANSWERABLE for its database (the data needed is not \
recorded), but a text-to-SQL agent answered it instead of abstaining. Pick the single best \
description of what went wrong.

Causes:
{taxonomy}

Question: {question}

Agent's SQL: {sql}
Agent's answer: {answer}

Respond with only a JSON object: {{"error_type": "<one cause name>", "reason": "<one sentence>"}}"""


def _taxonomy_text(taxonomy: dict[str, str]) -> str:
    return "\n".join(f"- {name}: {desc}" for name, desc in taxonomy.items())


def _gold_text(item: EvalItem, gold_summaries: dict) -> str:
    if item.gold_answer is not None:
        return item.gold_answer
    g = gold_summaries.get(item.id)
    if not g or "error" in g:
        return "(unavailable)"
    rows = "; ".join(", ".join("NULL" if v is None else str(v) for v in row) for row in g["preview"])
    more = f"; … ({g['row_count']} rows total)" if g["row_count"] > len(g["preview"]) else ""
    return f"columns ({', '.join(g['columns'])}): {rows or '(no rows)'}{more}"


def needs_label(item: EvalItem, rec: dict) -> bool:
    return rec["score"].get("correct") is False


def deterministic_label(item: EvalItem, rec: dict) -> str | None:
    if rec["status"] == "failed":
        return "no_usable_answer"
    if item.answerable and rec["score"].get("rule") == "false_abstention":
        return "false_abstention"
    return None


def llm_label(client: OpenRouterClient, item: EvalItem, rec: dict, gold_summaries: dict) -> dict:
    if item.answerable:
        taxonomy = ANSWER_ERRORS
        prompt = ANSWER_PROMPT.format(
            taxonomy=_taxonomy_text(taxonomy), question=item.question,
            evidence=f"Hint given to the agent: {item.evidence}\n" if item.evidence else "",
            gold_sql=gold_sql_for_execution(item)[:3000], gold=_gold_text(item, gold_summaries)[:4000],
            sql=(rec.get("sql") or "none")[:3000], answer=(rec.get("answer") or "")[:4000],
            grader_reason=((rec["score"].get("judge") or {}).get("reason") or rec["score"].get("rule") or "")[:500],
        )
    else:
        taxonomy = REFUSAL_ERRORS
        prompt = REFUSAL_PROMPT.format(
            taxonomy=_taxonomy_text(taxonomy), question=item.question,
            sql=(rec.get("sql") or "none")[:3000], answer=(rec.get("answer") or "")[:4000],
        )

    cost, last = 0.0, ""
    for _ in range(3):
        try:
            data = client.chat(JUDGE_MODEL, [{"role": "user", "content": prompt}], tools=None, max_tokens=4000)
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
        if parsed.get("error_type") in taxonomy:
            return {"type": parsed["error_type"], "reason": parsed.get("reason"), "cost_usd": cost}
        last = f"unparseable or unknown label: {text[:200]!r}"
    return {"type": "other", "reason": f"labeling failed: {last}", "cost_usd": cost, "failed": True}


def label_run_dir(run_dir: Path, items_by_id: dict[str, EvalItem], models: list[str] | None = None,
                  relabel: bool = False, concurrency: int = 8) -> dict[str, int]:
    """Write taxonomy-v2 labels into <model>.scored.jsonl. Returns number of LLM label calls per model."""
    gold_path = run_dir / "gold_summary.json"
    gold_summaries = json.loads(gold_path.read_text()) if gold_path.exists() else {}
    client = OpenRouterClient()
    calls = {}

    for path in sorted(run_dir.glob("*.scored.jsonl")):
        model = path.name.removesuffix(".scored.jsonl")
        if models and model not in models:
            continue
        records = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

        def work(rec: dict) -> tuple[dict, bool]:
            score = rec["score"]
            item = items_by_id[rec["item_id"]]
            if not needs_label(item, rec):
                return rec, False
            if not relabel and (score.get("label") or {}).get("taxonomy") == TAXONOMY_VERSION:
                return rec, False
            if "error_type_v1" not in score:
                score["error_type_v1"] = score.get("error_type")
            fixed = deterministic_label(item, rec)
            label = {"type": fixed, "reason": None, "cost_usd": 0.0} if fixed else llm_label(client, item, rec, gold_summaries)
            label["taxonomy"] = TAXONOMY_VERSION
            score["label"] = label
            score["error_type"] = label["type"]
            return rec, fixed is None

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(work, records))
        path.write_text("".join(json.dumps(r, default=str) + "\n" for r, _ in results))
        calls[model] = sum(1 for _, used_llm in results if used_llm)
    return calls
