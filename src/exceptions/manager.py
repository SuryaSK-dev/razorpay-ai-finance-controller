# src/exceptions/manager.py
"""
Converts MatchResult + TaxVerification into a final MatchDecision by
consulting the explicit DECISION_TABLE in decision_table.py. This
module's job is building the DecisionContext (the fact set) and
translating the winning rule into a MatchDecision with COMPLETE
evidence -- including every violated condition, not just the single
condition that determined the final status.
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


def _build_context(match_result: MatchResult, seller_annual_gross: Optional[Decimal]) -> DecisionContext:
    no_candidates_found = match_result.bank_record is None and match_result.invoice_record is None
    is_ambiguous = match_result.is_ambiguous
    low_confidence = not is_auto_matchable(match_result.confidence) and not is_ambiguous

    missing_bank = match_result.bank_record is None
    missing_invoice = match_result.invoice_record is None

    gst_mismatch = False
    tds_mismatch = False
    tax_unverifiable = False

    if not no_candidates_found and not is_ambiguous and not low_confidence:
        tax = verify_tax(match_result.pg_record, match_result.invoice_record, seller_annual_gross)
        if match_result.invoice_record is not None:
            if seller_annual_gross is None:
                tax_unverifiable = True
            else:
                # Evaluated INDEPENDENTLY -- a record can have both a
                # GST problem AND a TDS problem simultaneously. Only
                # the decision table's priority order picks which one
                # determines `status`; both are captured in evidence
                # regardless, via reason_codes below.
                gst_mismatch = not tax.gst_verified
                tds_mismatch = not tax.tds_verified

    fully_clean = (
        not no_candidates_found and not is_ambiguous and not low_confidence
        and not missing_bank and not missing_invoice
        and not gst_mismatch and not tds_mismatch and not tax_unverifiable
    )

    return DecisionContext(
        no_candidates_found=no_candidates_found,
        is_ambiguous=is_ambiguous,
        low_confidence=low_confidence,
        missing_bank=missing_bank,
        missing_invoice=missing_invoice,
        gst_mismatch=gst_mismatch,
        tds_mismatch=tds_mismatch,
        tax_unverifiable=tax_unverifiable,
        fully_clean=fully_clean,
    )


def _all_violated_codes(context: DecisionContext) -> list[ExceptionCode]:
    """Every violated condition present in this transaction, not just
    the single code tied to the winning decision rule. A record with
    BOTH a GST problem and a TDS problem should show both in evidence,
    even though `status` and the primary `exception_code` reflect only
    the highest-priority one."""
    codes = []
    if context.no_candidates_found:
        codes.append(ExceptionCode.HUMAN_REVIEW_REQUIRED) 
    if context.is_ambiguous:
        codes.append(ExceptionCode.AMBIGUOUS_MATCH)
    if context.gst_mismatch:
        codes.append(ExceptionCode.ERR_GST_MISMATCH)
    if context.tds_mismatch:
        codes.append(ExceptionCode.ERR_TDS_VARIANCE)
    if context.missing_bank:
        codes.append(ExceptionCode.MISSING_IN_BANK)
    if context.missing_invoice:
        codes.append(ExceptionCode.MISSING_IN_INVOICE)
    if context.tax_unverifiable:
        codes.append(ExceptionCode.HUMAN_REVIEW_REQUIRED)
    seen = set()
    deduped = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped or [ExceptionCode.NONE]


def decide(match_result: MatchResult, seller_annual_gross: Optional[Decimal] = None) -> MatchDecision:
    context = _build_context(match_result, seller_annual_gross)
    rule = evaluate(context)

    return MatchDecision(
        txn_id=match_result.txn_id,
        status=rule.status,
        confidence_score=match_result.score.total_score,
        matched_sources=match_result.sources_present,
        tax_verified=(not context.gst_mismatch and not context.tds_mismatch
                      and not context.tax_unverifiable) if not context.no_candidates_found else None,
        exception_code=rule.exception_code,
        reason_codes=_all_violated_codes(context),
        evidence={
            "matched_rule": rule.name,
            "context": context.__dict__,
            "match_signals": match_result.score.signals,
            "selection_reason": match_result.selection_reason,
        },
    )


def decide_batch(match_results: list[MatchResult]) -> list[MatchDecision]:
    cumulative_gross_by_txn = build_seller_annual_gross(match_results)
    decisions = []
    for result in match_results:
        seller_gross = cumulative_gross_by_txn.get(result.txn_id)
        decisions.append(decide(result, seller_gross))
    return decisions
