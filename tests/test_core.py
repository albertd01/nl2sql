import sqlite3

import pytest

from nl2sql.db import Database, build_schema_card
from nl2sql.tools import Toolbox, check_sql


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr("nl2sql.db.introspect.CACHE_DIR", tmp_path / "cache")
    path = tmp_path / "shop.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, country TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
                             total REAL, status TEXT);
        INSERT INTO customers VALUES (1, 'Ada Lovelace', 'UK'), (2, 'Grace Hopper', 'US'),
                                     (3, 'Alan Turing', 'UK'), (4, '50% Off Ltd', 'US'),
                                     (5, 'AC/DC', 'AU'), (6, 'Motörhead', 'UK');
        INSERT INTO orders VALUES (1, 1, 10.0, 'shipped'), (2, 1, 20.0, 'pending'),
                                  (3, 2, 5.5, 'shipped'), (4, 3, 7.0, 'cancelled');
        """
    )
    con.commit()
    con.close()
    return Database(f"sqlite:///{path}")


@pytest.fixture()
def toolbox(db):
    return Toolbox(db, build_schema_card(db, use_cache=False))


def test_schema_card_profiles_tables(toolbox):
    schema = toolbox.schema
    orders = schema.table("orders")
    assert orders.row_count == 4
    assert orders.column("status").values == ["shipped", "pending", "cancelled"]
    assert orders.foreign_keys[0].ref_table == "customers"
    assert schema.table("customers").column("id").primary_key


def test_connection_is_read_only(db):
    with pytest.raises(Exception, match="readonly"):
        db.execute("INSERT INTO customers VALUES (9, 'x', 'y')")


@pytest.mark.parametrize(
    "sql, fragment",
    [
        ("DELETE FROM orders", "read-only"),
        ("SELECT 1; DROP TABLE orders", "one statement"),
        ("PRAGMA table_info(orders)", "read-only"),
        ("SELECT * INTO backup FROM orders", "INTO"),
        ("SELECT * FROM invoices", "Unknown table"),
        ("SELECT o.amount FROM orders o", "amount"),
        ("SELEC * FROM orders", "Syntax"),
    ],
)
def test_check_sql_rejects(toolbox, sql, fragment):
    check = check_sql(sql, toolbox.schema)
    assert not check.ok
    assert fragment.lower() in " ".join(check.errors).lower()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT c.name, SUM(o.total) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.name",
        "WITH uk AS (SELECT id FROM customers WHERE country = 'UK') SELECT COUNT(*) FROM orders WHERE customer_id IN (SELECT id FROM uk)",
        "SELECT name FROM (SELECT name, DENSE_RANK() OVER (ORDER BY id) AS r FROM customers) t WHERE r <= 2;",
    ],
)
def test_check_sql_accepts(toolbox, sql):
    check = check_sql(sql, toolbox.schema)
    assert check.ok, check.errors


def test_run_query_returns_rows_and_rejects_writes(toolbox):
    ok = toolbox.call("run_query", {"sql": "SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY status"})
    assert ok.ok and ok.data["rows"] == [("cancelled", 1), ("pending", 1), ("shipped", 2)]

    bad = toolbox.call("run_query", {"sql": "UPDATE orders SET total = 0"})
    assert not bad.ok and "rejected" in bad.text


def test_run_query_passes_literals_verbatim(toolbox):
    r = toolbox.call("run_query", {"sql": "SELECT '12:30' AS t, '%:__%' AS p, '100%' AS pct FROM customers WHERE name LIKE '%Ho%'"})
    assert r.ok, r.text
    assert r.data["rows"] == [("12:30", "%:__%", "100%")]


def test_run_query_flags_empty_result(toolbox):
    r = toolbox.call("run_query", {"sql": "SELECT * FROM customers WHERE country = 'United Kingdom'"})
    assert r.ok and "EMPTY" in r.text


def test_search_values_substring_fuzzy_and_escaping(toolbox):
    sub = toolbox.call("search_values", {"table": "customers", "column": "name", "term": "hopper"})
    assert sub.data["method"] == "substring" and sub.data["matches"][0][0] == "Grace Hopper"

    fuzzy = toolbox.call("search_values", {"table": "customers", "column": "name", "term": "Alan Turring"})
    assert fuzzy.data["method"] == "fuzzy" and fuzzy.data["matches"][0][0] == "Alan Turing"

    punct = toolbox.call("search_values", {"table": "customers", "column": "name", "term": "acdc"})
    assert [m[0] for m in punct.data["matches"]] == ["AC/DC"]

    # '%' in the term is literal, not a wildcard
    pct = toolbox.call("search_values", {"table": "customers", "column": "name", "term": "50%"})
    assert [m[0] for m in pct.data["matches"]] == ["50% Off Ltd"]


def test_search_values_rejects_unknown_identifiers(toolbox):
    r = toolbox.call("search_values", {"table": "customers", "column": "name; DROP TABLE x", "term": "a"})
    assert not r.ok and "Unknown column" in r.text


def test_bad_tool_arguments_are_reported(toolbox):
    r = toolbox.call("describe_table", {"tbl": "orders"})
    assert not r.ok and "Bad arguments" in r.text
