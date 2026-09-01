# tests/test_agent_invariants.py
"""
Proves the core Phase 5 claim with a direct before/after comparison,
not an indirect inference: running the agent controller over a
MatchDecision produces IDENTICAL financial facts before and after --
status, exception_code, reason_codes, confidence_score never change.
Only explanation (new, additive content) differs.
"""

import sys
import copy
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.models import MatchDecision, DecisionStatus, ExceptionCode
from src.agent.controller import FinanceControllerAgent


def _sample_decision() -> MatchDecision:
    return MatchDecision(
        txn_id="TXN_INVARIANT", status=DecisionStatus.TAX_MISMATCH,
        confidence_score=94, exception_code=ExceptionCode.ERR_GST_MISMATCH,
        reason_codes=[ExceptionCode.ERR_GST_MISMATCH, ExceptionCode.ERR_TDS_VARIANCE],
        evidence={"gst": {"expected": "9.00", "claimed": "6.00"}},
    )


def _working_llm(prompt: str) -> str:
    """
    A FAITHFUL explanation -- it carries every authoritative token
    forward verbatim.

    This stub used to return "This is a sample explanation of the tax
    mismatch." That passed for the whole life of the project because
    explain() validated text LENGTH only. Once the faithfulness
    validator was wired into the runtime path (FAILURE_LOG.md section
    62) it stopped being a successful call and started being a
    rejection, which made this test assert the opposite of its own
    name. The stub was wrong, not the check: prose that names no fact
    is exactly what the validator exists to refuse.
    """
    return (
        "Transaction TXN_INVARIANT was resolved as TAX_MISMATCH. "
        "Violated conditions: ERR_GST_MISMATCH and ERR_TDS_VARIANCE. "
        "Confidence 94."
    )


def _broken_llm(prompt: str) -> str:
    raise ConnectionError("simulated failure")


def test_decision_facts_unchanged_after_successful_llm_explanation():
    decision_before = _sample_decision()
    snapshot = copy.deepcopy(decision_before)

    agent = FinanceControllerAgent(_working_llm)
    response = agent.explain(decision_before)

    assert decision_before.status == snapshot.status
    assert decision_before.exception_code == snapshot.exception_code
    assert decision_before.reason_codes == snapshot.reason_codes
    assert decision_before.confidence_score == snapshot.confidence_score
    assert decision_before.evidence == snapshot.evidence

    assert response.status == snapshot.status.value
    assert response.explanation_source == "llm"


def test_decision_facts_unchanged_after_failed_llm_explanation():
    decision_before = _sample_decision()
    snapshot = copy.deepcopy(decision_before)

    agent = FinanceControllerAgent(_broken_llm)
    response = agent.explain(decision_before)

    assert decision_before.status == snapshot.status
    assert decision_before.exception_code == snapshot.exception_code
    assert decision_before.reason_codes == snapshot.reason_codes
    assert decision_before.confidence_score == snapshot.confidence_score

    assert response.status == snapshot.status.value
    assert response.explanation_source == "deterministic_fallback"
    assert len(response.explanation) > 0


def test_proposed_candidate_not_in_index_is_discarded():
    from src.agent.tools.candidate_lookup import lookup_proposed_txn_id
    from src.matching.candidates import CandidateIndex
    from src.agent.contracts import NarrationExtraction

    empty_index = CandidateIndex([], [])

    extraction = NarrationExtraction(proposed_txn_id="TXN_99999")
    result = lookup_proposed_txn_id(extraction, empty_index)

    assert result.found is False
    assert result.candidates == []


if __name__ == "__main__":
    test_decision_facts_unchanged_after_successful_llm_explanation()
    test_decision_facts_unchanged_after_failed_llm_explanation()
    test_proposed_candidate_not_in_index_is_discarded()
    print("All Phase 5 invariant tests passed -- financial facts provably unchanged by agent layer.")