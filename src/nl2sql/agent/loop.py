"""The agent loop: model <-> OpenRouter <-> Toolbox, emitting step events for UIs."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from ..config import AgentConfig
from ..db.connect import Database
from ..db.introspect import SchemaCard
from ..tools.self_check import review_final_sql
from ..tools.toolbox import Toolbox, openai_tool_schemas
from .models import ModelSpec
from .openrouter import OpenRouterClient, OpenRouterError
from .prompts import build_system_prompt

MAX_TURNS = 16
MAX_TOOL_TEXT_TO_MODEL = 8000
TURN_WARNING_AT = 4          # force_final: remaining turns when the model is warned
MAX_SELF_CHECK_ROUNDS = 2    # self_check: max times a final_answer is sent back for review


@dataclass
class Step:
    kind: str                    # "tool" | "model_text" | "error"
    name: str | None = None      # tool name
    args: dict | None = None
    output: str | None = None
    ok: bool = True
    data: dict = field(default_factory=dict)
    elapsed_ms: int = 0


@dataclass
class AgentResult:
    question: str
    model: str
    status: str                  # "answered" | "unanswerable" | "failed"
    answer: str | None
    sql: str | None
    steps: list[Step]
    turns: int
    tool_calls: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    providers: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    error: str | None = None
    config: dict = field(default_factory=dict)

    @property
    def last_successful_sql(self) -> str | None:
        for step in reversed(self.steps):
            if step.name == "run_query" and step.ok:
                return step.args.get("sql")
        return None

    def to_dict(self) -> dict:
        return asdict(self)


class Agent:
    def __init__(self, db: Database, schema: SchemaCard, model: ModelSpec, client: OpenRouterClient,
                 reference_time: str | None = None, config: AgentConfig | None = None):
        self.db = db
        self.schema = schema
        self.model = model
        self.client = client
        self.config = config or AgentConfig()
        self.toolbox = Toolbox(db, schema, self.config)
        self.system_prompt = build_system_prompt(schema, reference_time, self.config, MAX_TURNS)
        self.tools = openai_tool_schemas()

    def run(self, question: str, on_step: Callable[[Step], None] | None = None) -> AgentResult:
        t0 = time.monotonic()
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": question},
        ]
        result = AgentResult(question=question, model=self.model.key, status="failed", answer=None,
                             sql=None, steps=[], turns=0, tool_calls=0, config=self.config.to_dict())

        def emit(step: Step):
            result.steps.append(step)
            if on_step:
                on_step(step)

        nudged = False
        self_check_rounds = 0
        raised_issues: dict[str, str] = {}
        for turn in range(MAX_TURNS):
            result.turns = turn + 1
            tools = self.tools
            if self.config.force_final:
                remaining = MAX_TURNS - turn
                if remaining == TURN_WARNING_AT:
                    messages.append({"role": "user", "content": (
                        f"{remaining} turns left. If you already have a query result that answers the question, "
                        "call final_answer now.")})
                elif remaining == 1:
                    messages.append({"role": "user", "content": (
                        "This is your last turn. Call final_answer now with your best answer from the results you "
                        "already have, or status 'unanswerable' if you could not find the data.")})
                    tools = [t for t in self.tools if t["function"]["name"] == "final_answer"]
            try:
                data = self.client.chat(self.model, messages, tools)
            except OpenRouterError as e:
                result.error = str(e)
                emit(Step(kind="error", output=str(e), ok=False))
                break
            self._account(result, data)

            msg = data["choices"][0]["message"]
            # Keep the assistant message intact (including reasoning fields) so models with
            # thinking modes see their own prior reasoning on the next turn.
            history_msg = {k: v for k, v in msg.items() if v is not None and k != "refusal"}
            messages.append(history_msg)
            tool_calls = msg.get("tool_calls") or []

            if msg.get("content") and tool_calls:
                emit(Step(kind="model_text", output=msg["content"]))

            if not tool_calls:
                text = (msg.get("content") or "").strip()
                if not nudged:
                    # Models sometimes answer in plain text instead of calling final_answer.
                    # Nudge once; if they do it again, accept the text as the answer.
                    nudged = True
                    if text:
                        emit(Step(kind="model_text", output=text))
                    messages.append({"role": "user", "content": "Call the final_answer tool to finish."})
                    continue
                result.status = "answered" if text else "failed"
                result.answer = text or None
                result.sql = result.last_successful_sql
                if not text:
                    result.error = "Model returned no answer."
                break

            finished = False
            for i, tc in enumerate(tool_calls):
                name = tc["function"]["name"]
                raw_args = tc["function"].get("arguments") or "{}"
                t_call = time.monotonic()
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be a JSON object")
                except (json.JSONDecodeError, ValueError) as e:
                    args = {}
                    output, ok, tool_data, final = f"Could not parse tool arguments as JSON ({e}). Raw: {str(raw_args)[:300]}", False, {}, False
                    # Providers re-parse tool-call arguments from the history on the next request
                    # and reject the whole call (HTTP 400) if they're invalid JSON. Keep the error
                    # for the model in the tool message, but replay valid arguments.
                    fixed_calls = [dict(c, function=dict(c["function"])) for c in history_msg["tool_calls"]]
                    fixed_calls[i]["function"]["arguments"] = "{}"
                    history_msg["tool_calls"] = fixed_calls
                else:
                    try:
                        tr = self.toolbox.call(name, args)
                        output, ok, tool_data, final = tr.text, tr.ok, tr.data, tr.final
                    except Exception as e:  # tool bug or DB failure must not kill the run
                        output, ok, tool_data, final = f"Tool failed: {type(e).__name__}: {e}", False, {}, False

                    if final and self._should_self_check(tool_data, turn, self_check_rounds):
                        new_issues = {k: v for k, v in review_final_sql(tool_data["sql"], self.db, self.schema, check_ties=self.config.tie_check).items()
                                      if k not in raised_issues}
                        if new_issues:
                            raised_issues.update(new_issues)
                            self_check_rounds += 1
                            final, ok = False, False
                            tool_data = dict(tool_data, self_check=list(new_issues))
                            output = ("Not recorded yet — review before finishing:\n"
                                      + "\n".join(f"- {msg}" for msg in new_issues.values())
                                      + "\nFix the query if needed, then call final_answer again "
                                        "(unchanged if your answer is still right).")

                result.tool_calls += 1
                emit(Step(kind="tool", name=name, args=args, output=output, ok=ok, data=tool_data,
                          elapsed_ms=int((time.monotonic() - t_call) * 1000)))
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": output[:MAX_TOOL_TEXT_TO_MODEL]})

                if final:
                    result.status = tool_data["status"]
                    result.answer = tool_data["answer"]
                    result.sql = tool_data["sql"]
                    finished = True
            if finished:
                break
        else:
            result.error = f"Turn limit ({MAX_TURNS}) reached without a final answer."
            emit(Step(kind="error", output=result.error, ok=False))

        result.elapsed_s = round(time.monotonic() - t0, 2)
        return result

    def _should_self_check(self, tool_data: dict, turn: int, rounds: int) -> bool:
        return (
            self.config.self_check
            and tool_data.get("status") == "answered"
            and bool(tool_data.get("sql"))
            and rounds < MAX_SELF_CHECK_ROUNDS
            and turn < MAX_TURNS - 2  # leave the model turns to act on the feedback
        )

    @staticmethod
    def _account(result: AgentResult, data: dict) -> None:
        usage = data.get("usage") or {}
        result.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        result.completion_tokens += int(usage.get("completion_tokens") or 0)
        result.cost_usd += float(usage.get("cost") or 0.0)
        provider = data.get("provider")
        if provider and provider not in result.providers:
            result.providers.append(provider)
