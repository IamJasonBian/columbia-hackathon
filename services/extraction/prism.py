"""Fail-open PRISM trace delivery for direct model clients."""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_HOST = "https://prism.blockconvey.com"
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="prismtrace")


@dataclass(frozen=True)
class PrismConfig:
    api_key: str
    project_id: str
    host: str

    @classmethod
    def from_env(cls) -> PrismConfig | None:
        api_key = os.environ.get("PRISMTRACE_API_KEY", "").strip()
        project_id = os.environ.get("PRISMTRACE_PROJECT_ID", "").strip()
        if not api_key or not project_id:
            return None
        host = os.environ.get("PRISMTRACE_HOST", DEFAULT_HOST).strip().rstrip("/")
        return cls(api_key=api_key, project_id=project_id, host=host)


def _token_counts(usage: dict[str, Any]) -> tuple[int, int]:
    input_count = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    output_count = usage.get("output_tokens", usage.get("completion_tokens", 0))
    return int(input_count or 0), int(output_count or 0)


def build_trace(
    config: PrismConfig,
    *,
    model: str,
    input_messages: list[dict[str, str]],
    output_message: str,
    latency_ms: int,
    usage: dict[str, Any] | None = None,
    session_id: str | None = None,
    agent_id: str,
    agent_name: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    input_tokens, output_tokens = _token_counts(usage or {})
    trace: dict[str, Any] = {
        "project_id": config.project_id,
        "model": model,
        "input_messages": input_messages,
        "output_message": output_message,
        "latency_ms": max(0, latency_ms),
        "token_count_input": input_tokens,
        "token_count_output": output_tokens,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "metadata": metadata or {},
    }
    if session_id:
        trace["session_id"] = session_id
    return trace


def _send(config: PrismConfig, trace: dict[str, Any]) -> None:
    try:
        response = requests.post(
            f"{config.host}/api/traces",
            headers={
                "Content-Type": "application/json",
                "X-PRISMtrace-Key": config.api_key,
            },
            json=trace,
            timeout=5,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("PRISM trace delivery failed: %s", exc)


def emit_trace(**kwargs: Any) -> None:
    """Queue one trace without adding PRISM latency to the model request."""
    config = PrismConfig.from_env()
    if config is None:
        return
    trace = build_trace(config, **kwargs)
    _executor.submit(_send, config, trace)
