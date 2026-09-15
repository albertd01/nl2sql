"""End-to-end check of the running demo, without a browser.

Sends one question through Open WebUI exactly like the chat UI does (saved chat + streaming task,
so Open WebUI runs the MCP tool loop), prints the tool calls and the answer, then deletes the chat.

    uv run --no-project --with httpx python openwebui/check.py
    uv run --no-project --with httpx python openwebui/check.py "Which 5 lab tests were performed most often?"

Exit code 0 when the model called at least one database tool and produced an answer.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

import httpx

BASE = os.environ.get("NL2SQL_OW_URL", "http://127.0.0.1:8080")
MODEL = "sql-agent-mimic"
TOOL_ID = "server:mcp:mimic"
DEFAULT_QUESTION = "How many female patients were admitted through the ER?"
TIMEOUT_S = 240


def main() -> int:
    question = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUESTION
    http = httpx.Client(base_url=BASE, timeout=60)
    try:
        token = http.post("/api/v1/auths/signin", json={"email": "", "password": ""}).json()["token"]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        print(f"FAIL: Open WebUI not reachable at {BASE} ({e}). Run openwebui/start.sh first.")
        return 1
    http.headers["Authorization"] = f"Bearer {token}"

    models = [m["id"] for m in http.get("/api/models").json().get("data", [])]
    if MODEL not in models:
        print(f"FAIL: model preset '{MODEL}' missing. Run openwebui/start.sh (it runs configure.py).")
        return 1

    user_id, answer_id, now = str(uuid.uuid4()), str(uuid.uuid4()), int(time.time())
    user_msg = {"id": user_id, "parentId": None, "childrenIds": [answer_id], "role": "user",
                "content": question, "timestamp": now, "models": [MODEL]}
    answer_msg = {"id": answer_id, "parentId": user_id, "childrenIds": [], "role": "assistant",
                  "content": "", "model": MODEL, "timestamp": now}
    chat_id = http.post("/api/v1/chats/new", json={"chat": {
        "title": "nl2sql check", "models": [MODEL], "messages": [user_msg, answer_msg],
        "history": {"messages": {user_id: user_msg, answer_id: answer_msg}, "currentId": answer_id},
    }}).json()["id"]

    print(f"Question: {question}")
    try:
        started = time.time()
        http.post("/api/chat/completions", json={
            "model": MODEL, "stream": True, "chat_id": chat_id, "id": answer_id, "session_id": "nl2sql-check",
            "messages": [{"role": "user", "content": question}], "tool_ids": [TOOL_ID],
            "background_tasks": {"title_generation": False, "tags_generation": False, "follow_up_generation": False},
        }).raise_for_status()
        while time.time() - started < TIMEOUT_S:
            if not http.get(f"/api/tasks/chat/{chat_id}").json().get("task_ids"):
                break
            time.sleep(2)
        else:
            print(f"FAIL: no answer within {TIMEOUT_S}s")
            return 1

        msg = http.get(f"/api/v1/chats/{chat_id}").json()["chat"]["history"]["messages"][answer_id]
        calls = [item for item in msg.get("output") or [] if item.get("type") == "function_call"]
        for item in calls:
            print(f"  tool call: {item['name']}({item.get('arguments', '')[:160]})")
        if msg.get("error"):
            print(f"FAIL: {json.dumps(msg['error'])[:500]}")
            print("See README 'Troubleshooting' for provider errors.")
            return 1
        content = (msg.get("content") or "").strip()
        print(f"Answer ({time.time() - started:.0f}s):\n{content}")
        if not calls or not content:
            print("FAIL: expected at least one database tool call and a non-empty answer")
            return 1
        print("OK: the demo works end to end")
        return 0
    finally:
        http.delete(f"/api/v1/chats/{chat_id}")


if __name__ == "__main__":
    sys.exit(main())
