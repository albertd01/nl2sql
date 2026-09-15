import copy
import json
import sqlite3

import pytest

from nl2sql.agent import Agent, get_model
from nl2sql.agent.loop import MAX_TURNS
from nl2sql.config import AgentConfig
from nl2sql.db import Database, build_schema_card


class FakeClient:
    """Replays scripted assistant messages and records what the agent sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.tools_sent = []

    def chat(self, model, messages, tools, max_tokens=8000):
        self.sent.append(copy.deepcopy(messages))
        self.tools_sent.append([t["function"]["name"] for t in tools or []])
        return {"choices": [{"message": self.replies.pop(0)}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def tool_call(call_id, name, arguments):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}]}


@pytest.fixture()
def agent_parts(tmp_path, monkeypatch):
    monkeypatch.setattr("nl2sql.db.introspect.CACHE_DIR", tmp_path / "cache")
    path = tmp_path / "t.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT); INSERT INTO t VALUES (1, 'a');")
    con.close()
    db = Database(f"sqlite:///{path}")
    return db, build_schema_card(db, use_cache=False)


def test_malformed_tool_arguments_are_not_replayed(agent_parts):
    db, schema = agent_parts
    broken = '{"answer": "one row", "sql": SELECT * FROM t}'  # unquoted SQL, as Gemma produced
    client = FakeClient([
        tool_call("c1", "final_answer", broken),
        tool_call("c2", "final_answer", json.dumps({"status": "answered", "answer": "one row", "sql": "SELECT * FROM t"})),
    ])
    result = Agent(db, schema, get_model("gemma"), client).run("how many rows?")

    assert result.status == "answered" and result.sql == "SELECT * FROM t"
    second_request = client.sent[1]
    replayed = second_request[-2]["tool_calls"][0]["function"]["arguments"]
    assert replayed == "{}"
    json.loads(replayed)
    assert "Could not parse tool arguments" in second_request[-1]["content"]
    assert result.steps[0].ok is False


def test_plain_text_answer_is_nudged_then_accepted(agent_parts):
    db, schema = agent_parts
    client = FakeClient([
        {"role": "assistant", "content": "There is one row."},
        {"role": "assistant", "content": "There is one row."},
    ])
    result = Agent(db, schema, get_model("gemma"), client).run("how many rows?")
    assert result.status == "answered" and result.answer == "There is one row."
    assert client.sent[1][-1] == {"role": "user", "content": "Call the final_answer tool to finish."}


def run_query(call_id, sql):
    return tool_call(call_id, "run_query", json.dumps({"sql": sql}))


def final(call_id, sql, answer="answer", status="answered"):
    return tool_call(call_id, "final_answer", json.dumps({"status": status, "answer": answer, "sql": sql}))


def test_force_final_restricts_last_turn(agent_parts):
    db, schema = agent_parts
    replies = [run_query(f"q{i}", "SELECT * FROM t") for i in range(MAX_TURNS - 1)]
    replies.append(final("f", "SELECT * FROM t", "one row"))
    client = FakeClient(replies)
    result = Agent(db, schema, get_model("gemma"), client, config=AgentConfig(force_final=True)).run("rows?")

    assert result.status == "answered"
    assert client.tools_sent[-1] == ["final_answer"]
    assert len(client.tools_sent[-2]) > 1
    assert "last turn" in client.sent[-1][-1]["content"]
    assert any("turns left" in (m.get("content") or "") for m in client.sent[-1] if m["role"] == "user")


def test_without_force_final_turn_limit_fails(agent_parts):
    db, schema = agent_parts
    client = FakeClient([run_query(f"q{i}", "SELECT * FROM t") for i in range(MAX_TURNS)])
    result = Agent(db, schema, get_model("gemma"), client).run("rows?")
    assert result.status == "failed" and "Turn limit" in result.error
    assert all(len(tools) > 1 for tools in client.tools_sent)


def test_self_check_sends_empty_result_back_once(agent_parts):
    db, schema = agent_parts
    empty_sql = "SELECT * FROM t WHERE name = 'zzz'"
    client = FakeClient([final("f1", empty_sql, "none"), final("f2", empty_sql, "No rows match.")])
    result = Agent(db, schema, get_model("gemma"), client, config=AgentConfig(self_check=True)).run("rows named zzz?")

    assert result.status == "answered" and result.answer == "No rows match."
    first = result.steps[0]
    assert first.name == "final_answer" and not first.ok and first.data["self_check"] == ["empty"]
    assert "returns no rows" in client.sent[1][-1]["content"]
    assert result.steps[1].ok  # same issue is not raised twice


def test_self_check_off_accepts_immediately(agent_parts):
    db, schema = agent_parts
    client = FakeClient([final("f1", "SELECT * FROM t WHERE name = 'zzz'", "none")])
    result = Agent(db, schema, get_model("gemma"), client).run("q")
    assert result.status == "answered" and result.config == AgentConfig().to_dict()
