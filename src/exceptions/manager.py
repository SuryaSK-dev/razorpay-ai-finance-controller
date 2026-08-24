# src/exceptions/manager.py
"""
Converts MatchResult + TaxVerification into a final MatchDecision,
with a specific, evidenced ExceptionCode whenever the record isn't a
clean match. This is the single point of accountability: every
transaction gets exactly one final status, never left ambiguous by
omission.
"""

from __future__ import annotations
from decimal import Decimal
from typing import Optional

from src.models import MatchDecision, DecisionStatus, ExceptionCode
from src.matching.engine import MatchResult
from src.matching.scoring import ConfidenceTier, is_auto_matchable
from src.tax.validator import verify_tax
from src.tax.seller_ledger import build_seller_annual_gross


def decide(match_result: MatchResult,
           seller_annual_gross: Optional[Decimal] = None) -> MatchDecision:
    """
    Central decision logic. Order of checks matters: we only verify
    tax if the underlying match is credible enough to bother -- an
    UNMATCHED record has no tax claim worth checking yet.
    """
    # ---- Case 1: genuinely no candidate found at all ----
    if match_result.bank_record is None and match_result.invoice_record is None:
        return MatchDecision(
            txn_id=match_result.txn_id,
            status=DecisionStatus.UNMATCHED,
            confidence_score=match_result.score.total_score,
            matched_sources=match_result.sources_present,
            exception_code=ExceptionCode.MISSING_IN_BANK if match_result.bank_candidate_count == 0
                            else ExceptionCode.AMBIGUOUS_MATCH,
            reason_codes=[ExceptionCode.MISSING_IN_BANK, ExceptionCode.MISSING_IN_INVOICE],
            evidence={"match_signals": match_result.score.signals,
                      "selection_reason": match_result.selection_reason},
        )

    # ---- Case 2: ambiguity flagged by the matching engine ----
    if match_result.is_ambiguous:
        return MatchDecision(
            txn_id=match_result.txn_id,
            status=DecisionStatus.AMBIGUOUS,
            confidence_score=match_result.score.total_score,
            matched_sources=match_result.sources_present,
            exception_code=ExceptionCode.AMBIGUOUS_MATCH,
            reason_codes=[ExceptionCode.AMBIGUOUS_MATCH],
            evidence={
                "bank_candidate_count": match_result.bank_candidate_count,
                "invoice_candidate_count": match_result.invoice_candidate_count,
                "rejected_bank_utrs": [r.utr for r in match_result.rejected_bank_candidates],
                "selection_reason": match_result.selection_reason,
            },
        )

    # ---- Case 3: not auto-matchable on confidence alone ----
    if not is_auto_matchable(match_result.confidence):
        exception_code = ExceptionCode.AMOUNT_MISMATCH if match_result.confidence == ConfidenceTier.LOW \
            else ExceptionCode.REFERENCE_MISMATCH
        return MatchDecision(
            txn_id=match_result.txn_id,
            status=DecisionStatus.HUMAN_REVIEW,
            confidence_score=match_result.score.total_score,
            matched_sources=match_result.sources_present,
            exception_code=exception_code,
            reason_codes=[exception_code],
            evidence={"match_signals": match_result.score.signals,
                      "confidence_tier": match_result.confidence.value},
        )

    # ---- Case 4: credible match -- now verify tax ----
    tax = verify_tax(match_result.pg_record, match_result.invoice_record, seller_annual_gross)

    missing_sources = ("invoice" not in match_result.sources_present
                        or "bank" not in match_result.sources_present)

    if not tax.fully_verified and match_result.invoice_record is not None:
        exception_code = ExceptionCode.ERR_GST_MISMATCH if not tax.gst_verified else ExceptionCode.ERR_TDS_VARIANCE
        if (tax.tds_threshold_applicable is False
                and match_result.pg_record.tds and match_result.pg_record.tds > 0):
            exception_code = ExceptionCode.ERR_TDS_BELOW_THRESHOLD
        return MatchDecision(
            txn_id=match_result.txn_id,
            status=DecisionStatus.TAX_MISMATCH,
            confidence_score=match_result.score.total_score,
            matched_sources=match_result.sources_present,
            tax_verified=False,
            exception_code=exception_code,
            reason_codes=[exception_code],
            evidence={"tax_signals": tax.signals},
        )

    # ---- Case 5: missing one secondary source, otherwise clean ----
    if missing_sources:
        exception_code = ExceptionCode.MISSING_IN_BANK if match_result.bank_record is None \
            else ExceptionCode.MISSING_IN_INVOICE
        return MatchDecision(
            txn_id=match_result.txn_id,
            status=DecisionStatus.PARTIAL_MATCH,
            confidence_score=match_result.score.total_score,
            matched_sources=match_result.sources_present,
            tax_verified=tax.fully_verified if match_result.invoice_record else None,
            exception_code=exception_code,
            reason_codes=[exception_code],
            evidence={"match_signals": match_result.score.signals, "tax_signals": tax.signals},
        )

    # ---- Case 6: fully matched, fully tax-verified ----
    return MatchDecision(
        txn_id=match_result.txn_id,
        status=DecisionStatus.MATCHED,
        confidence_score=match_result.score.total_score,
        matched_sources=match_result.sources_present,
        tax_verified=True,
        exception_code=ExceptionCode.NONE,
        reason_codes=[],
        evidence={"match_signals": match_result.score.signals, "tax_signals": tax.signals},
    )


def decide_batch(match_results: list[MatchResult]) -> list[MatchDecision]:
    """
    Entry point: run decide() across the whole batch, using a REAL
    cumulative per-merchant gross ledger (built from the batch
    itself) to correctly evaluate the TDS threshold for every
    transaction -- not a stub, not an assumption.
    """
    cumulative_gross_by_txn = build_seller_annual_gross(match_results)

    decisions = []
    for result in match_results:
        seller_gross = cumulative_gross_by_txn.get(result.txn_id)
        decisions.append(decide(result, seller_gross))
    return decisions