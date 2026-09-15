"""The agent's tools: five database tools plus a terminating final_answer tool.

Every tool returns plain text for the model, and records structured data (rows,
SQL, check results) on the ToolResult so a UI can render it without re-parsing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from ..db.connect import Database, QueryTimeout
from ..db.introspect import SchemaCard
from ..config import AgentConfig
from .sql_check import check_sql
from .value_check import missing_values

PREVIEW_ROWS = 50        # rows shown to the model
MAX_FETCH_ROWS = 1000    # rows fetched from the database per query
MAX_RESULT_CHARS = 8000
FUZZY_SCAN_VALUES = 50_000


@dataclass
class ToolResult:
    text: str
    ok: bool = True
    data: dict = field(default_factory=dict)
    final: bool = False


TOOL_SPECS = [
    {
        "name": "list_tables",
        "description": "List all tables with row counts and column names.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "describe_table",
        "description": (
            "Show a table's columns with types, primary/foreign keys, sample rows, and the complete "
            "set of values for low-cardinality text columns."
        ),
        "parameters": {
            "type": "object",
            "properties": {"table": {"type": "string", "description": "Table name"}},
            "required": ["table"],
        },
    },
    {
        "name": "search_values",
        "description": (
            "Find actual values stored in a text column that match a search term (case-insensitive "
            "substring match, falling back to fuzzy matching). Use this before filtering on names, "
            "titles, labels or codes so the WHERE clause uses values that really exist."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "column": {"type": "string"},
                "term": {"type": "string", "description": "Text to look for"},
                "limit": {"type": "integer", "description": "Max matches to return (default 20)"},
            },
            "required": ["table", "column", "term"],
        },
    },
    {
        "name": "check_sql",
        "description": (
            "Validate a SQL query without running it: syntax for this database's dialect, read-only, "
            "and that referenced tables and columns exist."
        ),
        "parameters": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
    {
        "name": "run_query",
        "description": (
            f"Validate and execute a read-only SQL query. Returns column names, up to {PREVIEW_ROWS} rows, "
            "and the total row count."
        ),
        "parameters": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
    {
        "name": "final_answer",
        "description": (
            "Finish with the answer for the user. Call exactly once, at the end. Use status "
            "'unanswerable' if the database does not contain the data needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["answered", "unanswerable"]},
                "answer": {"type": "string", "description": "Concise natural-language answer, or why it can't be answered"},
                "sql": {"type": ["string", "null"], "description": "The exact SQL query that produced the answer; null if unanswerable"},
            },
            "required": ["status", "answer", "sql"],
        },
    },
]


def openai_tool_schemas() -> list[dict]:
    return [{"type": "function", "function": spec} for spec in TOOL_SPECS]


class Toolbox:
    def __init__(self, db: Database, schema: SchemaCard, config: AgentConfig | None = None):
        self.db = db
        self.schema = schema
        self.config = config or AgentConfig()

    def call(self, name: str, args: dict) -> ToolResult:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None or name not in {s["name"] for s in TOOL_SPECS}:
            return ToolResult(f"Unknown tool '{name}'.", ok=False)
        try:
            return handler(**args)
        except TypeError as e:
            return ToolResult(f"Bad arguments for {name}: {e}", ok=False)

    # -- tools -----------------------------------------------------------------

    def _tool_list_tables(self) -> ToolResult:
        return ToolResult(self.schema.overview())

    def _tool_describe_table(self, table: str) -> ToolResult:
        card = self.schema.table(table)
        if card is None:
            return ToolResult(f"Unknown table '{table}'. Available: {', '.join(self.schema.tables)}", ok=False)
        return ToolResult(card.render())

    def _tool_search_values(self, table: str, column: str, term: str, limit: int = 20) -> ToolResult:
        card = self.schema.table(table)
        if card is None:
            return ToolResult(f"Unknown table '{table}'.", ok=False)
        col = card.column(column)
        if col is None:
            return ToolResult(f"Unknown column '{column}' in {card.name}. Columns: {', '.join(c.name for c in card.columns)}", ok=False)
        limit = max(1, min(int(limit or 20), 100))
        # Identifiers come from the schema card (never from model text), values are bound parameters.
        qt, qc = self.db.quote(card.name), self.db.quote(col.name)

        as_text = {"postgresql": f"CAST({qc} AS TEXT)", "mysql": f"CAST({qc} AS CHAR)", "mariadb": f"CAST({qc} AS CHAR)"}.get(self.db.backend, qc)
        sql = (
            f"SELECT {qc} AS v, COUNT(*) AS n FROM {qt} "
            f"WHERE LOWER({as_text}) LIKE :pattern ESCAPE '!' "
            f"GROUP BY {qc} ORDER BY n DESC"
        )
        escaped = term.lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
        res = self.db.execute(sql, {"pattern": f"%{escaped}%"}, max_rows=limit)
        matches = [(str(v), int(n)) for v, n in res.rows]
        method = "substring"

        if not matches:
            method = "fuzzy"
            scan = self.db.execute(
                f"SELECT DISTINCT {qc} FROM {qt} WHERE {qc} IS NOT NULL", max_rows=FUZZY_SCAN_VALUES
            )
            choices = [str(r[0]) for r in scan.rows]
            # Compare on letters/digits only, so "acdc" finds "AC/DC" and "o'neil" finds "O Neil".
            hits = process.extract(term, choices, scorer=fuzz.WRatio, processor=_alnum,
                                   limit=limit, score_cutoff=75)
            matches = [(value, None) for value, _score, _idx in hits]
            scores = {value: round(score) for value, score, _idx in hits}

        if not matches:
            return ToolResult(
                f"No values in {card.name}.{col.name} match '{term}'.",
                data={"matches": [], "method": method},
            )
        if method == "fuzzy":
            lines = [f"No exact substring match. Closest values in {card.name}.{col.name} for '{term}' "
                     "(similarity 0-100; low scores may be unrelated):"]
        else:
            lines = [f"Substring matches in {card.name}.{col.name} for '{term}':"]
        for value, n in matches:
            suffix = f"  ({n} rows)" if n is not None else f"  (similarity {scores[value]})"
            lines.append(f"  {json.dumps(value, ensure_ascii=False)}{suffix}")
        if res.truncated and method == "substring":
            lines.append("  … more matches exist; refine the term.")
        return ToolResult("\n".join(lines), data={"matches": matches, "method": method})

    def _tool_check_sql(self, sql: str) -> ToolResult:
        check = check_sql(sql, self.schema)
        return ToolResult(check.render(), ok=check.ok, data={"sql": sql, "errors": check.errors, "warnings": check.warnings})

    def _tool_run_query(self, sql: str) -> ToolResult:
        check = check_sql(sql, self.schema)
        if not check.ok:
            return ToolResult("Query rejected before execution.\n" + check.render(), ok=False, data={"sql": sql, "errors": check.errors})
        try:
            res = self.db.execute(sql.strip().rstrip(";"), max_rows=MAX_FETCH_ROWS)
        except QueryTimeout as e:
            return ToolResult(f"Query timed out: {e}. Simplify it or add more selective filters.", ok=False, data={"sql": sql})
        except Exception as e:
            msg = str(getattr(e, "orig", e)).strip().splitlines()[0][:500]
            return ToolResult(f"Database error: {msg}", ok=False, data={"sql": sql})

        total = f"{len(res.rows)}+" if res.truncated else str(len(res.rows))
        text = _format_table(res.columns, res.rows[:PREVIEW_ROWS])
        header = f"{total} row(s), {res.elapsed_ms} ms."
        if len(res.rows) > PREVIEW_ROWS:
            header += f" Showing first {PREVIEW_ROWS}."
        if not res.rows:
            header += " The result is EMPTY — check filter values (search_values) before concluding there is no data."
        if check.warnings:
            header += "\n" + "\n".join(f"WARNING: {w}" for w in check.warnings)
        value_warnings = self._value_warnings(sql) if self.config.value_check else []
        if value_warnings:
            header += "\n" + "\n".join(value_warnings)
        body = header + "\n" + text
        if len(body) > MAX_RESULT_CHARS:
            body = body[:MAX_RESULT_CHARS] + "\n… (output truncated)"
        return ToolResult(
            body,
            data={"sql": sql, "columns": res.columns, "rows": res.rows, "truncated": res.truncated, "elapsed_ms": res.elapsed_ms},
        )

    def _value_warnings(self, sql: str) -> list[str]:
        warnings = []
        for m in missing_values(sql, self.schema, self.db):
            hint = self._tool_search_values(m.table, m.column, m.value, limit=5)
            closest = [json.dumps(v, ensure_ascii=False) for v, _n in hint.data.get("matches", [])]
            suggestion = f" Closest stored values: {', '.join(closest)}." if closest else " No similar stored values."
            warnings.append(
                f"WARNING: filter value {json.dumps(m.value, ensure_ascii=False)} does not occur in "
                f"{m.table}.{m.column}, so that condition matches nothing.{suggestion}"
            )
        return warnings

    def _tool_final_answer(self, status: str, answer: str, sql: str | None = None) -> ToolResult:
        if status not in ("answered", "unanswerable"):
            return ToolResult("status must be 'answered' or 'unanswerable'.", ok=False)
        if sql in ("", "null", "NONE"):
            sql = None
        return ToolResult("Answer recorded.", final=True, data={"status": status, "answer": answer, "sql": sql})


def _alnum(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch == " ").strip()


def _format_table(columns: list[str], rows: list[tuple]) -> str:
    def cell(v):
        s = "NULL" if v is None else str(v)
        return s if len(s) <= 80 else s[:80] + "…"

    lines = [" | ".join(columns)]
    lines += [" | ".join(cell(v) for v in row) for row in rows]
    return "\n".join(lines)
