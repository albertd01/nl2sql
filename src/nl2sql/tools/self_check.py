"""Review a final answer's SQL before the agent is allowed to finish.

Checks are cheap, database-grounded, and phrased as questions for the model to decide on —
they never override the answer themselves:
  - the SQL fails to execute
  - the SQL returns no rows
  - `ORDER BY ... LIMIT n` cut through a tie (row n and row n+1 share the ordering value)
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp

from ..db.connect import Database
from ..db.introspect import SchemaCard

MAX_TIE_LIMIT = 100


def review_final_sql(sql: str, db: Database, schema: SchemaCard, check_ties: bool = True) -> dict[str, str]:
    """Return {issue_kind: message}. Empty dict means no concerns."""
    issues: dict[str, str] = {}
    try:
        res = db.execute(sql.strip().rstrip(";"), max_rows=1000)
    except Exception as e:
        msg = str(getattr(e, "orig", e)).strip().splitlines()[0][:300]
        return {"sql_error": f"Your final SQL fails to execute: {msg}. Fix it, or give the query you actually ran."}

    if not res.rows:
        issues["empty"] = (
            "Your final SQL returns no rows. If no records really match, the answer must say so explicitly. "
            "Otherwise re-check the filter values (search_values), joins and date logic."
        )
        return issues

    tie = tie_at_cutoff(sql, db, schema) if check_ties else None
    if tie:
        issues["tie"] = tie
    return issues


def tie_at_cutoff(sql: str, db: Database, schema: SchemaCard) -> str | None:
    """Detect ORDER BY ... LIMIT n where rows n and n+1 have equal ordering keys."""
    try:
        tree = sqlglot.parse_one(sql.strip().rstrip(";"), read=schema.dialect)
    except Exception:
        return None
    if not isinstance(tree, exp.Select) or tree.args.get("distinct") or tree.args.get("offset"):
        return None
    limit, order = tree.args.get("limit"), tree.args.get("order")
    if limit is None or order is None:
        return None
    try:
        n = int(limit.expression.name)
    except (AttributeError, ValueError):
        return None
    if not 1 <= n <= MAX_TIE_LIMIT:
        return None

    aliases = {s.alias: s.unalias() for s in tree.expressions if isinstance(s, exp.Alias)}
    keys, ordered = [], []
    for i, o in enumerate(order.expressions):
        key = o.this
        if isinstance(key, exp.Literal) and key.is_int:
            idx = int(key.name) - 1
            if not 0 <= idx < len(tree.expressions):
                return None
            key = tree.expressions[idx].unalias()
        elif isinstance(key, exp.Column) and not key.table and key.name in aliases:
            key = aliases[key.name]
        keys.append(key.copy().as_(f"__k{i}"))
        ordered.append(exp.Ordered(this=exp.column(f"__k{i}"), desc=o.args.get("desc"), nulls_first=o.args.get("nulls_first")))

    probe = tree.copy()
    probe.set("expressions", keys)
    probe.set("order", exp.Order(expressions=ordered))
    probe.set("limit", exp.Limit(expression=exp.Literal.number(n + 1)))
    try:
        rows = db.execute(probe.sql(dialect=schema.dialect), max_rows=n + 1).rows
    except Exception:
        return None  # e.g. GROUP BY referenced a projection alias we removed
    if len(rows) == n + 1 and rows[n - 1] == rows[n]:
        return (
            f"Rows {n} and {n + 1} of your final SQL have the same ORDER BY value, so LIMIT {n} dropped "
            f"tied rows arbitrarily. If the question asks for the top {n}, decide whether every row tied "
            f"at rank {n} belongs in the answer."
        )
    return None
