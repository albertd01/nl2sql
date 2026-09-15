"""Paired comparison of two scored runs of the same model on the same questions.

Overlapping confidence intervals can hide real differences (and noise can fake them), so
this looks at the questions that *changed* between runs: fixed (A wrong -> B right) versus
broken (A right -> B wrong), with an exact McNemar test on those discordant pairs.
"""
from __future__ import annotations

import json
import statistics
from math import comb
from pathlib import Path

from .datasets import EvalItem


def mcnemar_exact(fixed: int, broken: int) -> float:
    """Two-sided exact binomial test on discordant pairs."""
    n = fixed + broken
    if n == 0:
        return 1.0
    k = min(fixed, broken)
    tail = sum(comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def _load(run_dir: Path, model: str) -> dict[str, dict]:
    path = run_dir / f"{model}.scored.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — score the run first.")
    return {r["item_id"]: r for r in map(json.loads, path.read_text().splitlines())}


def _label(run_dir: Path) -> str:
    meta = run_dir / "meta.json"
    cfg = json.loads(meta.read_text()).get("config_label", "baseline") if meta.exists() else "?"
    return f"{run_dir.name} ({cfg})"


def compare_runs(run_a: Path, run_b: Path, model: str, items_by_id: dict[str, EvalItem]) -> str:
    a, b = _load(run_a, model), _load(run_b, model)
    shared = sorted(set(a) & set(b))
    lines = [f"# {_label(run_b)} vs {_label(run_a)} — model `{model}`", "",
             f"{len(shared)} paired questions. Fixed = wrong in A, right in B. Broken = right in A, wrong in B. "
             "p = exact McNemar test on fixed vs broken (small p = unlikely to be noise).", ""]

    groups = []
    for suite in sorted({items_by_id[i].suite for i in shared}):
        suite_ids = [i for i in shared if items_by_id[i].suite == suite]
        ans = [i for i in suite_ids if items_by_id[i].answerable]
        unans = [i for i in suite_ids if not items_by_id[i].answerable]
        if ans:
            groups.append((f"{suite} answerable", ans))
        if unans:
            groups.append((f"{suite} unanswerable (refusals)", unans))

    rows, details = [], []
    for name, ids in groups:
        ids = [i for i in ids if a[i]["score"]["correct"] is not None and b[i]["score"]["correct"] is not None]
        acc_a = sum(bool(a[i]["score"]["correct"]) for i in ids)
        acc_b = sum(bool(b[i]["score"]["correct"]) for i in ids)
        fixed = [i for i in ids if not a[i]["score"]["correct"] and b[i]["score"]["correct"]]
        broken = [i for i in ids if a[i]["score"]["correct"] and not b[i]["score"]["correct"]]
        n = len(ids)
        rows.append(f"| {name} | {n} | {100*acc_a/n:.1f}% | {100*acc_b/n:.1f}% | {100*(acc_b-acc_a)/n:+.1f} pts "
                    f"| {len(fixed)} | {len(broken)} | {mcnemar_exact(len(fixed), len(broken)):.3f} |")
        for title, subset, before, after in (("Fixed", fixed, a, b), ("Broken", broken, a, b)):
            if not subset:
                continue
            details += [f"### {name}: {title} ({len(subset)})", ""]
            for i in subset:
                item = items_by_id[i]
                was = before[i]["score"].get("error_type") or before[i]["score"]["rule"]
                now = after[i]["score"].get("error_type") or after[i]["score"]["rule"]
                details.append(f"- `{i}` {item.question[:110]}{'…' if len(item.question) > 110 else ''}  "
                               f"— A: {was if title == 'Fixed' else 'correct'} → B: {'correct' if title == 'Fixed' else now}")
            details.append("")

    lines += ["| Group | n | A | B | Δ | Fixed | Broken | p |", "|---|---|---|---|---|---|---|---|", *rows, ""]

    def stat(runs, key, fn=statistics.median):
        vals = [runs[i][key] for i in shared if runs[i]["status"] != "failed"]
        return fn(vals) if vals else 0

    lines += [
        "| Efficiency | A | B |", "|---|---|---|",
        f"| Median tool calls | {stat(a, 'tool_calls'):.0f} | {stat(b, 'tool_calls'):.0f} |",
        f"| Median time | {stat(a, 'elapsed_s'):.1f}s | {stat(b, 'elapsed_s'):.1f}s |",
        f"| Agent failures | {sum(a[i]['status'] == 'failed' for i in shared)} | {sum(b[i]['status'] == 'failed' for i in shared)} |",
        f"| Total cost | ${sum(a[i]['cost_usd'] for i in shared):.2f} | ${sum(b[i]['cost_usd'] for i in shared):.2f} |",
        "",
    ]
    return "\n".join(lines + details)
