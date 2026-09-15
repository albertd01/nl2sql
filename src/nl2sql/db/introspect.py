"""Build "schema cards": a compact, model-friendly description of any database.

Per table: columns (type, nullable, PK), foreign keys, row count, a few sample rows,
and — for low-cardinality text columns — the actual distinct values, which is what
lets a model write `gender = 'F'` instead of guessing `'female'`.

Cards are cached as JSON under .cache/, keyed by URL + a fingerprint of the table/column
structure, so reconnecting to an unchanged database is instant.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import sqlalchemy as sa

from .connect import Database

CACHE_DIR = Path(__file__).resolve().parents[3] / ".cache" / "schema"
SAMPLE_ROWS = 3
DISTINCT_SCAN_ROWS = 20_000  # rows scanned per column to find low-cardinality values
MAX_ENUM_VALUES = 25
MAX_CELL_CHARS = 60
TEXT_TYPES = ("CHAR", "TEXT", "STRING", "CLOB", "ENUM")


@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    primary_key: bool = False
    values: list[str] | None = None  # full distinct set, when small


@dataclass
class ForeignKey:
    columns: list[str]
    ref_table: str
    ref_columns: list[str]


@dataclass
class TableCard:
    name: str
    columns: list[Column]
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    row_count: int | None = None
    sample_rows: list[list] = field(default_factory=list)

    def column(self, name: str) -> Column | None:
        lname = name.lower()
        return next((c for c in self.columns if c.name.lower() == lname), None)

    def is_text(self, column: Column) -> bool:
        return any(t in column.type.upper() for t in TEXT_TYPES)

    def render(self) -> str:
        head = f"TABLE {self.name}" + (f"  ({self.row_count:,} rows)" if self.row_count is not None else "")
        lines = [head]
        for c in self.columns:
            flags = []
            if c.primary_key:
                flags.append("PK")
            if not c.nullable:
                flags.append("NOT NULL")
            line = f"  - {c.name} {c.type}" + (f" [{', '.join(flags)}]" if flags else "")
            if c.values is not None:
                line += f"  values: {json.dumps(c.values, ensure_ascii=False)}"
            lines.append(line)
        for fk in self.foreign_keys:
            lines.append(f"  FK ({', '.join(fk.columns)}) -> {fk.ref_table}({', '.join(fk.ref_columns)})")
        if self.sample_rows:
            lines.append("  sample rows (" + ", ".join(c.name for c in self.columns) + "):")
            for row in self.sample_rows:
                lines.append("    " + json.dumps(row, ensure_ascii=False, default=str))
        return "\n".join(lines)


@dataclass
class SchemaCard:
    database: str
    dialect: str
    tables: dict[str, TableCard]

    def table(self, name: str) -> TableCard | None:
        lname = name.lower()
        return next((t for t in self.tables.values() if t.name.lower() == lname), None)

    def overview(self) -> str:
        """One line per table: name, row count, column names."""
        out = []
        for t in self.tables.values():
            rc = f"{t.row_count:,} rows" if t.row_count is not None else "? rows"
            out.append(f"- {t.name} ({rc}): {', '.join(c.name for c in t.columns)}")
        return "\n".join(out)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)

    @classmethod
    def from_json(cls, text: str) -> "SchemaCard":
        raw = json.loads(text)
        tables = {}
        for name, t in raw["tables"].items():
            tables[name] = TableCard(
                name=t["name"],
                columns=[Column(**c) for c in t["columns"]],
                foreign_keys=[ForeignKey(**f) for f in t["foreign_keys"]],
                row_count=t["row_count"],
                sample_rows=t["sample_rows"],
            )
        return cls(database=raw["database"], dialect=raw["dialect"], tables=tables)


def _clip(value):
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + "…"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"
    return value


def _fingerprint(db: Database, inspector) -> str:
    parts = []
    for table in sorted(inspector.get_table_names()):
        cols = ",".join(f"{c['name']}:{c['type']}" for c in inspector.get_columns(table))
        parts.append(f"{table}({cols})")
    return hashlib.sha256((db.url + "|" + ";".join(parts)).encode()).hexdigest()[:16]


def build_schema_card(db: Database, use_cache: bool = True) -> SchemaCard:
    inspector = sa.inspect(db.engine)
    cache_file = CACHE_DIR / f"{_fingerprint(db, inspector)}.json"
    if use_cache and cache_file.exists():
        return SchemaCard.from_json(cache_file.read_text())

    tables: dict[str, TableCard] = {}
    for name in sorted(inspector.get_table_names()):
        pk_cols = set(inspector.get_pk_constraint(name).get("constrained_columns") or [])
        columns = [
            Column(name=c["name"], type=str(c["type"]), nullable=bool(c.get("nullable", True)), primary_key=c["name"] in pk_cols)
            for c in inspector.get_columns(name)
        ]
        fks = [
            ForeignKey(columns=fk["constrained_columns"], ref_table=fk["referred_table"], ref_columns=fk["referred_columns"])
            for fk in inspector.get_foreign_keys(name)
        ]
        card = TableCard(name=name, columns=columns, foreign_keys=fks)
        _profile_table(db, card)
        tables[name] = card

    schema = SchemaCard(database=db.display_name, dialect=db.dialect, tables=tables)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(schema.to_json())
    return schema


def _profile_table(db: Database, card: TableCard) -> None:
    qt = db.quote(card.name)
    try:
        card.row_count = int(db.execute(f"SELECT COUNT(*) FROM {qt}").rows[0][0])
        sample = db.execute(f"SELECT * FROM {qt}", max_rows=SAMPLE_ROWS)
        card.sample_rows = [[_clip(v) for v in row] for row in sample.rows]
    except Exception:
        return  # unreadable table (permissions, exotic types): keep structure only

    for col in card.columns:
        if not card.is_text(col) or col.primary_key:
            continue
        qc = db.quote(col.name)
        sql = (
            f"SELECT v FROM (SELECT {qc} AS v FROM {qt} LIMIT {DISTINCT_SCAN_ROWS}) s "
            f"WHERE v IS NOT NULL GROUP BY v ORDER BY COUNT(*) DESC"
        )
        try:
            res = db.execute(sql, max_rows=MAX_ENUM_VALUES)
        except Exception:
            continue
        # Only record the value set if it's complete within the scanned rows, and the
        # values are short labels rather than free text.
        if not res.truncated and res.rows and all(len(str(r[0])) <= 40 for r in res.rows):
            if card.row_count is None or card.row_count > len(res.rows):
                col.values = [str(r[0]) for r in res.rows]
