# src/agent/explainer.py
"""
Bounded LLM use, case 2: generate a plain-English explanation of an
already-finished MatchDecision. Strictly READ-ONLY over evidence that
has already been computed by the deterministic pipeline -- this
module cannot alter status, exception_code, or any financial fact. It
can only narrate what already happened.

FAITHFULNESS IS ENFORCED HERE, NOT ONLY MEASURED OFFLINE
--------------------------------------------------------
Until this module was changed, `validate_fn` was:

    def _validate_explanation(value): return True

so `explain()` checked the LENGTH of the model's text and nothing
else. `validate_explanation()` -- a real faithfulness checker -- was
exercised by scripts/eval_explanation_quality.py and by
tests/test_explanation_validator.py, but the production path routed
around it. That is FAILURE_LOG.md section 20's shape: a contract that
exists, is tested, and is not on the path that runs. See section 62.

The validator now sits in the guardrail's validate_fn position. A
model explanation that does not carry the deterministic facts forward
is rejected, `call_llm_bounded` returns succeeded=False, and the
caller falls back to `fallback_template_explanation()`.

WHY THE PROMPT DEMANDS VERBATIM TOKENS
--------------------------------------
`validate_explanation()` is a containment check over normalized text:
it asks whether each authoritative fact APPEARS in the prose. It does
not paraphrase-match, and deliberately so -- a fuzzy matcher would
admit wrong numbers in order to avoid rejecting correct paraphrases.

That makes the contract achievable only if the model is told to quote
the tokens exactly. DecisionStatus.TAX_MISMATCH renders as
"TAX_MISMATCH"; a model writing the natural phrase "tax mismatch"
fails containment on the underscore. The fix is to state the
requirement in the prompt, NOT to loosen the check -- loosening it is
how a fabricated figure gets through.
"""

from __future__ import annotations

from src.models import ExceptionCode, MatchDecision
from src.agent.guardrails import call_llm_bounded, AgentCallResult
from src.agent.contracts import Explanation
from src.agent.explanation_contracts import (
    ExplanationFacts,
    ExplanationResponse,
)
from src.agent.explanation_validator import validate_explanation


# ======================================================================
# DETERMINISTIC FACTS
# ======================================================================

def _quotable(value) -> str | None:
    """
    Accept a fact only if it is a non-empty string.

    ExplanationFacts declares every financial field as `str | None`,
    and the validator's containment check needs something quotable. A
    None, an empty string, or a stray non-string is "no fact stated"
    rather than "a fact the model must repeat" -- asserting a fact we
    cannot source is how an unfalsifiable check gets written.
    """
    if isinstance(value, str) and value.strip():
        return value
    return None


def build_explanation_facts(decision: MatchDecision) -> ExplanationFacts:
    """
    Project a finished MatchDecision onto the read-only fact contract.

    Field for field this mirrors how scripts/eval_explanation_quality.py
    builds ExplanationFacts from its frozen dataset, so the runtime
    check and the offline evaluation ask the same question.

    Two mapping notes, both deliberate:

    `reason_codes` drops ExceptionCode.NONE. The evaluation dataset
    represents a clean match as an empty list, and requiring the word
    "NONE" to appear in prose would be an assertion about vocabulary,
    not about faithfulness.

    `evidence` is left empty. MatchDecision.evidence is a STRUCTURED
    dict -- matched_rule, context, match_signals, selection_reason --
    not the short quotable phrases ("GST mismatch") the dataset
    carries. Feeding "gst_mismatch_takes_priority_over_tds" into a
    containment check would reject every readable explanation. The
    amounts below are the part of that dict that IS quotable, and they
    are the part a fabrication would falsify.
    """
    evidence = decision.evidence or {}
    signals = evidence.get("match_signals") or {}
    amount_bank = signals.get("amount_bank") or {}

    return ExplanationFacts(
        status=decision.status.value,
        reason_codes=tuple(
            code.value
            for code in decision.reason_codes
            if code is not ExceptionCode.NONE
        ),
        confidence_score=float(decision.confidence_score),
        evidence=(),
        claimed_amount=_quotable(amount_bank.get("bank_amount")),
        expected_amount=_quotable(amount_bank.get("pg_expected_net")),
        # MatchDecision does not carry claimed/expected GST or TDS.
        # TaxVerification computes them; decide_batch does not persist
        # them into evidence. Stated as absent rather than guessed --
        # see FAILURE_LOG.md section 62.
        claimed_tax=None,
        expected_tax=None,
    )


# ======================================================================
# PROMPT
# ======================================================================

def _build_prompt(decision: MatchDecision) -> str:
    facts = build_explanation_facts(decision)

    required: list[str] = [facts.status, *facts.reason_codes]
    for value in (facts.claimed_amount, facts.expected_amount):
        if value is not None and value not in required:
            required.append(value)

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
        "\n"
        "REQUIRED TOKENS -- a deterministic validator checks that each "
        "of the following appears in your answer EXACTLY as written, "
        "underscores and decimal places included. Quote them verbatim; "
        "you may add a plain-English gloss alongside each one. An "
        "explanation missing any of them is discarded and replaced "
        "with a template, so paraphrasing loses your work.\n"
        f"{required}\n"
    )


def _parse_response(raw: str) -> Explanation:
    # grounded_evidence_keys is populated from the evidence dict's
    # own keys -- a lightweight, honest signal of what was available
    # to ground the explanation, not a claim the model actually used
    # each one.
    return Explanation(text=raw.strip(), source="llm")


# ======================================================================
# THE CALL
# ======================================================================

def explain_decision_via_llm(
    decision: MatchDecision, llm_call_fn
) -> AgentCallResult[Explanation]:
    """
    Generate an explanation, rejecting it if it is not faithful.

    The violation list is carried out through `error`. `validate_fn`
    can only return a bool, and guardrails.py is shared with the ask()
    path, so the violations are captured in a closure cell and spliced
    onto the guardrail's generic message here. A rejection whose reason
    is not recorded is a silent failure, which is the category this
    project exists to avoid.
    """
    facts = build_explanation_facts(decision)
    captured: list[str] = []

    def _validate(value: Explanation) -> bool:
        ok, violations = validate_explanation(
            facts, ExplanationResponse(explanation=value.text)
        )
        captured.clear()
        captured.extend(violations)
        return ok

    result = call_llm_bounded(
        call_fn=lambda: llm_call_fn(_build_prompt(decision)),
        parse_fn=_parse_response,
        validate_fn=_validate,
    )

    if not result.succeeded and captured:
        result.error = f"{result.error} -- faithfulness violations: {captured}"

    return result


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
