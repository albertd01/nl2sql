"""Configure a running Open WebUI (from openwebui/start.sh) for the SQL agent demo.

    UV_PROJECT_ENVIRONMENT=~/.venvs/nl2sql uv run python openwebui/configure.py

Idempotent. It:
  1. points the connection at OpenRouter and lists only the demo model (not hundreds),
  2. creates/updates the "SQL Agent · MIMIC-IV" model preset: Gemma + our chat system prompt,
     native tool calling, the MCP tools enabled by default, bf16 provider pin, demo prompts,
  3. makes that preset the default model.
"""
from __future__ import annotations

import os
import sys

import httpx

from nl2sql.agent.models import get_model
from nl2sql.agent.prompts import build_system_prompt
from nl2sql.db import Database, build_schema_card
from nl2sql.eval.datasets import EHRSQL_NOW, MIMIC_DB

BASE = os.environ.get("NL2SQL_OW_URL", "http://127.0.0.1:8080")
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
PRESET_ID = "sql-agent-mimic"
TOOL_ID = "server:mcp:mimic"  # matches info.id in start.sh's TOOL_SERVER_CONNECTIONS
MODEL = get_model("gemma")

DEMO_PROMPTS = [
    ("How many female patients", "were admitted through the ER?"),
    ("Which 5 lab tests", "were performed most often, and how many times each?"),
    ("Average ICU stay", "for patients diagnosed with atrial fibrillation?"),
    ("Phone number of the doctor", "who treated patient 10004235?"),
]
DEMO_CONTENT = [
    "How many female patients were admitted through the ER?",
    "Which 5 lab tests were performed most often, and how many times each?",
    "What is the average ICU stay in days for patients diagnosed with atrial fibrillation?",
    "What is the phone number of the doctor who treated patient 10004235?",
]


def main() -> None:
    http = httpx.Client(base_url=BASE, timeout=60)
    signin = http.post("/api/v1/auths/signin", json={"email": "", "password": ""})
    signin.raise_for_status()
    http.headers["Authorization"] = f"Bearer {signin.json()['token']}"

    # 1. Only list the demo model from the OpenRouter connection
    cfg = http.get("/openai/config").json()
    configs = cfg.get("OPENAI_API_CONFIGS") or {}
    configs["0"] = {**configs.get("0", {}), "enable": True, "model_ids": [MODEL.openrouter_id]}
    http.post("/openai/config/update", json={
        "ENABLE_OPENAI_API": True,
        "OPENAI_API_BASE_URLS": [OPENROUTER_BASE],
        "OPENAI_API_KEYS": cfg["OPENAI_API_KEYS"],
        "OPENAI_API_CONFIGS": configs,
    }).raise_for_status()

    # 2. Model preset
    db = Database(f"sqlite:///{MIMIC_DB}")
    system_prompt = build_system_prompt(build_schema_card(db), EHRSQL_NOW, chat=True)
    form = {
        "id": PRESET_ID,
        "base_model_id": MODEL.openrouter_id,
        "name": "SQL Agent · MIMIC-IV",
        "meta": {
            "description": "Answers questions about the MIMIC-IV demo database by writing and running read-only SQL.",
            "toolIds": [TOOL_ID],
            # builtin_tools off: Open WebUI would otherwise add ~27 of its own tools (memory, notes,
            # web search, ...). Venice rejects Gemma requests with more than 20 tool definitions, and
            # the demo should only offer the database tools anyway.
            "capabilities": {"vision": False, "file_upload": False, "web_search": False,
                             "image_generation": False, "code_interpreter": False, "citations": False,
                             "builtin_tools": False},
            "suggestion_prompts": [{"title": list(t), "content": c} for t, c in zip(DEMO_PROMPTS, DEMO_CONTENT)],
        },
        "params": {
            "system": system_prompt,
            "function_calling": "native",
            "temperature": 0,
            # Forwarded into the OpenRouter request body: same precision pin as the evaluated agent.
            "custom_params": {"provider": MODEL.provider_routing()},
        },
        "is_active": True,
    }
    exists = http.get("/api/v1/models/model", params={"id": PRESET_ID})
    if exists.status_code == 200 and exists.json():
        http.post("/api/v1/models/model/update", json=form).raise_for_status()
        print(f"updated model preset {PRESET_ID}")
    else:
        http.post("/api/v1/models/create", json=form).raise_for_status()
        print(f"created model preset {PRESET_ID}")

    # 3. Default model
    models_cfg = http.get("/api/v1/configs/models").json()
    models_cfg["DEFAULT_MODELS"] = PRESET_ID
    models_cfg["MODEL_ORDER_LIST"] = [PRESET_ID]
    http.post("/api/v1/configs/models", json=models_cfg).raise_for_status()
    print(f"default model: {PRESET_ID} · system prompt {len(system_prompt):,} chars · tools {TOOL_ID}")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as e:
        sys.exit(f"Open WebUI not reachable or rejected a request: {e}")
