import sqlite3

import pytest

from nl2sql.config import AgentConfig
from nl2sql.agent.prompts import build_system_prompt
from nl2sql.db import Database, build_schema_card
from nl2sql.tools import Toolbox
from nl2sql.tools.self_check import review_final_sql, tie_at_cutoff
from nl2sql.tools.value_check import literal_filters, missing_values


@pytest.fixture()
def shop(tmp_path, monkeypatch):
    monkeypatch.setattr("nl2sql.db.introspect.CACHE_DIR", tmp_path / "cache")
    path = tmp_path / "shop.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, country TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, total REAL, status TEXT, note TEXT);
        INSERT INTO customers VALUES (1, 'Ada Lovelace', 'UK'), (2, 'Grace Hopper', 'US'), (3, 'Alan Turing', 'UK');
        INSERT INTO orders VALUES (1, 1, 10, 'shipped', 'n1'), (2, 1, 20, 'shipped', 'n2'),
                                  (3, 2, 5, 'pending', 'n3'), (4, 3, 7, 'cancelled', 'n4');
        """
    )
    con.close()
    db = Database(f"sqlite:///{path}")
    return db, build_schema_card(db, use_cache=False)


def test_literal_filters_resolve_aliases_in_lists_and_subqueries(shop):
    _, schema = shop
    sql = ("SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id "
           "WHERE c.country IN ('UK', 'FR') AND o.status <> 'Shipped' "
           "AND o.customer_id IN (SELECT id FROM customers WHERE name = 'Ada')")
    assert set(literal_filters(sql, schema)) == {
        ("customers", "country", "UK"), ("customers", "country", "FR"),
        ("orders", "status", "Shipped"), ("customers", "name", "Ada"),
    }


def test_literal_filters_ignore_numbers_like_and_ctes(shop):
    _, schema = shop
    assert literal_filters("SELECT * FROM orders WHERE total = 10 AND status LIKE 'ship%'", schema) == []
    assert literal_filters("WITH x AS (SELECT * FROM orders) SELECT * FROM x WHERE status = 'nope'", schema) == []


def test_missing_values_uses_card_values_and_database(shop):
    db, schema = shop
    # status has a card value list; name is free text (all unique) so it is checked in the database
    missing = missing_values("SELECT * FROM orders o JOIN customers c ON c.id=o.customer_id "
                             "WHERE o.status = 'SHIPPED' AND c.name = 'Grace Hopper' AND c.name <> 'Nobody'", schema, db)
    assert {(m.table, m.column, m.value) for m in missing} == {("orders", "status", "SHIPPED"), ("customers", "name", "Nobody")}


def test_run_query_warns_on_missing_values_only_when_enabled(shop):
    db, schema = shop
    sql = "SELECT COUNT(*) FROM orders WHERE status = 'Shipped'"
    plain = Toolbox(db, schema).call("run_query", {"sql": sql})
    assert "does not occur" not in plain.text
    checked = Toolbox(db, schema, AgentConfig(value_check=True)).call("run_query", {"sql": sql})
    assert '"Shipped" does not occur in orders.status' in checked.text
    assert '"shipped"' in checked.text  # closest stored value suggested


@pytest.mark.parametrize("order_by", ["n DESC", "COUNT(*) DESC", "2 DESC"])
def test_tie_at_cutoff_detected(shop, order_by):
    db, schema = shop
    # counts: shipped 2, pending 1, cancelled 1 -> LIMIT 2 cuts a tie
    sql = f"SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY {order_by} LIMIT 2"
    assert "Rows 2 and 3" in tie_at_cutoff(sql, db, schema)


def test_no_tie_reported_when_cutoff_is_clean(shop):
    db, schema = shop
    assert tie_at_cutoff("SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY n DESC LIMIT 1", db, schema) is None
    assert tie_at_cutoff("SELECT status FROM orders LIMIT 2", db, schema) is None  # no ORDER BY


def test_review_final_sql(shop):
    db, schema = shop
    assert review_final_sql("SELECT * FROM orders WHERE status = 'shipped'", db, schema) == {}
    assert "empty" in review_final_sql("SELECT * FROM orders WHERE status = 'lost'", db, schema)
    assert "sql_error" in review_final_sql("SELECT * FROM nope", db, schema)
    assert "tie" in review_final_sql("SELECT status FROM orders GROUP BY status ORDER BY COUNT(*) DESC LIMIT 2", db, schema)


def test_prompt_unchanged_when_flags_off(shop):
    _, schema = shop
    base = build_system_prompt(schema, "2100-01-01 00:00:00")
    assert build_system_prompt(schema, "2100-01-01 00:00:00", AgentConfig()) == base
    assert "8. " not in base
    tied = build_system_prompt(schema, "2100-01-01 00:00:00", AgentConfig(tie_rule=True))
    assert "8. Rankings and ties" in tied


def test_config_parsing():
    assert AgentConfig.from_flags("none") == AgentConfig()
    assert AgentConfig.from_flags("recommended") == AgentConfig(abstain_rule_v2=True, force_final=True, self_check=True)
    assert AgentConfig.from_flags("tie_rule,value_check").label == "value_check+tie_rule"
    assert all(AgentConfig.from_flags("all").to_dict().values())
    with pytest.raises(ValueError):
        AgentConfig.from_flags("tie_rules")


def test_tie_detection_is_optional_in_self_check(shop):
    db, schema = shop
    sql = "SELECT status FROM orders GROUP BY status ORDER BY COUNT(*) DESC LIMIT 2"
    assert review_final_sql(sql, db, schema, check_ties=False) == {}


def test_abstain_rule_v2_prompt(shop):
    _, schema = shop
    text = build_system_prompt(schema, "2100-01-01 00:00:00", AgentConfig(abstain_rule_v2=True))
    assert "nonexistent entity" in text and "stand-in field" in text
