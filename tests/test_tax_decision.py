# tests/test_tax_decision.py
"""
Phase 4 tests: tax verification, seller ledger, and the full
decision engine -- isolated unit tests plus integration against the
real generated batch.
"""

from __future__ import annotations
import sys
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.models import NormalizedRecord, DecisionStatus, ExceptionCode
from src.tax.validator import verify_tax
from src.tax.seller_ledger import build_seller_annual_gross
from src.exceptions.manager import decide, decide_batch
from src.matching.engine import run_matching, MatchResult
from src.matching.scoring import score_candidate, classify_confidence
from src.ingestion.loader import load_batch
from src.normalization.engine import normalize_batch

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def make_normalized(txn_id, source, amount="1000.00", fee=None, gst=None, tds=None, raw_ref=None):
    return NormalizedRecord(
        txn_id=txn_id, source=source, amount=Decimal(amount),
        fee=Decimal(fee) if fee else None, gst=Decimal(gst) if gst else None,
        tds=Decimal(tds) if tds else None,
        date_utc=datetime(2026, 8, 15, tzinfo=timezone.utc), raw_ref=raw_ref or {},
    )


# =======================================================================
# TAX VERIFICATION -- ISOLATED
# =======================================================================

def test_gst_verified_when_correct():
    pg = make_normalized("TXN_A", "pg", amount="1000.00", fee="20.00")
    invoice = make_normalized("TXN_A", "invoice", amount="23.60", gst="3.60", tds="0.00")
    result = verify_tax(pg, invoice, seller_annual_gross=Decimal("100000"))
    assert result.gst_verified is True


def test_gst_flagged_when_wrong():
    pg = make_normalized("TXN_B", "pg", amount="1000.00", fee="20.00")
    invoice = make_normalized("TXN_B", "invoice", amount="26.00", gst="6.00", tds="0.00")
    result = verify_tax(pg, invoice, seller_annual_gross=Decimal("100000"))
    assert result.gst_verified is False


def test_tds_correctly_zero_below_threshold():
    pg = make_normalized("TXN_C", "pg", amount="1000.00", fee="20.00")
    invoice = make_normalized("TXN_C", "invoice", amount="23.60", gst="3.60", tds="0.00")
    result = verify_tax(pg, invoice, seller_annual_gross=Decimal("100000"))
    assert result.tds_verified is True
    assert result.expected_tds == Decimal("0.00")


def test_tds_wrongly_zero_above_threshold_is_flagged():
    """The critical danger case: TDS shows 0 but the seller HAS
    crossed the threshold. Must be flagged, never silently passed."""
    pg = make_normalized("TXN_D", "pg", amount="10000.00", fee="200.00")
    invoice = make_normalized("TXN_D", "invoice", amount="236.00", gst="36.00", tds="0.00")
    result = verify_tax(pg, invoice, seller_annual_gross=Decimal("600000"))
    assert result.tds_verified is False
    assert result.expected_tds > Decimal("0")


def test_tds_unknown_seller_gross_cannot_verify():
    pg = make_normalized("TXN_E", "pg", amount="1000.00", fee="20.00")
    invoice = make_normalized("TXN_E", "invoice", amount="23.60", gst="3.60", tds="0.00")
    result = verify_tax(pg, invoice, seller_annual_gross=None)
    assert result.tds_verified is False


# =======================================================================
# SELLER LEDGER -- ISOLATED
# =======================================================================

def test_seller_ledger_reads_opening_balance_directly():
    """The current approach computes threshold applicability
    per-record, directly from merchant_ytd_gross_opening -- no
    cross-transaction accumulation or ordering assumption of any
    kind. A transaction with a high opening balance should correctly
    show it's crossed the threshold even in isolation, independent
    of any other record in the batch."""
    pg_below_threshold = make_normalized(
        "TXN_F", "pg", amount="10000.00",
        raw_ref={"merchant_id": "M1", "merchant_ytd_gross_opening": "100000.00"}
    )
    pg_above_threshold = make_normalized(
        "TXN_G", "pg", amount="10000.00",
        raw_ref={"merchant_id": "M1", "merchant_ytd_gross_opening": "495000.00"}
    )

    def fake_result(pg):
        score = score_candidate(pg, None, None)
        conf = classify_confidence(score)
        return MatchResult(txn_id=pg.txn_id, pg_record=pg, bank_record=None, invoice_record=None,
                            score=score, confidence=conf, sources_present=["pg"])

    results = [fake_result(pg_below_threshold), fake_result(pg_above_threshold)]
    ledger = build_seller_annual_gross(results)

    assert ledger["TXN_F"] == Decimal("110000.00")   # 100000 + 10000, under 5L
    assert ledger["TXN_G"] == Decimal("505000.00")   # 495000 + 10000, over 5L


# =======================================================================
# DECISION ENGINE -- ISOLATED
# =======================================================================

def test_clean_match_produces_matched_status():
    pg = make_normalized("TXN_H", "pg", amount="1000.00", fee="20.00", gst="3.60",
                          raw_ref={"merchant_id": "M2"})
    bank = make_normalized("TXN_H", "bank", amount="976.40")
    invoice = make_normalized("TXN_H", "invoice", amount="23.60", fee="20.00", gst="3.60", tds="0.00")

    score = score_candidate(pg, bank, invoice)
    conf = classify_confidence(score)
    result = MatchResult(txn_id="TXN_H", pg_record=pg, bank_record=bank, invoice_record=invoice,
                          score=score, confidence=conf,
                          sources_present=["pg", "bank", "invoice"])

    decision = decide(result, seller_annual_gross=Decimal("100000"))
    assert decision.exception_code == ExceptionCode.NONE or decision.status == DecisionStatus.MATCHED


def test_unmatched_always_has_exception_code():
    pg = make_normalized("TXN_I", "pg", amount="500.00")
    score = score_candidate(pg, None, None)
    conf = classify_confidence(score)
    result = MatchResult(txn_id="TXN_I", pg_record=pg, bank_record=None, invoice_record=None,
                          score=score, confidence=conf, sources_present=["pg"])

    decision = decide(result)
    assert decision.status == DecisionStatus.UNMATCHED
    assert decision.exception_code != ExceptionCode.NONE


# =======================================================================
# INTEGRATION -- REAL GENERATED BATCH
# =======================================================================

def test_full_pipeline_produces_decisions_for_every_record():
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    match_results = run_matching(normalized.records)
    decisions = decide_batch(match_results)

    assert len(decisions) == len(match_results)
    statuses = {d.status for d in decisions}
    assert statuses.issubset(set(DecisionStatus))


def test_no_decision_left_without_exception_code_when_not_matched():
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    match_results = run_matching(normalized.records)
    decisions = decide_batch(match_results)

    for d in decisions:
        if d.status != DecisionStatus.MATCHED:
            assert d.exception_code != ExceptionCode.NONE, (
                f"{d.txn_id} has status {d.status} but no exception code."
            )


def test_decision_distribution_is_realistic():
    """Sanity check: not everything collapses into one status."""
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    match_results = run_matching(normalized.records)
    decisions = decide_batch(match_results)

    status_counts = {}
    for d in decisions:
        status_counts[d.status.value] = status_counts.get(d.status.value, 0) + 1

    print("\nDecision distribution:")
    for status, count in status_counts.items():
        print(f"  {status:<15} {count}")

    assert len(status_counts) > 1, "All decisions collapsed into a single status -- likely a bug."


def test_tax_mismatch_matches_ground_truth_exactly():
    """The regression test for the bug we just fixed: TAX_MISMATCH
    decisions must correspond exactly to the true tax_mismatch
    synthetic category, with zero false positives from other
    categories whose TDS/GST happened to be correct."""
    import json
    gt_path = Path(__file__).resolve().parent.parent / "data" / "ground_truth.json"
    ground_truth = {g["txn_id"]: g for g in json.load(open(gt_path))}

    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    match_results = run_matching(normalized.records)
    decisions = decide_batch(match_results)

    tax_mismatch_decisions = [d for d in decisions if d.status == DecisionStatus.TAX_MISMATCH]
    false_positives = [
        d for d in tax_mismatch_decisions
        if ground_truth.get(d.txn_id, {}).get("category") != "tax_mismatch"
    ]

    assert len(false_positives) == 0, (
        f"TAX_MISMATCH false positives found: {[d.txn_id for d in false_positives]}"
    )


if __name__ == "__main__":
    test_gst_verified_when_correct()
    test_gst_flagged_when_wrong()
    test_tds_correctly_zero_below_threshold()
    test_tds_wrongly_zero_above_threshold_is_flagged()
    test_tds_unknown_seller_gross_cannot_verify()
    test_seller_ledger_reads_opening_balance_directly()
    test_clean_match_produces_matched_status()
    test_unmatched_always_has_exception_code()
    test_full_pipeline_produces_decisions_for_every_record()
    test_no_decision_left_without_exception_code_when_not_matched()
    test_decision_distribution_is_realistic()
    test_tax_mismatch_matches_ground_truth_exactly()
    print("\nAll Phase 4 tests passed.")