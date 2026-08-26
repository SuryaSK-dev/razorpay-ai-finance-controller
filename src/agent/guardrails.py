# src/agent/guardrails.py
"""
Shared enforcement layer for every AI call in this system.

Every LLM call in this codebase MUST pass through call_llm_bounded().
No other module is permitted to call an LLM API directly.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar, Generic
import time
import concurrent.futures

T = TypeVar("T")

AGENT_CALL_TIMEOUT_SECONDS = 10


@dataclass
class AgentCallResult(Generic[T]):
    succeeded: bool
    value: Optional[T]
    raw_response: Optional[str]
    error: Optional[str]
    latency_seconds: float


_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)


def call_llm_bounded(
    call_fn: Callable[[], str],
    parse_fn: Callable[[str], T],
    validate_fn: Callable[[T], bool],
) -> AgentCallResult[T]:
    """
    The ONLY sanctioned way to call an LLM in this codebase.

    Enforces a REAL preemptive timeout via a thread pool future --
    if call_fn() has not returned within AGENT_CALL_TIMEOUT_SECONDS,
    this function returns a failure result immediately, regardless
    of whether the underlying call eventually completes. The
    underlying thread is abandoned (Python cannot forcibly kill a
    running thread), but the PIPELINE never waits on it -- this is
    the distinction that makes the timeout real rather than
    cosmetic.
    """
    start = time.perf_counter()
    future = _executor.submit(call_fn)

    try:
        raw = future.result(timeout=AGENT_CALL_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        return AgentCallResult(
            succeeded=False, value=None, raw_response=None,
            error=f"LLM call exceeded {AGENT_CALL_TIMEOUT_SECONDS}s timeout "
                  f"(pipeline did not wait for it to complete)",
            latency_seconds=time.perf_counter() - start,
        )
    except Exception as e:
        return AgentCallResult(
            succeeded=False, value=None, raw_response=None,
            error=f"LLM call failed: {e}",
            latency_seconds=time.perf_counter() - start,
        )

    elapsed = time.perf_counter() - start

    try:
        parsed = parse_fn(raw)
    except Exception as e:
        return AgentCallResult(
            succeeded=False, value=None, raw_response=raw,
            error=f"Failed to parse LLM output: {e}",
            latency_seconds=elapsed,
        )

    if not validate_fn(parsed):
        return AgentCallResult(
            succeeded=False, value=None, raw_response=raw,
            error="LLM output failed validation -- rejected, not used",
            latency_seconds=elapsed,
        )

    return AgentCallResult(
        succeeded=True, value=parsed, raw_response=raw,
        error=None, latency_seconds=elapsed,
    )