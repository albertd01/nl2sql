"""Model registry with pinned OpenRouter provider routing.

Precision policy: the highest precision each model is served at on OpenRouter, with no
fallback to lower-precision or unlabeled ("unknown") endpoints. Checked 2026-09-15:
  - gemma-4-31b-it:     bf16 on Venice, Novita (and Crusoe, relabeled bf16 later that day)
  - qwen3.8-27b:        bf16 on DeepInfra only
  - deepseek-v4-flash:  no bf16 endpoint; fp8 is the highest available
If every allowed provider is down, the request fails rather than silently degrading.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelSpec:
    key: str
    openrouter_id: str
    label: str
    quantizations: tuple[str, ...]
    reasoning_effort: str | None = None
    extra: dict = field(default_factory=dict)

    def provider_routing(self) -> dict:
        return {
            "quantizations": list(self.quantizations),
            "require_parameters": True,  # only endpoints that support tool calling + our params
        }


MODELS: dict[str, ModelSpec] = {
    "gemma": ModelSpec(
        key="gemma",
        openrouter_id="google/gemma-4-31b-it",
        label="Gemma 4 31B (bf16)",
        quantizations=("bf16",),
    ),
    "qwen": ModelSpec(
        key="qwen",
        openrouter_id="qwen/qwen3.8-27b",
        label="Qwen 3.8 27B (bf16)",
        quantizations=("bf16",),
    ),
    "deepseek": ModelSpec(
        key="deepseek",
        openrouter_id="deepseek/deepseek-v4-flash",
        label="DeepSeek V4 Flash (fp8)",
        quantizations=("fp8",),
    ),
}


def get_model(key: str) -> ModelSpec:
    try:
        return MODELS[key]
    except KeyError:
        raise ValueError(f"Unknown model '{key}'. Choose from: {', '.join(MODELS)}") from None
