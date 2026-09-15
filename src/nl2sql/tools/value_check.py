"""Find text literals in filter conditions that don't occur in the column they're compared to.

`WHERE gender = 'F'` against a column storing 'f' silently returns nothing — the most common
error class in the baseline. This resolves each `column = 'literal'` / `column IN (...)` /
`column <> 'literal'` to its base table (through aliases, via sqlglot scopes) and checks the
literal against the schema card's known values or, failing that, the database.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from ..db.connect import Database
from ..db.introspect import SchemaCard

MAX_CHECKS = 8


@dataclass
class MissingValue:
    table: str
    column: str
    value: str


def literal_filters(sql: str, schema: SchemaCard) -> list[tuple[str, str, str]]:
    """(table, column, literal) for text-literal comparisons on base-table text columns."""
    try:
        tree = sqlglot.parse_one(sql, read=schema.dialect)
    except Exception:
        return []
    mapping = {t.name: {c.name: c.type for c in t.columns} for t in schema.tables.values()}
    try:
        tree = qualify(tree, schema=mapping, dialect=schema.dialect, validate_qualify_columns=False,
                       quote_identifiers=False)
    except Exception:
        pass  # unqualified columns simply won't resolve

    found: list[tuple[str, str, str]] = []
    try:
        scopes = list(traverse_scope(tree))
    except Exception:
        return []
    for scope in scopes:
        for col in scope.columns:
            parent = col.parent
            literals: list[exp.Expression] = []
            if isinstance(parent, (exp.EQ, exp.NEQ)):
                other = parent.expression if parent.this is col else parent.this
                literals = [other]
            elif isinstance(parent, exp.In) and parent.this is col:
                literals = list(parent.expressions)
            strings = [lit.name for lit in literals if isinstance(lit, exp.Literal) and lit.is_string]
            if not strings:
                continue
            source = scope.sources.get(col.table)
            if not isinstance(source, exp.Table):
                continue  # CTE / subquery column: can't map to stored values reliably
            card = schema.table(source.name)
            column = card.column(col.name) if card else None
            if column is None or not card.is_text(column):
                continue
            for s in strings:
                key = (card.name, column.name, s)
                if key not in found:
                    found.append(key)
    return found


def missing_values(sql: str, schema: SchemaCard, db: Database) -> list[MissingValue]:
    missing = []
    for table, column, value in literal_filters(sql, schema)[:MAX_CHECKS]:
        col = schema.table(table).column(column)
        if col.values is not None:
            exists = value in col.values
        else:
            try:
                res = db.execute(
                    f"SELECT 1 FROM {db.quote(table)} WHERE {db.quote(column)} = :v LIMIT 1", {"v": value}, max_rows=1
                )
                exists = bool(res.rows)
            except Exception:
                continue
        if not exists:
            missing.append(MissingValue(table, column, value))
    return missing
