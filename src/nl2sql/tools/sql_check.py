"""Static SQL validation with sqlglot: read-only, single statement, known tables/columns.

This is the policy layer; the connection's read-only mode (db/connect.py) is the
enforcement layer underneath it. Both must pass for a query to run.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from ..db.introspect import SchemaCard

# Functions with side effects or filesystem/network access on common backends.
BLOCKED_FUNCTIONS = {
    "pg_sleep", "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "set_config",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "lo_import", "lo_export", "dblink",
    "load_extension", "readfile", "writefile", "sleep", "benchmark", "load_file",
}


@dataclass
class SqlCheck:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)

    def render(self) -> str:
        if self.ok and not self.warnings:
            return f"OK. Tables referenced: {', '.join(self.tables) or 'none'}."
        parts = ["OK." if self.ok else "INVALID."]
        parts += [f"ERROR: {e}" for e in self.errors]
        parts += [f"WARNING: {w}" for w in self.warnings]
        return "\n".join(parts)


def check_sql(sql: str, schema: SchemaCard) -> SqlCheck:
    dialect = schema.dialect
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        return SqlCheck(ok=False, errors=["Empty query."])

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except ParseError as e:
        return SqlCheck(ok=False, errors=[f"Syntax error ({dialect}): {_first_line(e)}"])

    if len(statements) != 1:
        return SqlCheck(ok=False, errors=["Exactly one statement is allowed per query."])
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        kind = type(tree).__name__.upper()
        return SqlCheck(ok=False, errors=[f"Only read-only SELECT queries are allowed (got {kind})."])

    errors: list[str] = []
    if tree.find(exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.Command):
        errors.append("Query contains a data-modifying or DDL clause.")
    if any(s.args.get("into") for s in tree.find_all(exp.Select)):
        errors.append("SELECT ... INTO creates a table and is not allowed.")
    for fn in tree.find_all(exp.Anonymous, exp.Func):
        name = (fn.name if isinstance(fn, exp.Anonymous) else fn.sql_name()).lower()
        if name in BLOCKED_FUNCTIONS:
            errors.append(f"Function {name}() is not allowed.")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    referenced = []
    for table in tree.find_all(exp.Table):
        name = table.name
        if not name or name.lower() in cte_names:
            continue
        if schema.table(name) is None:
            errors.append(f"Unknown table '{name}'. Use list_tables to see available tables.")
        elif name not in referenced:
            referenced.append(name)

    warnings: list[str] = []
    if not errors:
        mapping = {
            t.name: {c.name: c.type for c in t.columns} for t in schema.tables.values()
        }
        try:
            qualify(tree.copy(), schema=mapping, dialect=dialect, validate_qualify_columns=True, quote_identifiers=False)
        except OptimizeError as e:
            msg = _first_line(e)
            if "could not be resolved" in msg or "Unknown column" in msg:
                errors.append(f"{msg}. Use describe_table to check column names.")
            else:
                warnings.append(f"Could not fully validate columns: {msg}")
        except Exception as e:  # sqlglot doesn't model every dialect construct
            warnings.append(f"Could not fully validate columns: {_first_line(e)}")

    return SqlCheck(ok=not errors, errors=list(dict.fromkeys(errors)), warnings=warnings, tables=referenced)


def _first_line(e: Exception) -> str:
    return str(e).strip().splitlines()[0][:300]
