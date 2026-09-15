"""Summarize scored runs into markdown tables (printed and written to report.md)."""
from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from ..agent.models import MODELS
from .datasets import EvalItem


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (100 * max(0.0, (centre - margin) / denom), 100 * min(1.0, (centre + margin) / denom))


def pct(k: int, n: int) -> str:
    if n == 0:
        return "—"
    lo, hi = wilson(k, n)
    return f"{100 * k / n:.1f}% [{lo:.0f}–{hi:.0f}] ({k}/{n})"


def _table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out)


def build_report(run_dir: Path, items_by_id: dict[str, EvalItem]) -> str:
    records_by_model: dict[str, list[dict]] = {}
    for path in sorted(run_dir.glob("*.scored.jsonl")):
        key = path.name.removesuffix(".scored.jsonl")
        records_by_model[key] = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    if not records_by_model:
        return "No scored results found. Run `nl2sql eval score` first."

    meta = json.loads((run_dir / "meta.json").read_text()) if (run_dir / "meta.json").exists() else {}
    lines = [f"# Eval report: {run_dir.name}", ""]
    if meta:
        lines.append(f"Created {meta.get('created')} · splits {meta.get('splits')} · evidence hints: {meta.get('use_evidence')} "
                     f"· agent config: **{meta.get('config_label', 'baseline')}**")
        lines.append("")
    lines.append("Accuracy cells: value [Wilson 95% CI] (correct/n). `EX` = result sets match; "
                 "`Correct` = EX or LLM judge accepts the answer text. Items whose gold query or judge "
                 "call failed are excluded from n and listed separately.")
    lines.append("")

    suites = sorted({items_by_id[r["item_id"]].suite for recs in records_by_model.values() for r in recs})
    for suite in suites:
        lines += [f"## {suite}", ""]
        overview_rows, unans_rows, cost_rows = [], [], []
        breakdown = defaultdict(dict)  # label -> model -> cell
        error_counts = {}

        for model, recs in records_by_model.items():
            recs = [r for r in recs if items_by_id[r["item_id"]].suite == suite]
            ans = [r for r in recs if items_by_id[r["item_id"]].answerable]
            unans = [r for r in recs if not items_by_id[r["item_id"]].answerable]
            scored_ans = [r for r in ans if r["score"]["correct"] is not None]
            excluded = len(ans) - len(scored_ans)

            ex = sum(1 for r in scored_ans if r["score"]["ex"])
            correct = sum(1 for r in scored_ans if r["score"]["correct"])
            judged_in = sum(1 for r in scored_ans if r["score"]["correct"] and not r["score"]["ex"])
            false_abst = sum(1 for r in scored_ans if r["score"]["rule"] == "false_abstention")
            failed = sum(1 for r in recs if r["status"] == "failed")
            label = MODELS[model].label if model in MODELS else model
            overview_rows.append([label, pct(ex, len(scored_ans)), pct(correct, len(scored_ans)), judged_in,
                                  false_abst, failed, excluded or ""])

            if unans:
                abst = sum(1 for r in unans if r["score"]["correct"])
                unans_rows.append([label, pct(abst, len(unans))])

            calls = [r["tool_calls"] for r in recs if r["status"] != "failed"]
            secs = [r["elapsed_s"] for r in recs if r["status"] != "failed"]
            cost = sum(r["cost_usd"] for r in recs)
            providers = Counter(p for r in recs for p in r.get("providers", []))
            cost_rows.append([
                label,
                f"{statistics.median(calls):.0f}" if calls else "—",
                f"{statistics.median(secs):.1f}s" if secs else "—",
                f"{max(secs):.0f}s" if secs else "—",
                f"${cost / max(len(recs), 1):.4f}",
                f"${cost:.2f}",
                ", ".join(f"{p} ({n})" for p, n in providers.most_common()),
            ])

            # breakdowns: BIRD difficulty, MIMIC tie questions
            for r in scored_ans:
                item = items_by_id[r["item_id"]]
                if "difficulty" in item.tags:
                    breakdown[f"difficulty: {item.tags['difficulty']}"].setdefault(model, []).append(r["score"]["correct"])
                if "tie" in item.tags:
                    tag = "tie (top-N with ties)" if item.tags["tie"] else "no tie"
                    breakdown[tag].setdefault(model, []).append(r["score"]["correct"])
            v2 = any((r["score"].get("label") or {}).get("taxonomy") for r in recs)
            column = label if v2 else f"{label} (v1 labels)"
            error_counts[column] = Counter(r["score"]["error_type"] for r in recs if r["score"].get("error_type"))

        lines += ["### Answerable questions", "",
                  _table(["Model", "EX", "Correct", "Judge-rescued", "False abstentions", "Agent failures", "Excluded"], overview_rows), ""]
        if unans_rows:
            lines += ["### Unanswerable questions (correct = abstained)", "", _table(["Model", "Abstention rate"], unans_rows), ""]
        if breakdown:
            models = list(records_by_model)
            rows = []
            for tag in sorted(breakdown):
                row = [tag]
                for m in models:
                    vals = breakdown[tag].get(m, [])
                    row.append(pct(sum(vals), len(vals)))
                rows.append(row)
            lines += ["### Breakdown (Correct)", "", _table(["Subset"] + [MODELS[m].label if m in MODELS else m for m in models], rows), ""]
        lines += ["### Efficiency", "", _table(["Model", "Median tool calls", "Median time", "Max time", "Cost/question", "Total cost", "Providers (requests)"], cost_rows), ""]

        all_types = sorted({t for c in error_counts.values() for t in c})
        if all_types:
            rows = [[t] + [error_counts[label].get(t, 0) or "" for label in error_counts] for t in all_types]
            lines += ["### Error types", "",
                      "Taxonomy v2 labels (see `eval/labeling.py`). Columns marked *v1 labels* use the older, "
                      "unreliable judge labels — run `nl2sql eval relabel` for them.", "",
                      _table(["Error type"] + list(error_counts), rows), ""]

        gold_errors = sorted({r["item_id"] for recs in records_by_model.values() for r in recs
                              if items_by_id[r["item_id"]].suite == suite and r["score"]["rule"] == "gold_error"})
        if gold_errors:
            lines += [f"Gold query failed (excluded): {', '.join(gold_errors)}", ""]

    judge_cost = sum((r["score"].get("judge") or {}).get("cost_usd", 0) for recs in records_by_model.values() for r in recs)
    label_cost = sum((r["score"].get("label") or {}).get("cost_usd", 0) for recs in records_by_model.values() for r in recs)
    lines.append(f"Judge cost: ${judge_cost:.2f} · error-label cost: ${label_cost:.2f}")
    return "\n".join(lines)
