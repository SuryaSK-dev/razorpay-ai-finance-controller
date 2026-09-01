# tests/test_agent_concurrency.py
"""
The concurrency half of the timeout guarantee.

WHAT WAS ALREADY PROVEN, AND WHAT WAS NOT
-----------------------------------------
`test_real_timeout_returns_before_slow_call_completes` proves the
SINGLE-call property: one hung provider call returns in under 12s against
a 15s hang, measured on the wall clock. That is the strong form and it
holds.

It says nothing about what happens when several calls hang at once, and
`guardrails.py` uses a module-level pool with a fixed worker count. Python
cannot kill an abandoned thread, so a hung call holds its worker until the
call itself returns -- which for a genuinely wedged provider is never.

That makes worker exhaustion a real operating mode, and it was the one
question the repository could not answer (FAILURE_LOG.md section 63.5's
"documented, not solved", ARCHITECTURE.md section 5). These tests do not
solve it -- solving it needs a circuit breaker, which is a design change.
They establish what the behaviour ACTUALLY IS, so the claim in
ARCHITECTURE.md is measured rather than reasoned.

THE PROPERTY THAT MATTERS
-------------------------
The guarantee this system makes is *"the pipeline does not wait"* -- not
*"the provider call is terminated"*. Under saturation that guarantee must
degrade to *bounded failure*, never to a hang:

    N concurrent hangs   -> each returns at the timeout, concurrently
    the (N+1)th caller   -> still returns at the timeout, having never run
    after release        -> the pool recovers and serves normally

A deadlock, an unbounded wait, or a caller that never returns would all be
far worse than a timeout, and none of them was ruled out before this file.

WHY threading.Event RATHER THAN time.sleep
------------------------------------------
A `time.sleep(15)` stub abandons a worker for the full 15 seconds, into a
pool shared with every other test in the suite. Two such tests existed and
each held a worker for 15s -- half the pool, for no benefit after the
assertion had already passed.

An Event lets the test release every abandoned thread the moment the
assertions are done, so the pool is clean when the test exits. The proof
is identical and arguably stronger: at assertion time the call provably
has NOT completed, because nothing has released it yet.
"""

import concurrent.futures
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from src.agent import guardrails
from src.agent.guardrails import call_llm_bounded

# Short enough to keep the suite fast, long enough that scheduling jitter
# on a loaded machine cannot be mistaken for a timeout.
TEST_TIMEOUT_SECONDS = 2


@pytest.fixture
def fast_timeout(monkeypatch):
    """
    Patch the guardrail's timeout down for these tests.

    `call_llm_bounded` reads AGENT_CALL_TIMEOUT_SECONDS as a module global
    at call time, so patching the attribute changes behaviour without
    touching the real configured value -- which the single-call test in
    test_agent_guardrails.py still exercises unpatched.
    """
    monkeypatch.setattr(
        guardrails, "AGENT_CALL_TIMEOUT_SECONDS", TEST_TIMEOUT_SECONDS
    )
    return TEST_TIMEOUT_SECONDS


@pytest.fixture
def hanging_call():
    """
    A provider stub that blocks until the test releases it.

    Yields (call_fn, release). The release is fired in teardown too, so a
    failing assertion cannot leak a worker into the rest of the suite.
    """
    release = threading.Event()

    def call_fn() -> str:
        # The generous ceiling is a backstop against a hung test run, not
        # part of the property under test.
        release.wait(timeout=60)
        return "late response"

    yield call_fn, release
    release.set()


def _bounded(call_fn):
    return call_llm_bounded(
        call_fn=call_fn,
        parse_fn=lambda raw: raw,
        validate_fn=lambda value: True,
    )


def _workers() -> int:
    return guardrails._executor._max_workers


# ======================================================================
# SATURATION
# ======================================================================

def test_concurrent_hung_calls_each_return_at_the_timeout(
    fast_timeout, hanging_call
):
    """
    Fill every worker at once. Each caller must come back at the timeout,
    and they must come back CONCURRENTLY.

    The elapsed-time assertion is the real content: if the calls were
    serialised, total time would be workers x timeout. Asserting it stays
    near a single timeout proves the pool is genuinely parallel and that
    one hung call does not delay another caller's failure.
    """
    call_fn, release = hanging_call
    workers = _workers()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as callers:
        start = time.perf_counter()
        futures = [callers.submit(_bounded, call_fn) for _ in range(workers)]
        results = [f.result(timeout=60) for f in futures]
        elapsed = time.perf_counter() - start

    release.set()

    assert len(results) == workers
    for result in results:
        assert result.succeeded is False
        assert result.value is None
        assert "timeout" in result.error.lower()

    assert elapsed < TEST_TIMEOUT_SECONDS * 2, (
        f"{workers} concurrent hung calls took {elapsed:.1f}s. A single "
        f"timeout is {TEST_TIMEOUT_SECONDS}s, so this is closer to "
        f"serialised than concurrent -- callers are queueing behind each "
        f"other's failures."
    )


def test_a_caller_beyond_pool_capacity_still_returns(fast_timeout, hanging_call):
    """
    THE ACTUAL EXHAUSTION QUESTION.

    With every worker held, the next caller's task is queued and never
    starts. It must still return -- at the timeout, with a timeout error --
    rather than blocking forever.

    This is the difference between *bounded degradation* and a *hang*, and
    it is the only thing that makes "the pipeline does not wait" survive
    contact with a wedged provider. The answer is not good news; it is
    knowable news.
    """
    call_fn, release = hanging_call
    workers = _workers()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as callers:
        saturating = [callers.submit(_bounded, call_fn) for _ in range(workers)]

        # Let the pool actually pick the tasks up before measuring.
        time.sleep(0.3)

        start = time.perf_counter()
        overflow = _bounded(call_fn)
        elapsed = time.perf_counter() - start

        release.set()
        for future in saturating:
            future.result(timeout=60)

    assert overflow.succeeded is False
    assert "timeout" in overflow.error.lower()
    assert elapsed < TEST_TIMEOUT_SECONDS * 3, (
        f"the caller past pool capacity waited {elapsed:.1f}s -- it must "
        f"fail at the timeout, not queue indefinitely"
    )


def test_the_pool_recovers_once_hung_calls_release(fast_timeout, hanging_call):
    """
    Exhaustion must be a phase, not a terminal state.

    If the workers were never reclaimed after the provider recovered, the
    process would be permanently degraded and a restart would be the only
    remedy. Asserting recovery is what makes "documented limitation"
    honest rather than a euphemism.
    """
    call_fn, release = hanging_call
    workers = _workers()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as callers:
        saturating = [callers.submit(_bounded, call_fn) for _ in range(workers)]
        for future in saturating:
            future.result(timeout=60)

        release.set()
        for future in saturating:
            future.result(timeout=60)

    # Give the released threads a moment to return their workers.
    deadline = time.perf_counter() + 10
    recovered = None
    while time.perf_counter() < deadline:
        recovered = _bounded(lambda: "healthy response")
        if recovered.succeeded:
            break

    assert recovered is not None and recovered.succeeded is True, (
        "the pool never served a normal call again after the hung calls "
        "were released -- worker exhaustion is permanent, not a phase"
    )
    assert recovered.value == "healthy response"


# ======================================================================
# THE NUMBER THAT DECIDES ALL OF THE ABOVE
# ======================================================================

def test_the_worker_count_is_deliberate():
    """
    `max_workers` IS the saturation threshold, so it is load-bearing for
    every claim in this file and for ARCHITECTURE.md section 5.

    Pinned so that changing it is a decision rather than a side effect. If
    you raise it, the concurrency story changes and that section needs
    rewriting -- update both, do not delete this.
    """
    assert _workers() == 4, (
        f"pool size changed to {_workers()}. ARCHITECTURE.md section 5 "
        f"states the exhaustion threshold as four simultaneous hangs; "
        f"update it together with this assertion."
    )


def test_the_executor_is_module_level_not_a_context_manager():
    """
    STRUCTURAL, and the subtlest thing in the guardrail.

    `with ThreadPoolExecutor() as pool:` would block on `__exit__` waiting
    for the very thread this design deliberately abandons -- silently
    converting the preemptive timeout back into "wait for the provider".
    The single-call wall-clock test would then fail, but only because of a
    change made somewhere else entirely, which is a bad way to find out.

    This asserts the shape at the point where the reasoning lives.
    """
    source = (ROOT / "src" / "agent" / "guardrails.py").read_text(
        encoding="utf-8"
    )

    assert "_executor = concurrent.futures.ThreadPoolExecutor" in source, (
        "the module-level executor is gone -- if it moved inside a "
        "context manager, the timeout is no longer preemptive"
    )

    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "with concurrent.futures.ThreadPoolExecutor" not in stripped, (
            "ThreadPoolExecutor used as a context manager: __exit__ joins "
            "the abandoned worker, so call_llm_bounded would wait for the "
            "hung provider call it just gave up on"
        )
