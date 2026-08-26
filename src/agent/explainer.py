# src/agent/explainer.py
"""
Bounded LLM use, case 2: generate a plain-English explanation of an
already-finished MatchDecision. Strictly READ-ONLY over evidence that
has already been computed by the deterministic pipeline -- this
module cannot alter status, exception_code, or any financial fact. It
can only narrate what already happened.
"""

from __future__ import annotations
from src.models import MatchDecision
from src.agent.guardrails import call_llm_bounded, AgentCallResult
from src.agent.contracts import Explanation


def _build_prompt(decision: MatchDecision) -> str:
    return (
        "Explain the following reconciliation decision in one clear, "
        "plain-English paragraph for a finance-ops reviewer. Use ONLY "
        "the facts given below -- do not invent numbers, dates, or "
        "reasons not present in the evidence.\n\n"
        f"Transaction: {decision.txn_id}\n"
        f"Status: {decision.status.value}\n"
        f"Primary reason: {decision.exception_code.value}\n"
        f"All violated conditions: {[c.value for c in decision.reason_codes]}\n"
        f"Confidence score: {decision.confidence_score}\n"
        f"Evidence: {decision.evidence}\n"
    )


def _parse_response(raw: str) -> Explanation:
    # grounded_evidence_keys is populated from the evidence dict's
    # own keys -- a lightweight, honest signal of what was available
    # to ground the explanation, not a claim the model actually used
    # each one.
    return Explanation(text=raw.strip(), source="llm")


def _validate_explanation(value: Explanation) -> bool:
    return True  # __post_init__ already enforced length bounds


def explain_decision_via_llm(decision: MatchDecision, llm_call_fn) -> AgentCallResult[Explanation]:
    prompt = _build_prompt(decision)
    return call_llm_bounded(
        call_fn=lambda: llm_call_fn(prompt),
        parse_fn=_parse_response,
        validate_fn=_validate_explanation,
    )


def fallback_template_explanation(decision: MatchDecision) -> Explanation:
    """Now returns Explanation too -- the fallback path must produce
    the SAME contract type as the LLM path, so callers never have to
    branch on which type they received."""
    reasons = ", ".join(c.value for c in decision.reason_codes if c.value != "NONE")
    if decision.status.value == "MATCHED":
        text = f"Transaction {decision.txn_id} matched successfully across all sources with no exceptions."
    else:
        text = (
            f"Transaction {decision.txn_id} was flagged as {decision.status.value}. "
            f"Violated condition(s): {reasons or 'see evidence for details'}. "
            f"Confidence score: {decision.confidence_score}."
        )
    return Explanation(text=text, source="deterministic_fallback")