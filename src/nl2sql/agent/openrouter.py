"""Minimal OpenRouter chat-completions client with retries."""
from __future__ import annotations

import os
import time

import httpx

from .models import ModelSpec

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class OpenRouterError(Exception):
    pass


class OpenRouterClient:
    def __init__(self, api_key: str | None = None, timeout_s: float = 180.0):
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise OpenRouterError("OPENROUTER_API_KEY is not set.")
        self.http = httpx.Client(
            timeout=timeout_s,
            headers={
                "Authorization": f"Bearer {key}",
                "X-Title": "nl2sql demo agent",
            },
        )

    def chat(self, model: ModelSpec, messages: list[dict], tools: list[dict] | None, max_tokens: int = 8000) -> dict:
        payload = {
            "model": model.openrouter_id,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "usage": {"include": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if model.quantizations:
            payload["provider"] = model.provider_routing()
        if model.reasoning_effort:
            payload["reasoning"] = {"effort": model.reasoning_effort}
        payload.update(model.extra)

        last_error = None
        attempts = 6
        for attempt in range(attempts):
            try:
                resp = self.http.post(OPENROUTER_URL, json=payload)
            except httpx.HTTPError as e:
                last_error = f"network error: {e}"
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("error"):
                        last_error = f"provider error: {data['error']}"
                    elif data.get("choices"):
                        return data
                    else:
                        last_error = f"empty response: {str(data)[:300]}"
                elif resp.status_code in RETRYABLE_STATUS:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                else:
                    # 400/401/402/404 won't fix themselves — e.g. 404 "no endpoints found"
                    # when every provider matching the precision pin is unavailable.
                    raise OpenRouterError(f"HTTP {resp.status_code}: {resp.text[:500]}")
            if attempt < attempts - 1:
                time.sleep(min(30, 2 ** (attempt + 1)))  # 2, 4, 8, 16, 30s — rides out rate limits
        raise OpenRouterError(f"Giving up after retries — {last_error}")
