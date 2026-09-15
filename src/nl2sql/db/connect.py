"""Read-only database connections for any SQLAlchemy URL.

Read-only is enforced at the connection level, independent of the SQL validator:
  - SQLite:   opened via a `file:...?mode=ro` URI, so writes fail inside SQLite itself.
  - Postgres: every session starts with default_transaction_read_only=on and a
              statement_timeout. For real deployments also connect as a role that only
              has SELECT grants — a session setting can be changed by SQL, a grant can't.
  - MySQL:    session is set to READ ONLY with max_execution_time.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import make_url

# SQLAlchemy dialect name -> sqlglot dialect name
SQLGLOT_DIALECTS = {
    "sqlite": "sqlite",
    "postgresql": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "duckdb": "duckdb",
    "mssql": "tsql",
    "oracle": "oracle",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
}


class QueryTimeout(Exception):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool  # more rows existed than max_rows
    elapsed_ms: int
    extra: dict = field(default_factory=dict)


class Database:
    def __init__(self, url: str, timeout_s: float = 20.0):
        self.url = url
        self.timeout_s = timeout_s
        parsed = make_url(url)
        self.backend = parsed.get_backend_name()
        self.engine = self._make_engine(parsed)
        self.dialect = SQLGLOT_DIALECTS.get(self.backend, self.backend)

    @property
    def display_name(self) -> str:
        parsed = make_url(self.url)
        if self.backend == "sqlite":
            return Path(parsed.database or "").name or "sqlite"
        return f"{self.backend}:{parsed.database}"

    def _make_engine(self, parsed) -> sa.Engine:
        timeout_ms = int(self.timeout_s * 1000)
        if self.backend == "sqlite":
            path = Path(parsed.database or "")
            if not path.exists():
                raise FileNotFoundError(f"SQLite database not found: {path}")
            uri = f"file:{path.resolve()}?mode=ro"
            # One connection per query: SQLite connects in microseconds, and this keeps
            # concurrent agents (side-by-side model runs) from sharing a connection.
            return sa.create_engine(
                "sqlite://",
                creator=lambda: sqlite3.connect(uri, uri=True, check_same_thread=False),
                poolclass=sa.pool.NullPool,
            )
        if self.backend == "postgresql":
            engine = sa.create_engine(parsed)

            @sa.event.listens_for(engine, "connect")
            def _pg_session(dbapi_conn, _record):
                with dbapi_conn.cursor() as cur:
                    cur.execute("SET default_transaction_read_only = on")
                    cur.execute(f"SET statement_timeout = {timeout_ms}")
                dbapi_conn.commit()

            return engine
        if self.backend in ("mysql", "mariadb"):
            engine = sa.create_engine(parsed)

            @sa.event.listens_for(engine, "connect")
            def _mysql_session(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("SET SESSION TRANSACTION READ ONLY")
                cur.execute(f"SET SESSION max_execution_time = {timeout_ms}")
                cur.close()

            return engine
        # Other backends work, but without connection-level read-only enforcement;
        # the SQL validator is then the only guard.
        return sa.create_engine(parsed)

    def execute(self, sql: str, params: dict | None = None, max_rows: int = 1000) -> QueryResult:
        t0 = time.monotonic()
        with self.engine.connect() as conn:
            if self.backend == "sqlite":
                self._arm_sqlite_timeout(conn)
            try:
                if params:
                    cursor = conn.execute(sa.text(sql), params)
                else:
                    # Model/gold SQL goes to the driver verbatim: sa.text() would treat ':30' in
                    # '12:30' as a bind parameter, and no_parameters stops drivers from
                    # interpreting '%' in LIKE patterns as placeholders.
                    cursor = conn.exec_driver_sql(sql, execution_options={"no_parameters": True})
                columns = list(cursor.keys())
                rows = [tuple(r) for r in cursor.fetchmany(max_rows + 1)]
                cursor.close()
            except sa.exc.OperationalError as e:
                if "interrupted" in str(e).lower() or "statement timeout" in str(e).lower():
                    raise QueryTimeout(f"Query exceeded {self.timeout_s:.0f}s timeout") from e
                raise
            finally:
                conn.rollback()
                if self.backend == "sqlite":
                    conn.connection.dbapi_connection.set_progress_handler(None, 0)
        return QueryResult(
            columns=columns,
            rows=rows[:max_rows],
            truncated=len(rows) > max_rows,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    def _arm_sqlite_timeout(self, conn) -> None:
        deadline = time.monotonic() + self.timeout_s
        raw = conn.connection.dbapi_connection
        # Returning non-zero from the progress handler aborts the running statement.
        raw.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)

    def quote(self, identifier: str) -> str:
        return self.engine.dialect.identifier_preparer.quote(identifier)
