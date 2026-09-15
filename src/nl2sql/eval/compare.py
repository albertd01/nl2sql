"""Result-set comparison for execution accuracy.

Follows BIRD's convention of comparing result *sets* (row order and duplicates ignored),
with two relaxations that matter for an agent writing its own queries:

  - value normalization: numbers compared to 4 significant figures (models ROUND()
    differently), strings case-folded and trimmed, Decimal/bool/int/float unified.
  - column projection: the model may return extra columns (e.g. name *and* id) as long
    as some choice of its columns reproduces the gold result exactly.

Returns a MatchResult naming which rule matched, so reports can show how lenient each
match was.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from decimal import Decimal

MAX_PROJECTIONS = 20_000


@dataclass
class MatchResult:
    match: bool
    rule: str  # "exact" | "projection" | "none" | "gold_empty" | reason for no comparison


def normalize_value(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, Decimal)):
        v = float(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if v == 0:
            return 0.0
        return float(f"{v:.4g}")
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v)
    s = str(v).strip()
    # Numeric text (common in SQLite) compares equal to the number
    try:
        return normalize_value(float(s)) if s and s.lstrip("-+").replace(".", "", 1).isdigit() else s.casefold()
    except ValueError:
        return s.casefold()


def normalize_rows(rows) -> set[tuple]:
    return {tuple(normalize_value(v) for v in row) for row in rows}


def compare_results(gold_rows, pred_rows) -> MatchResult:
    gold = normalize_rows(gold_rows)
    pred = normalize_rows(pred_rows)
    if gold == pred:
        return MatchResult(True, "exact" if gold else "both_empty")
    if not gold or not pred:
        return MatchResult(False, "none")

    g_width = len(next(iter(gold)))
    p_width = len(next(iter(pred)))
    if p_width <= g_width:
        return MatchResult(False, "none")

    # Try projecting the prediction's columns onto the gold's shape.
    for n, perm in enumerate(itertools.permutations(range(p_width), g_width)):
        if n >= MAX_PROJECTIONS:
            break
        if {tuple(row[i] for i in perm) for row in pred} == gold:
            return MatchResult(True, "projection")
    return MatchResult(False, "none")
