# src/exceptions/manager.py
"""
Deterministic decision manager for the reconciliation engine.

Responsibilities
----------------
1. Convert a MatchResult into a complete DecisionContext.
2. Perform deterministic financial-control checks only when the
   underlying source relationship is sufficiently resolvable.
3. Evaluate the explicit DECISION_TABLE.
4. Produce a fully auditable MatchDecision.

Architecture
------------
Matching authority belongs to the matching engine.

This module does NOT create a second matching algorithm and does not
reconstruct candidate authority from individual fields.

The matching engine provides:
    - selected candidates
    - confidence
    - ambiguity state
    - duplicate state
    - source presence

The decision manager then applies deterministic financial controls:

    MatchResult
        |
        +--> source / identity state
        |
        +--> confidence gate
        |
        +--> settlement amount control
        |
        +--> tax verification
        |
        +--> DECISION_TABLE
        |
        v
    MatchDecision

No LLM output participates in the financial decision path.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from src.models import MatchDecision, ExceptionCode
from src.matching.engine import MatchResult
from src.matching.scoring import is_auto_matchable
from src.tax.validator import verify_tax
from src.tax.seller_ledger import build_seller_annual_gross
from src.exceptions.decision_table import DecisionContext, evaluate


_MONEY_TOLERANCE = Decimal("0.01")


def _build_context(
    match_result: MatchResult,
    seller_annual_gross: Optional[Decimal],
) -> DecisionContext:
    """
    Build the complete deterministic fact set consumed by the
    decision policy.

    Important design rule
    ---------------------
    `MatchResult` is the output of the matching engine.

    The decision manager must NOT invent another matching authority
    layer on top of it.

    `is_auto_matchable(match_result.confidence)` is the deterministic
    confidence gate for downstream controls.

    `is_ambiguous` and `duplicate_detected` are hard identity
    guardrails and always take precedence in the decision table.

    `authoritative_match`, when present on MatchResult, is retained as
    audit evidence but is NOT used as a second independent gate here.
    """

    # ------------------------------------------------------------------
    # SOURCE PRESENCE
    # ------------------------------------------------------------------

    missing_bank = match_result.bank_record is None
    missing_invoice = match_result.invoice_record is None

    no_candidates_found = (
        missing_bank
        and missing_invoice
    )

    # ------------------------------------------------------------------
    # MATCHING STATE
    # ------------------------------------------------------------------

    is_ambiguous = match_result.is_ambiguous
    duplicate_detected = match_result.duplicate_detected

    # Confidence is meaningful only after excluding explicit identity
    # and source-state exceptions.
    #
    # A weak candidate must never silently become MATCHED.
    #
    # Missing-bank / missing-invoice cases have dedicated policy rules,
    # so they should not be relabeled as generic REFERENCE_MISMATCH.
    low_confidence = (
        not no_candidates_found
        and not missing_bank
        and not missing_invoice
        and not is_ambiguous
        and not duplicate_detected
        and not is_auto_matchable(match_result.confidence)
    )

    # ------------------------------------------------------------------
    # SETTLEMENT AMOUNT CONTROL
    # ------------------------------------------------------------------
    #
    # Financial comparison is valid only when there is an actual bank
    # record and the selected relationship is sufficiently resolvable.
    #
    # Expected settlement:
    #
    #     PG gross
    #       - PG fee
    #       - GST on PG fee
    #       - TDS withheld
    #
    # This is compared against the bank credited amount.
    #
    # Never compare PG gross directly with bank net settlement.
    #
    # Never classify missing/ambiguous/duplicate/weak matches as an
    # amount mismatch merely because a candidate record exists.
    # ------------------------------------------------------------------

    amount_mismatch = False

    # ------------------------------------------------------------------
    # FIX (C3): amount reconciliation must be independent of confidence
    # classification.
    #
    # `low_confidence` is itself partly derived from the amount-match
    # signal inside scoring.py (SCORE_AMOUNT_BANK / SCORE_AMOUNT_INVOICE
    # contribute to normalized_score). Gating the amount control on
    # `not low_confidence` created a circular suppression: an amount
    # discrepancy could drag confidence down and then use that same
    # drop to skip the very check that would have reported it --
    # silently downgrading a genuine AMOUNT_MISMATCH into a generic
    # LOW_CONFIDENCE / REFERENCE_MISMATCH classification.
    #
    # Identity (is this the right candidate at all) and financial
    # correctness (does the amount reconcile) are different concerns.
    # A selected candidate only needs unambiguous, non-duplicate,
    # present-source identity for the amount comparison to be
    # meaningful -- confidence tier is not a precondition for it.
    # ------------------------------------------------------------------

    amount_control_evaluable = (
        not no_candidates_found
        and not missing_bank
        and not is_ambiguous
        and not duplicate_detected
        and match_result.bank_record is not None
    )

    if amount_control_evaluable:
        pg = match_result.pg_record
        bank = match_result.bank_record

        if pg is not None and bank is not None:
            expected_net = (
                pg.amount
                - (pg.fee or Decimal("0"))
                - (pg.gst or Decimal("0"))
                - (pg.tds or Decimal("0"))
            )

            amount_mismatch = (
                abs(bank.amount - expected_net)
                > _MONEY_TOLERANCE
            )

    # ------------------------------------------------------------------
    # TAX VERIFICATION
    # ------------------------------------------------------------------
    #
    # Tax verification requires:
    #
    #   - a usable transaction
    #   - no ambiguity
    #   - no duplicate identity
    #   - invoice present
    #
    # We deliberately do NOT require a second `authoritative_match`
    # boolean. The matching engine's confidence/ambiguity contract is
    # already the authority boundary for this deterministic stage.
    #
    # This is important for isolated Phase 4 fixtures as well as the
    # production pipeline: a valid HIGH/MEDIUM deterministic match must
    # be capable of reaching tax verification.
    # ------------------------------------------------------------------

    gst_mismatch = False
    tds_mismatch = False
    tax_unverifiable = False

    # FIX (C3): same reasoning as the amount control above -- tax
    # verification must not be hidden behind a confidence tier that is
    # itself partly derived from the same financial signals tax
    # verification independently re-checks.
    tax_evaluable = (
        not no_candidates_found
        and not is_ambiguous
        and not duplicate_detected
        and not missing_invoice
        and match_result.invoice_record is not None
    )

    if tax_evaluable:
        tax = verify_tax(
            match_result.pg_record,
            match_result.invoice_record,
            seller_annual_gross,
        )

        if seller_annual_gross is None:
            tax_unverifiable = True
        else:
            # GST and TDS are independent controls.
            #
            # Both may fail simultaneously.
            #
            # The decision table chooses the PRIMARY exception.
            # _all_violated_codes() preserves every violation for
            # auditability.
            gst_mismatch = not tax.gst_verified
            tds_mismatch = not tax.tds_verified

    # ------------------------------------------------------------------
    # FULLY CLEAN STATE
    # ------------------------------------------------------------------
    #
    # A transaction is fully clean only when:
    #
    #   - candidates exist
    #   - identity is unambiguous
    #   - no duplicate exists
    #   - confidence is sufficient
    #   - bank exists
    #   - invoice exists
    #   - settlement amount reconciles
    #   - GST is valid
    #   - TDS is valid
    #   - tax is verifiable
    #
    # `fully_clean` is deliberately derived from the same deterministic
    # facts that the decision table consumes.
    # ------------------------------------------------------------------

    fully_clean = (
        not no_candidates_found
        and not is_ambiguous
        and not duplicate_detected
        and not low_confidence
        and not missing_bank
        and not missing_invoice
        and not amount_mismatch
        and not gst_mismatch
        and not tds_mismatch
        and not tax_unverifiable
    )

    return DecisionContext(
        no_candidates_found=no_candidates_found,
        is_ambiguous=is_ambiguous,
        duplicate_detected=duplicate_detected,
        low_confidence=low_confidence,
        missing_bank=missing_bank,
        missing_invoice=missing_invoice,
        amount_mismatch=amount_mismatch,
        gst_mismatch=gst_mismatch,
        tds_mismatch=tds_mismatch,
        tax_unverifiable=tax_unverifiable,
        fully_clean=fully_clean,
    )


def _all_violated_codes(
    context: DecisionContext,
) -> list[ExceptionCode]:
    """
    Preserve every deterministic violation present in the transaction.

    The primary exception is selected by DECISION_TABLE.

    reason_codes preserve the complete deterministic violation set for
    auditability and downstream explanation.

    Example:

        GST mismatch + TDS mismatch

    may produce:

        exception_code = ERR_GST_MISMATCH

    while:

        reason_codes = [
            ERR_GST_MISMATCH,
            ERR_TDS_VARIANCE,
        ]
    """

    codes: list[ExceptionCode] = []

    if context.no_candidates_found:
        codes.append(
            ExceptionCode.HUMAN_REVIEW_REQUIRED
        )

    if context.duplicate_detected:
        codes.append(
            ExceptionCode.DUPLICATE_DETECTED
        )

    if context.is_ambiguous:
        codes.append(
            ExceptionCode.AMBIGUOUS_MATCH
        )

    if context.amount_mismatch:
        codes.append(
            ExceptionCode.AMOUNT_MISMATCH
        )

    if context.gst_mismatch:
        codes.append(
            ExceptionCode.ERR_GST_MISMATCH
        )

    if context.tds_mismatch:
        codes.append(
            ExceptionCode.ERR_TDS_VARIANCE
        )

    if context.missing_bank:
        codes.append(
            ExceptionCode.MISSING_IN_BANK
        )

    if context.missing_invoice:
        codes.append(
            ExceptionCode.MISSING_IN_INVOICE
        )

    if context.tax_unverifiable:
        codes.append(
            ExceptionCode.HUMAN_REVIEW_REQUIRED
        )

    # Preserve deterministic insertion order while removing duplicates.
    seen: set[ExceptionCode] = set()
    deduped: list[ExceptionCode] = []

    for code in codes:
        if code not in seen:
            seen.add(code)
            deduped.append(code)

    return deduped or [ExceptionCode.NONE]


def decide(
    match_result: MatchResult,
    seller_annual_gross: Optional[Decimal] = None,
) -> MatchDecision:
    """
    Convert one deterministic MatchResult into the final MatchDecision.

    Decision authority:

        MatchResult
            +
        deterministic financial controls
            +
        DECISION_TABLE

    No AI-generated explanation, recommendation, or classification
    participates in the financial decision.
    """

    context = _build_context(
        match_result,
        seller_annual_gross,
    )

    rule = evaluate(context)

    # A normal deterministic MatchResult must have a score.
    # Never invent a confidence value.
    if match_result.score is None:
        raise ValueError(
            f"MatchResult {match_result.txn_id} has no score; "
            "deterministic decision cannot be produced."
        )

    # ------------------------------------------------------------------
    # TAX VERIFIED OUTPUT
    # ------------------------------------------------------------------

    if context.no_candidates_found:
        tax_verified: Optional[bool] = None
    else:
        tax_verified = (
            not context.gst_mismatch
            and not context.tds_mismatch
            and not context.tax_unverifiable
        )

    # ------------------------------------------------------------------
    # FINAL DECISION
    # ------------------------------------------------------------------

    return MatchDecision(
        txn_id=match_result.txn_id,
        status=rule.status,
        confidence_score=match_result.score.total_score,
        matched_sources=match_result.sources_present,
        tax_verified=tax_verified,
        exception_code=rule.exception_code,
        reason_codes=_all_violated_codes(context),
        evidence={
            "matched_rule": rule.name,
            "context": context.__dict__,
            "match_signals": match_result.score.signals,
            "selection_reason": match_result.selection_reason,

            # Preserve the engine's authority signal as audit evidence
            # without using it as a second decision gate.
            "authoritative_match": getattr(
                match_result,
                "authoritative_match",
                None,
            ),
        },
    )


def decide_batch(
    match_results: list[MatchResult],
) -> list[MatchDecision]:
    """
    Produce deterministic decisions for the complete batch.

    Seller annual gross is calculated once for the batch and supplied
    transaction-by-transaction to deterministic tax verification.
    """

    cumulative_gross_by_txn = build_seller_annual_gross(
        match_results
    )

    decisions: list[MatchDecision] = []

    for result in match_results:
        seller_gross = cumulative_gross_by_txn.get(
            result.txn_id
        )

        decisions.append(
            decide(
                result,
                seller_gross,
            )
        )

    return decisions