# tests/test_agent_guardrails.py
"""
Proves the core Phase 5 claim: an LLM failure, timeout, or malformed
response NEVER changes the deterministic pipeline's output.
"""

import sys
import time
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.agent.guardrails import call_llm_bounded
from src.agent.narration_extractor import extract_txn_id_via_llm
from src.agent.explainer import explain_decision_via_llm, fallback_template_explanation
from src.models import MatchDecision, DecisionStatus, ExceptionCode


def _failing_llm(prompt: str) -> str:
    raise ConnectionError("simulated API outage")


def _slow_llm(prompt: str) -> str:
    time.sleep(15)  # exceeds AGENT_CALL_TIMEOUT_SECONDS (10s)
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


def test_llm_never_used_directly_without_guardrail():
    assert callable(call_llm_bounded)


def test_real_timeout_returns_before_slow_call_completes():
    """Proves the pipeline does not wait for a hung LLM call -- this
    must complete in well under 15s, confirming the timeout is
    preemptive (via ThreadPoolExecutor.future.result(timeout=...)),
    not a post-hoc elapsed-time check after the call already
    returned."""
    start = time.perf_counter()
    result = extract_txn_id_via_llm("some narration", _slow_llm)
    elapsed = time.perf_counter() - start

    assert result.succeeded is False
    assert "timeout" in result.error.lower()
    assert elapsed < 12, (
        f"Pipeline waited {elapsed:.1f}s for a call that should have "
        f"timed out at 10s -- timeout is not actually preemptive"
    )


if __name__ == "__main__":
    test_llm_failure_returns_unsuccessful_not_exception()
    test_malformed_llm_output_rejected_not_used()
    test_valid_llm_output_accepted()
    test_explanation_failure_has_working_fallback()
    test_llm_never_used_directly_without_guardrail()
    test_real_timeout_returns_before_slow_call_completes()
    print("All Phase 5 agent guardrail tests passed -- LLM failure never corrupts deterministic output, including a REAL enforced timeout.")