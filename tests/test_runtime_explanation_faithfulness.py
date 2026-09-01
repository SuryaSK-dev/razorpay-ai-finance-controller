# tests/test_runtime_explanation_faithfulness.py
"""
Proves the faithfulness validator runs on the PRODUCTION path.

WHY THIS FILE EXISTS SEPARATELY FROM test_explanation_validator.py
------------------------------------------------------------------
tests/test_explanation_validator.py calls `validate_explanation()`
directly. Every one of those tests passed for the entire life of the
project while `FinanceControllerAgent.explain()` was validating text
LENGTH and nothing else, because src/agent/explainer.py handed the
guardrail this:

    def _validate_explanation(value): return True

A test that calls the validator proves the validator works. It does
not prove the runtime uses it. That distinction IS the defect -- see
FAILURE_LOG.md section 62, and section 20 for the first time this
project shipped a tested contract the production path routed around.

So every test here goes through `FinanceControllerAgent.explain()`,
over a MatchDecision produced by the real `decide_batch()`, with a
stub `llm_call_fn`. Nothing here imports `validate_explanation`
except the one structural test that asserts the wiring still exists.
"""

import copy
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from src.agent.controller import FinanceControllerAgent
from src.exceptions.manager import decide_batch
from src.ingestion.loader import load_batch
from src.matching.engine import run_matching
from src.models import ExceptionCode, MatchDecision
from src.normalization.engine import normalize_batch

RAW_DIR = ROOT / "data" / "raw"

# A figure that appears nowhere in the batch, so its presence in a
# returned explanation can only mean the model's text survived.
FABRICATED_AMOUNT = "9999999.99"


# ======================================================================
# REAL DECISIONS -- not hand-built fixtures
# ======================================================================

@pytest.fixture(scope="module")
def decisions() -> list[MatchDecision]:
    batch = load_batch(RAW_DIR)
    return decide_batch(run_matching(normalize_batch(batch).records))


@pytest.fixture(scope="module")
def decision_with_amounts(decisions) -> MatchDecision:
    """
    A decision whose facts carry a specific settlement figure.

    `build_explanation_facts` sources claimed/expected amount from
    evidence["match_signals"]["amount_bank"], which is populated only
    when a bank record was matched. Selecting on that rather than
    assuming it keeps the test honest if the batch changes.
    """
    for decision in decisions:
        signals = (decision.evidence or {}).get("match_signals") or {}
        if (signals.get("amount_bank") or {}).get("bank_amount"):
            return decision
    pytest.fail("no decision in the batch carries a bank amount")


def _facts_of(decision: MatchDecision) -> tuple[str, tuple[str, ...], str]:
    signals = decision.evidence["match_signals"]["amount_bank"]
    return (
        decision.status.value,
        tuple(
            c.value for c in decision.reason_codes
            if c is not ExceptionCode.NONE
        ),
        signals["bank_amount"],
    )


def _faithful_text(decision: MatchDecision) -> str:
    """An explanation that carries every authoritative token forward."""
    status, reason_codes, amount = _facts_of(decision)
    expected = decision.evidence["match_signals"]["amount_bank"]["pg_expected_net"]
    reasons = ", ".join(reason_codes) or "no violated conditions"
    return (
        f"Transaction {decision.txn_id} was resolved as {status}. "
        f"Violated conditions: {reasons}. The bank credited {amount} "
        f"against an expected net settlement of {expected}, at "
        f"confidence {decision.confidence_score}."
    )


def _unfaithful_text(decision: MatchDecision) -> str:
    """
    The dangerous case: well-formed, confident, correctly structured
    prose that states the WRONG figure. Length bounds pass, tone is
    plausible, and only a fact check catches it.
    """
    status, reason_codes, _ = _facts_of(decision)
    reasons = ", ".join(reason_codes) or "no violated conditions"
    return (
        f"Transaction {decision.txn_id} was resolved as {status}. "
        f"Violated conditions: {reasons}. The bank credited "
        f"{FABRICATED_AMOUNT} against an expected net settlement of "
        f"{FABRICATED_AMOUNT}, at confidence "
        f"{decision.confidence_score}."
    )


def _agent(text: str) -> FinanceControllerAgent:
    return FinanceControllerAgent(lambda _prompt: text)


# ======================================================================
# THE DEFECT, CLOSED
# ======================================================================

def test_runtime_rejects_an_unfaithful_explanation(decision_with_amounts):
    """
    THE TEST THE TASK ASKED FOR.

    A well-formed explanation stating a figure the deterministic
    engine never produced must not reach the operator.
    """
    decision = decision_with_amounts
    before = copy.deepcopy(decision)

    response = _agent(_unfaithful_text(decision)).explain(decision)

    assert response.explanation_source == "deterministic_fallback", (
        "an explanation containing a fabricated settlement figure was "
        "returned to the operator as model output -- validate_fn is "
        "not enforcing faithfulness on the runtime path"
    )

    assert FABRICATED_AMOUNT not in response.explanation

    # Every financial field is byte-identical. explain() narrates; it
    # does not participate in the decision.
    assert decision.status == before.status
    assert decision.exception_code == before.exception_code
    assert decision.reason_codes == before.reason_codes
    assert decision.confidence_score == before.confidence_score
    assert decision.evidence == before.evidence
    assert decision.matched_sources == before.matched_sources
    assert decision.tax_verified == before.tax_verified

    # And the response reports them unchanged too.
    assert response.status == before.status.value
    assert response.exception_code == before.exception_code.value
    assert response.confidence_score == before.confidence_score


def test_a_faithful_explanation_is_still_accepted(decision_with_amounts):
    """
    THE CONTROL.

    A validator that rejects everything is not a validator, it is an
    outage. Without this test, replacing validate_fn with
    `lambda _: False` would pass the whole file.
    """
    decision = decision_with_amounts
    response = _agent(_faithful_text(decision)).explain(decision)

    assert response.explanation_source == "llm", (
        f"a faithful explanation was rejected: "
        f"{response.agent_metadata.get('llm_error')}"
    )
    assert response.agent_metadata["llm_error"] is None


def test_the_rejection_records_which_facts_were_missing(decision_with_amounts):
    """
    A rejection with no recorded reason is a silent failure.

    The operator sees the template; whoever debugs it needs to know
    the model dropped a specific figure rather than timing out.
    """
    decision = decision_with_amounts
    response = _agent(_unfaithful_text(decision)).explain(decision)

    error = response.agent_metadata["llm_error"]
    assert error is not None
    assert "faithfulness violations" in error

    _, _, amount = _facts_of(decision)
    assert f"missing_claimed_amount:{amount}" in error


# ======================================================================
# THE OTHER WAYS THE MODEL CAN BE WRONG
# ======================================================================

def test_dropping_the_status_is_rejected(decision_with_amounts):
    """
    Faithfulness is not only about numbers. An explanation that never
    names the outcome is not an explanation of it.
    """
    decision = decision_with_amounts
    text = _faithful_text(decision).replace(
        decision.status.value, "processed"
    )

    response = _agent(text).explain(decision)

    assert response.explanation_source == "deterministic_fallback"
    assert "missing_verified_status" in response.agent_metadata["llm_error"]


def test_dropping_a_reason_code_is_rejected(decisions):
    """
    A decision can violate several conditions at once. Narrating only
    the headline one tells the operator a true thing and hides a
    second true thing -- which is how a partial fix gets shipped.
    """
    decision = next(
        (d for d in decisions
         if len([c for c in d.reason_codes if c is not ExceptionCode.NONE]) >= 1),
        None,
    )
    assert decision is not None, "batch has no decision with a reason code"

    dropped = next(
        c.value for c in decision.reason_codes if c is not ExceptionCode.NONE
    )
    text = (
        f"Transaction {decision.txn_id} was resolved as "
        f"{decision.status.value} at confidence "
        f"{decision.confidence_score}. No further detail is available."
    )
    assert dropped not in text

    response = _agent(text).explain(decision)

    assert response.explanation_source == "deterministic_fallback"
    assert (
        f"missing_reason_code:{dropped}"
        in response.agent_metadata["llm_error"]
    )


def test_a_clean_match_needs_no_reason_code(decisions):
    """
    ExceptionCode.NONE is dropped from the fact pack deliberately.

    If it were not, every clean match would require the literal word
    "NONE" in prose -- an assertion about vocabulary, not about
    faithfulness, and one that would make the fallback the normal path
    for the largest group of records in the batch.
    """
    decision = next(
        (d for d in decisions if d.status.value == "MATCHED"), None
    )
    assert decision is not None, "batch has no MATCHED decision"

    signals = (decision.evidence or {}).get("match_signals") or {}
    amounts = signals.get("amount_bank") or {}
    text = (
        f"Transaction {decision.txn_id} is MATCHED. The bank credited "
        f"{amounts.get('bank_amount')} against an expected net of "
        f"{amounts.get('pg_expected_net')}."
    )

    response = _agent(text).explain(decision)

    assert response.explanation_source == "llm", (
        f"a clean match was rejected: "
        f"{response.agent_metadata.get('llm_error')}"
    )


# ======================================================================
# THE OTHER THREE FALLBACKS STILL BEHAVE
# ======================================================================

def test_a_provider_failure_still_falls_back(decision_with_amounts):
    """Wiring a validator in must not displace the existing paths."""
    def _broken(_prompt: str) -> str:
        raise ConnectionError("simulated provider outage")

    response = FinanceControllerAgent(_broken).explain(decision_with_amounts)

    assert response.explanation_source == "deterministic_fallback"
    assert "LLM call failed" in response.agent_metadata["llm_error"]
    # A transport failure is not a faithfulness failure, and the error
    # must not claim it was one.
    assert "faithfulness violations" not in response.agent_metadata["llm_error"]


def test_malformed_output_still_falls_back(decision_with_amounts):
    """
    Explanation.__post_init__ enforces [20, 2000] characters, so a
    two-word answer fails in parse_fn, before the validator sees it.
    """
    response = _agent("no.").explain(decision_with_amounts)

    assert response.explanation_source == "deterministic_fallback"
    assert "Failed to parse LLM output" in response.agent_metadata["llm_error"]


def test_the_fallback_returns_the_same_contract_type(decision_with_amounts):
    """
    Callers must never branch on which path produced the text.
    """
    from src.agent.contracts import Explanation
    from src.agent.explainer import fallback_template_explanation

    fallback = fallback_template_explanation(decision_with_amounts)
    assert isinstance(fallback, Explanation)
    assert fallback.source == "deterministic_fallback"
    assert 20 <= len(fallback.text) <= 2000


# ======================================================================
# STRUCTURAL -- the wiring itself
# ======================================================================

def test_the_explainer_routes_through_the_real_validator():
    """
    The behavioural tests above would all pass again if someone
    reintroduced `validate_fn=lambda _: True` AND happened to write
    stubs that look unfaithful for other reasons. This asserts the
    shape directly: the runtime module imports the real validator and
    no longer defines a stub that returns a bare True.
    """
    source = (ROOT / "src" / "agent" / "explainer.py").read_text(
        encoding="utf-8"
    )

    assert "from src.agent.explanation_validator import validate_explanation" in source
    assert "validate_explanation(" in source

    code = [
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    ]
    for line in code:
        assert not re.match(r"\s*return True\s*$", line), (
            f"explainer.py contains an unconditional `return True`: "
            f"{line!r} -- if this is the guardrail's validate_fn again, "
            f"the faithfulness check has been disconnected"
        )
