# tests/test_provider_failures.py
"""
Phase 5B.5 — Provider failure tests.

These tests verify that provider-level failures remain contained
inside the existing Phase 5 guardrail boundary.

Important:
    These tests intentionally use local failure simulation.

We do NOT deliberately generate real 429/network failures against
the Gemini Free Tier because doing so would consume quota without
providing additional architectural evidence.

The real Gemini API connectivity is already established by the
5B.4 smoke test.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.agent.config import AgentConfig          # was: load_agent_config
from src.agent.guardrails import call_llm_bounded
from src.agent.providers.base import ProviderResponse
from src.agent.providers.gemini_provider import GeminiProvider


def _failing_provider_call(prompt: str) -> str:
    """
    Simulates a provider/API failure after the provider boundary.
    """
    raise ConnectionError("simulated Gemini API connection failure")


def _slow_provider_call(prompt: str) -> str:
    """
    Simulates a provider that never responds within the guardrail
    timeout.
    """
    time.sleep(15)
    return "late response"


def _empty_provider_call(prompt: str) -> str:
    """
    Simulates an API response with no usable text.
    """
    return ""


def _malformed_provider_call(prompt: str) -> str:
    """
    Simulates a successful transport response containing malformed
    model output.
    """
    return "This is not a valid transaction ID."


def test_provider_exception_is_contained_by_guardrail():
    result = call_llm_bounded(
        call_fn=lambda: _failing_provider_call("test"),
        parse_fn=lambda raw: raw,
        validate_fn=lambda value: True,
    )

    assert result.succeeded is False
    assert result.value is None
    assert result.error is not None
    assert "failed" in result.error.lower()


def test_slow_provider_is_bounded_by_guardrail():
    start = time.perf_counter()

    result = call_llm_bounded(
        call_fn=lambda: _slow_provider_call("test"),
        parse_fn=lambda raw: raw,
        validate_fn=lambda value: True,
    )

    elapsed = time.perf_counter() - start

    assert result.succeeded is False
    assert result.value is None
    assert result.error is not None
    assert "timeout" in result.error.lower()

    # Must return before the simulated 15-second provider call finishes.
    assert elapsed < 12, (
        f"Guardrail waited {elapsed:.1f}s for a provider call "
        "that should have timed out at 10s"
    )


def test_empty_provider_response_is_rejected():
    result = call_llm_bounded(
        call_fn=lambda: _empty_provider_call("test"),
        parse_fn=lambda raw: raw.strip(),
        validate_fn=lambda value: bool(value),
    )

    assert result.succeeded is False
    assert result.value is None
    assert result.error is not None
    assert "validation" in result.error.lower()


def test_malformed_provider_response_is_rejected():
    result = call_llm_bounded(
        call_fn=lambda: _malformed_provider_call("test"),
        parse_fn=lambda raw: raw,
        validate_fn=lambda value: value.startswith("TXN_"),
    )

    assert result.succeeded is False
    assert result.value is None
    assert result.error is not None
    assert "validation" in result.error.lower()


def test_gemini_provider_rejects_empty_prompt():
    """
    Pure input validation -- no credentials, no network. The empty-prompt
    guard fires before any API call, so a placeholder key is sufficient
    and keeps this test runnable in CI without secrets.
    """
    config = AgentConfig(
        gemini_api_key="test-key-never-used",
        gemini_model="gemini-3.1-flash-lite",
        timeout_seconds=10,
        max_output_tokens=512,
        free_only=True,
    )
    provider = GeminiProvider(config)

    try:
        provider.call("")
        assert False, "Expected empty prompt to be rejected"
    except ValueError as exc:
        assert "empty" in str(exc).lower()


def test_provider_response_contains_no_financial_authority():
    """
    Structural regression guard.

    ProviderResponse may carry operational metadata, but it must not
    acquire financial decision fields.
    """

    fields = ProviderResponse.__dataclass_fields__.keys()

    forbidden = {
        "status",
        "decision",
        "amount",
        "gst",
        "tds",
        "exception_code",
        "matched",
        "confidence_score",
    }

    assert not forbidden.intersection(fields)


if __name__ == "__main__":
    test_provider_exception_is_contained_by_guardrail()
    test_slow_provider_is_bounded_by_guardrail()
    test_empty_provider_response_is_rejected()
    test_malformed_provider_response_is_rejected()
    test_gemini_provider_rejects_empty_prompt()
    test_provider_response_contains_no_financial_authority()

    print(
        "All Phase 5B provider failure tests passed -- "
        "provider failures remain contained and cannot acquire "
        "financial authority."
    )