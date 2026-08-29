# tests/test_tax_verified_states.py
"""
Regression tests for FIX (C4) -- tax_verified is a THREE-state field.

    True  -- verify_tax() ran and every control passed
    False -- verify_tax() ran and at least one control failed
    None  -- verify_tax() never ran; nothing is claimed

The bug
-------
Before C4, only `no_candidates_found` produced tax_verified=None.
Every other non-evaluated path -- missing invoice, ambiguous identity,
duplicate identity -- left gst_mismatch/tds_mismatch/tax_unverifiable
at their False defaults, and tax_verified was derived purely from
those flags:

    tax_verified = (not gst_mismatch
                    and not tds_mismatch
                    and not tax_unverifiable)

So "no mismatch was recorded" silently became "tax was checked and is
correct". A PARTIAL_MATCH transaction with no invoice at all reported
tax_verified=True.

Why it matters
--------------
This is a fail-open in the REPORTING layer, not the decision layer.
The DecisionStatus was always correct -- these tests assert that too,
to prove C4 did not disturb the decision table. But tax_verified feeds
the explanation contracts, so the agent could have told a finance
operator that tax is verified on a transaction that has no invoice to
verify against.

These tests exercise the decision manager directly rather than the
full batch, so they remain valid regardless of what any particular
generated dataset happens to contain.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.models import NormalizedRecord, DecisionStatus, ExceptionCode
from src.matching.engine import MatchResult
from src.matching.scoring import score_candidate, classify_confidence
from src.exceptions.manager import decide


# ======================================================================
# FIXTURES
# ======================================================================

def make_normalized(
    txn_id: str,
    source: str,
    amount: str = "1000.00",
    fee: str | None = None,
    gst: str | None = None,
    tds: str | None = None,
    raw_ref: dict | None = None,
) -> NormalizedRecord:
    return NormalizedRecord(
        txn_id=txn_id,
        source=source,
        utr=f"UTR{txn_id}",
        amount=Decimal(amount),
        fee=Decimal(fee) if fee is not None else None,
        gst=Decimal(gst) if gst is not None else None,
        tds=Decimal(tds) if tds is not None else None,
        date_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
        raw_ref=raw_ref or {"merchant_id": "M1"},
    )


def build_result(
    txn_id: str,
    pg: NormalizedRecord,
    bank: NormalizedRecord | None,
    invoice: NormalizedRecord | None,
    *,
    is_ambiguous: bool = False,
    duplicate_detected: bool = False,
) -> MatchResult:
    """
    Construct a MatchResult with real scoring, so confidence is never
    fabricated. Identity flags are set explicitly because these tests
    are about the decision manager's handling of them, not about
    whether the matching engine detects them.
    """
    score = score_candidate(pg, bank, invoice)
    confidence = classify_confidence(score)

    sources = ["pg"]
    if bank is not None:
        sources.append("bank")
    if invoice is not None:
        sources.append("invoice")

    return MatchResult(
        txn_id=txn_id,
        pg_record=pg,
        bank_record=bank,
        invoice_record=invoice,
        score=score,
        confidence=confidence,
        sources_present=sources,
        is_ambiguous=is_ambiguous,
        duplicate_detected=duplicate_detected,
    )


def clean_triplet(txn_id: str):
    """A fully reconciling PG/bank/invoice triplet."""
    pg = make_normalized(
        txn_id, "pg", amount="1000.00", fee="20.00", gst="3.60",
    )
    bank = make_normalized(txn_id, "bank", amount="976.40")
    invoice = make_normalized(
        txn_id, "invoice", amount="23.60",
        fee="20.00", gst="3.60", tds="0.00",
    )
    return pg, bank, invoice


# ======================================================================
# THE BUG: tax_verified must be None when tax never ran
# ======================================================================

def test_missing_invoice_reports_tax_not_evaluated():
    """
    THE REGRESSION. Before C4 this returned tax_verified=True on a
    transaction with no invoice -- asserting tax was checked and passed
    when verify_tax() was never called.
    """
    pg, bank, _ = clean_triplet("TXN_NOINV")

    decision = decide(
        build_result("TXN_NOINV", pg, bank, None),
        seller_annual_gross=Decimal("100000"),
    )

    assert decision.tax_verified is None, (
        "No invoice means tax was never verified. True is a false "
        "claim; False would wrongly imply a detected violation."
    )
    assert decision.evidence["tax_evaluated"] is False


def test_ambiguous_identity_reports_tax_not_evaluated():
    pg, bank, invoice = clean_triplet("TXN_AMB")

    decision = decide(
        build_result("TXN_AMB", pg, bank, invoice, is_ambiguous=True),
        seller_annual_gross=Decimal("100000"),
    )

    assert decision.tax_verified is None
    assert decision.evidence["tax_evaluated"] is False


def test_duplicate_identity_reports_tax_not_evaluated():
    pg, bank, invoice = clean_triplet("TXN_DUP")

    decision = decide(
        build_result(
            "TXN_DUP", pg, bank, invoice, duplicate_detected=True,
        ),
        seller_annual_gross=Decimal("100000"),
    )

    assert decision.tax_verified is None
    assert decision.evidence["tax_evaluated"] is False


def test_no_candidates_reports_tax_not_evaluated():
    """Pre-existing correct behaviour -- must not regress."""
    pg = make_normalized("TXN_NONE", "pg", amount="500.00")

    decision = decide(build_result("TXN_NONE", pg, None, None))

    assert decision.tax_verified is None
    assert decision.evidence["tax_evaluated"] is False


# ======================================================================
# tax_verified must still be a real boolean when tax DID run
# ======================================================================

def test_clean_transaction_reports_tax_verified_true():
    pg, bank, invoice = clean_triplet("TXN_CLEAN")

    decision = decide(
        build_result("TXN_CLEAN", pg, bank, invoice),
        seller_annual_gross=Decimal("100000"),
    )

    assert decision.tax_verified is True
    assert decision.evidence["tax_evaluated"] is True


def test_gst_mismatch_reports_tax_verified_false():
    """
    False and None must remain distinguishable: this transaction WAS
    checked and FAILED, which is a different fact from never checked.
    """
    pg, bank, _ = clean_triplet("TXN_BADGST")
    invoice = make_normalized(
        "TXN_BADGST", "invoice", amount="23.60",
        fee="20.00", gst="2.40", tds="0.00",   # 12% slab, not 18%
    )

    decision = decide(
        build_result("TXN_BADGST", pg, bank, invoice),
        seller_annual_gross=Decimal("100000"),
    )

    assert decision.tax_verified is False
    assert decision.evidence["tax_evaluated"] is True


def test_unknown_seller_gross_reports_tax_verified_false():
    """
    tax_unverifiable is NOT the same as tax-not-evaluated. Here
    verify_tax() ran but could not complete the TDS threshold decision,
    which is a recorded finding, not an absence of one.
    """
    pg, bank, invoice = clean_triplet("TXN_NOLEDGER")

    decision = decide(
        build_result("TXN_NOLEDGER", pg, bank, invoice),
        seller_annual_gross=None,
    )

    assert decision.tax_verified is False
    assert decision.evidence["tax_evaluated"] is True


# ======================================================================
# C4 must not have disturbed the decision layer
# ======================================================================

def test_c4_did_not_change_decision_status():
    """
    tax_verified is a reporting field. Fixing it must not alter any
    DecisionStatus -- otherwise C4 quietly became a policy change.
    """
    pg, bank, invoice = clean_triplet("TXN_S1")

    clean = decide(
        build_result("TXN_S1", pg, bank, invoice),
        seller_annual_gross=Decimal("100000"),
    )
    assert clean.status == DecisionStatus.MATCHED

    pg2, bank2, _ = clean_triplet("TXN_S2")
    no_invoice = decide(
        build_result("TXN_S2", pg2, bank2, None),
        seller_annual_gross=Decimal("100000"),
    )
    assert no_invoice.status == DecisionStatus.PARTIAL_MATCH
    assert (
        ExceptionCode.MISSING_IN_INVOICE
        in no_invoice.reason_codes
    )


def test_reason_codes_still_preserve_every_violation():
    """
    Multi-violation preservation is a separate guarantee from
    tax_verified and must survive C4 untouched.
    """
    pg, bank, _ = clean_triplet("TXN_MULTI")
    invoice = make_normalized(
        "TXN_MULTI", "invoice", amount="23.60",
        fee="20.00", gst="2.40", tds="0.00",
    )

    decision = decide(
        build_result("TXN_MULTI", pg, bank, invoice),
        seller_annual_gross=Decimal("600000"),   # TDS applies
    )

    assert ExceptionCode.ERR_GST_MISMATCH in decision.reason_codes
    assert ExceptionCode.ERR_TDS_VARIANCE in decision.reason_codes


# ======================================================================
# Invariant: the three states are exhaustive and never ambiguous
# ======================================================================

def test_tax_evaluated_evidence_always_agrees_with_tax_verified():
    """
    Cross-check the audit trail against the reported value. If
    tax_evaluated is False, tax_verified MUST be None, and vice versa.
    Any drift between the gate and the field is a bug.
    """
    cases = []

    pg, bank, invoice = clean_triplet("TXN_X1")
    cases.append(build_result("TXN_X1", pg, bank, invoice))

    pg, bank, _ = clean_triplet("TXN_X2")
    cases.append(build_result("TXN_X2", pg, bank, None))

    pg, bank, invoice = clean_triplet("TXN_X3")
    cases.append(
        build_result("TXN_X3", pg, bank, invoice, is_ambiguous=True)
    )

    pg, bank, invoice = clean_triplet("TXN_X4")
    cases.append(
        build_result(
            "TXN_X4", pg, bank, invoice, duplicate_detected=True,
        )
    )

    pg = make_normalized("TXN_X5", "pg", amount="500.00")
    cases.append(build_result("TXN_X5", pg, None, None))

    for result in cases:
        decision = decide(
            result, seller_annual_gross=Decimal("100000"),
        )

        evaluated = decision.evidence["tax_evaluated"]

        if evaluated:
            assert decision.tax_verified in (True, False), (
                f"{decision.txn_id}: tax was evaluated, so "
                "tax_verified must be a real boolean"
            )
        else:
            assert decision.tax_verified is None, (
                f"{decision.txn_id}: tax was NOT evaluated, so "
                "tax_verified must be None"
            )


def main() -> None:
    test_missing_invoice_reports_tax_not_evaluated()
    test_ambiguous_identity_reports_tax_not_evaluated()
    test_duplicate_identity_reports_tax_not_evaluated()
    test_no_candidates_reports_tax_not_evaluated()
    test_clean_transaction_reports_tax_verified_true()
    test_gst_mismatch_reports_tax_verified_false()
    test_unknown_seller_gross_reports_tax_verified_false()
    test_c4_did_not_change_decision_status()
    test_reason_codes_still_preserve_every_violation()
    test_tax_evaluated_evidence_always_agrees_with_tax_verified()

    print("C4 tax_verified three-state tests passed.")


if __name__ == "__main__":
    main()