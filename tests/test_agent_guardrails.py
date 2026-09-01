# tests/test_agent_guardrails.py
"""
Proves the core Phase 5 claim: an LLM failure, timeout, or malformed
response NEVER changes the deterministic pipeline's output.
"""

import ast
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from src.agent.guardrails import call_llm_bounded
from src.agent.narration_extractor import extract_txn_id_via_llm
from src.agent.explainer import explain_decision_via_llm, fallback_template_explanation
from src.models import MatchDecision, DecisionStatus, ExceptionCode

NEWLINE = "\n"


def _failing_llm(prompt: str) -> str:
    raise ConnectionError("simulated API outage")


# Released by the timeout test once its assertions have run.
#
# This used to be `time.sleep(15)`. The assertion was identical, but the
# abandoned worker then sat in the SHARED module-level pool for the full
# 15 seconds -- a quarter of the pool, held for 5s after the test had
# already finished proving its point, and Python cannot reclaim it. An
# Event releases the thread the moment the proof is done.
#
# The proof is if anything stronger: at assertion time the call provably
# has NOT completed, because nothing has released it yet. See
# tests/test_agent_concurrency.py.
_HANG_UNTIL_RELEASED = threading.Event()


def _slow_llm(prompt: str) -> str:
    # Ceiling is a backstop against a wedged test run, not the property
    # under test. AGENT_CALL_TIMEOUT_SECONDS (10s) fires long before it.
    _HANG_UNTIL_RELEASED.wait(timeout=60)
    return "TXN_00001"


def _malformed_llm(prompt: str) -> str:
    return "I think it might be transaction number one, not totally sure though!"


def _valid_llm(prompt: str) -> str:
    return "TXN_00042"


def test_llm_failure_returns_unsuccessful_not_exception():
    result = extract_txn_id_via_llm("some narration", _failing_llm)
    assert result.succeeded is False
    assert result.value is None
    assert "failed" in result.error.lower()


def test_malformed_llm_output_rejected_not_used():
    result = extract_txn_id_via_llm("some narration", _malformed_llm)
    assert result.succeeded is False
    assert result.value is None


def test_valid_llm_output_accepted():
    result = extract_txn_id_via_llm("NEFT CR TXN_00042 MERCH_001", _valid_llm)
    assert result.succeeded is True
    assert result.value.proposed_txn_id == "TXN_00042"


def test_explanation_failure_has_working_fallback():
    decision = MatchDecision(
        txn_id="TXN_TEST", status=DecisionStatus.TAX_MISMATCH,
        confidence_score=95, exception_code=ExceptionCode.ERR_GST_MISMATCH,
        reason_codes=[ExceptionCode.ERR_GST_MISMATCH],
    )
    result = explain_decision_via_llm(decision, _failing_llm)
    assert result.succeeded is False

    fallback = fallback_template_explanation(decision)
    assert "TXN_TEST" in fallback.text          # .text, was bare string
    assert "TAX_MISMATCH" in fallback.text
    assert len(fallback.text) > 20


# ======================================================================
# THE SINGLE SANCTIONED PATH -- STRUCTURAL
# ======================================================================
#
# This section replaces:
#
#     def test_llm_never_used_directly_without_guardrail():
#         assert callable(call_llm_bounded)
#
# which carried that name for the life of the project and asserted
# nothing. It would have passed if every module under src/agent/ bypassed
# the guardrail entirely.
#
# That is the FOURTH instance of the pattern this project has already
# named three times -- FAILURE_LOG.md section 4 ("a conditional invariant
# tells you nothing when the condition never occurs"), section 20 ("a
# tested boundary the production path does not go through is not a
# boundary"), and section 62 (the faithfulness validator wired to the
# evaluation harness and not to the product).
#
# Section 63 records that the fourth one was found in this file, under a
# name that claimed to prevent exactly it.

MODEL_CALLABLES = {"llm_call_fn", "as_callable"}

# The only subtree permitted to name a provider SDK. guardrails.py is the
# chokepoint; providers/ is the adapter layer it calls through.
SDK_PERMITTED_PREFIXES = ("src/agent/providers/",)

SDK_MARKERS = {"google", "genai", "openai", "anthropic", "httpx", "requests"}


def _agent_modules():
    for path in sorted((ROOT / "src" / "agent").rglob("*.py")):
        yield str(path.relative_to(ROOT)).replace("\\", "/"), path


def _calls_to(tree, names):
    """(lineno, name) for every Call whose callee name is in `names`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name in names:
                yield node.lineno, name


def _wrapped_call_linenos(tree):
    """Linenos of model calls inside a call_llm_bounded(call_fn=...)."""
    wrapped = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "call_llm_bounded":
            continue

        for keyword in node.keywords:
            if keyword.arg != "call_fn":
                continue
            for lineno, _ in _calls_to(keyword.value, MODEL_CALLABLES):
                wrapped.add(lineno)

    return wrapped


def test_every_model_call_goes_through_the_guardrail():
    """
    THE ACTUAL CLAIM, ASSERTED.

    guardrails.py states: "Every LLM call in this codebase MUST pass
    through call_llm_bounded(). No other module is permitted to call an
    LLM API directly."

    Every invocation of a model callable anywhere under src/agent/ must
    sit lexically inside the `call_fn` argument of a `call_llm_bounded`
    call. A new capability that calls its provider directly fails here --
    which is the only moment anyone would notice before it shipped.
    """
    unwrapped = []

    for relative, path in _agent_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        wrapped = _wrapped_call_linenos(tree)

        for lineno, name in _calls_to(tree, MODEL_CALLABLES):
            if lineno not in wrapped:
                unwrapped.append(f"{relative}:{lineno} calls {name}()")

    assert not unwrapped, (
        "model call(s) not wrapped in call_llm_bounded(call_fn=...):"
        + NEWLINE + "  "
        + (NEWLINE + "  ").join(unwrapped)
        + NEWLINE + NEWLINE
        + "Every LLM call must go through the guardrail so the timeout, "
        "parse containment and validation apply. If the call is "
        "deliberate it still needs the guardrail -- change the call, not "
        "this test."
    )


def test_the_guardrail_sweep_can_actually_fail():
    """
    THE CONTROL.

    A sweep that finds nothing proves nothing until you know it can find
    something. This feeds the same AST logic a module that bypasses the
    guardrail and asserts it is caught, then a compliant one and asserts
    it is not.

    Without this, a typo in MODEL_CALLABLES would make the test above
    pass permanently and silently -- which is the same failure the test
    above exists to prevent, one level up. Writing the guard without
    writing its control is how this file got into trouble the first time.
    """
    bypassing = ast.parse(
        "def go(llm_call_fn):" + NEWLINE +
        "    return llm_call_fn('prompt')" + NEWLINE
    )
    assert list(_calls_to(bypassing, MODEL_CALLABLES)), (
        "the sweep cannot see a direct model call at all"
    )
    assert not _wrapped_call_linenos(bypassing), (
        "the sweep counted an unwrapped call as wrapped"
    )

    compliant = ast.parse(
        "def go(llm_call_fn):" + NEWLINE +
        "    return call_llm_bounded(" + NEWLINE +
        "        call_fn=lambda: llm_call_fn('prompt')," + NEWLINE +
        "        parse_fn=str, validate_fn=bool," + NEWLINE +
        "    )" + NEWLINE
    )
    assert _wrapped_call_linenos(compliant), (
        "the sweep failed to recognise a correctly wrapped call"
    )


def test_only_the_provider_layer_names_a_provider_sdk():
    """
    The import-level half of the same property.

    A module importing `google.genai` has a route to the network the
    guardrail cannot see, whatever its call sites look like.
    """
    offenders = []

    for relative, path in _agent_modules():
        if relative.startswith(SDK_PERMITTED_PREFIXES):
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue

            for name in names:
                if name.split(".")[0] in SDK_MARKERS:
                    offenders.append(
                        f"{relative}:{node.lineno} imports {name}"
                    )

    assert not offenders, (
        "provider SDK imported outside src/agent/providers/:"
        + NEWLINE + "  "
        + (NEWLINE + "  ").join(offenders)
    )


def test_the_guardrail_is_importable_and_callable():
    """
    What the old one-liner actually checked, kept as a smoke test rather
    than deleted -- but no longer wearing a name that promises more.
    """
    assert callable(call_llm_bounded)


def test_real_timeout_returns_before_slow_call_completes():
    """Proves the pipeline does not wait for a hung LLM call -- this
    must complete in well under 15s, confirming the timeout is
    preemptive (via ThreadPoolExecutor.future.result(timeout=...)),
    not a post-hoc elapsed-time check after the call already
    returned."""
    _HANG_UNTIL_RELEASED.clear()
    try:
        start = time.perf_counter()
        result = extract_txn_id_via_llm("some narration", _slow_llm)
        elapsed = time.perf_counter() - start

        assert result.succeeded is False
        assert "timeout" in result.error.lower()
        assert elapsed < 12, (
            f"Pipeline waited {elapsed:.1f}s for a call that should have "
            f"timed out at 10s -- timeout is not actually preemptive"
        )
    finally:
        # Return the worker to the shared pool even if an assertion
        # failed. A leaked worker turns one red test into several.
        _HANG_UNTIL_RELEASED.set()


if __name__ == "__main__":
    test_llm_failure_returns_unsuccessful_not_exception()
    test_malformed_llm_output_rejected_not_used()
    test_valid_llm_output_accepted()
    test_explanation_failure_has_working_fallback()
    test_every_model_call_goes_through_the_guardrail()
    test_the_guardrail_sweep_can_actually_fail()
    test_only_the_provider_layer_names_a_provider_sdk()
    test_the_guardrail_is_importable_and_callable()
    test_real_timeout_returns_before_slow_call_completes()
    print("All Phase 5 agent guardrail tests passed -- LLM failure never corrupts deterministic output, including a REAL enforced timeout.")
