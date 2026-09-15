import asyncio
import sqlite3

import pytest

from nl2sql.agent.prompts import build_system_prompt
from nl2sql.config import AgentConfig
from nl2sql.db import Database, build_schema_card
from nl2sql.mcp_server import build_server


@pytest.fixture()
def db_url(tmp_path, monkeypatch):
    monkeypatch.setattr("nl2sql.db.introspect.CACHE_DIR", tmp_path / "cache")
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT); INSERT INTO t VALUES (1, 'Emergency Room');")
    con.close()
    return f"sqlite:///{path}"


def test_mcp_server_exposes_database_tools_without_final_answer(db_url):
    server = build_server(db_url)
    tools = asyncio.run(server.list_tools())
    names = [t.name for t in tools]
    assert names == ["list_tables", "describe_table", "search_values", "check_sql", "run_query"]


def test_mcp_tools_call_the_toolbox(db_url):
    server = build_server(db_url)
    result = asyncio.run(server.call_tool("search_values", {"table": "t", "column": "name", "term": "emergency"}))
    assert "Emergency Room" in str(result)
    rejected = asyncio.run(server.call_tool("run_query", {"sql": "DELETE FROM t"}))
    assert "read-only" in str(rejected)


def test_chat_prompt_has_no_final_answer_tool(db_url):
    schema = build_schema_card(Database(db_url), use_cache=False)
    chat = build_system_prompt(schema, "2100-01-01 00:00:00", AgentConfig.from_flags("recommended"), chat=True)
    assert "final_answer" not in chat and "```sql" in chat and "Nonexistent entity" in chat
    agent = build_system_prompt(schema, "2100-01-01 00:00:00", AgentConfig.from_flags("recommended"))
    assert "final_answer" in agent
