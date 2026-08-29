# src/agent/config.py
"""
Phase 5B configuration boundary.

This module contains configuration for the real LLM provider only.
It must not contain financial logic, matching logic, tax logic, or
decisioning.

Security rules:
- API credentials come only from environment variables.
- Secrets are never hardcoded.
- Provider/model configuration is explicit.
- Free-tier-only operation is an explicit project constraint.
- No paid fallback or automatic model escalation is permitted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentConfig:
    """
    Immutable configuration for Phase 5B.

    No financial fields are permitted here.
    """

    gemini_api_key: str
    gemini_model: str
    timeout_seconds: int
    max_output_tokens: int
    free_only: bool = True


def _read_positive_int(name: str, default: int) -> int:
    """Read a positive integer environment variable."""

    raw = os.getenv(name, str(default)).strip()

    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{name} must be a positive integer; received {raw!r}."
        ) from exc

    if value <= 0:
        raise RuntimeError(
            f"{name} must be greater than zero; received {value}."
        )

    return value


def load_agent_config() -> AgentConfig:
    """
    Load Phase 5B configuration from environment variables.

    Required:
        GEMINI_API_KEY

    Optional:
        GEMINI_MODEL
        AGENT_CALL_TIMEOUT_SECONDS
        AGENT_MAX_OUTPUT_TOKENS
        AGENT_FREE_ONLY

    Missing credentials fail fast. No network request is made here.
    """

    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not configured. "
            "Set it as an environment variable; "
            "never hardcode the API key in source code."
        )

    model = os.getenv(
        "GEMINI_MODEL",
        "gemini-3.1-flash-lite",
    ).strip()

    if not model:
        raise RuntimeError(
            "GEMINI_MODEL cannot be empty."
        )

    timeout_seconds = _read_positive_int(
        "AGENT_CALL_TIMEOUT_SECONDS",
        default=10,
    )

    max_output_tokens = _read_positive_int(
        "AGENT_MAX_OUTPUT_TOKENS",
        default=512,
    )

    free_only_raw = os.getenv(
        "AGENT_FREE_ONLY",
        "true",
    ).strip().lower()

    if free_only_raw not in {"true", "false"}:
        raise RuntimeError(
            "AGENT_FREE_ONLY must be either 'true' or 'false'."
        )

    free_only = free_only_raw == "true"

    if not free_only:
        raise RuntimeError(
            "This project requires strict $0 API usage. "
            "AGENT_FREE_ONLY must remain true."
        )

    return AgentConfig(
        gemini_api_key=api_key,
        gemini_model=model,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
        free_only=free_only,
    )