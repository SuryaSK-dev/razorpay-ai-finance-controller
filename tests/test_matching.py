# tests/test_matching.py
"""
Phase 3 unit + integration tests: candidate generation, scoring, and
the matching engine orchestrator.

Two layers, deliberately:
  1. Isolated unit tests with hand-built fixtures -- exercise exact
     logic paths (UTR match, fuzzy fallback, ambiguity, tie-breaking)
     without depending on what the generator happened to produce.
  2. An integration test against the REAL generated batch -- confirms
     the whole pipeline actually recovers our synthetic anomaly
     categories (reference_mismatch_fuzzy, missing_in_source, etc.)
     end to end.

Run: python tests/test_matching.py
or:  python -m pytest tests/test_matching.py -v
"""

from __future__ import annotations
import sys
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.models import NormalizedRecord
from src.matching.candidates import (
    CandidateIndex,
    find_bank_candidates,
    find_invoice_candidates,
    generate_candidate_sets,
)
from src.matching.scoring import score_candidate, classify_confidence, ConfidenceTier
from src.matching.engine import run_matching, summarize, MatchResult

from src.ingestion.loader import load_batch
from src.normalization.engine import normalize_batch

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


# =======================================================================
# FIXTURE HELPERS
# =======================================================================

def make_normalized(
    txn_id: str | None,
    source: str,
    utr: str | None = None,
    amount: str = "1000.00",
    fee: str | None = None,
    gst: str | None = None,
    tds: str | None = None,
    date_utc: datetime | None = None,
    raw_ref: dict | None = None,
) -> NormalizedRecord:
    return NormalizedRecord(
        txn_id=txn_id,
        source=source,
        utr=utr,
        amount=Decimal(amount),
        fee=Decimal(fee) if fee else None,
        gst=Decimal(gst) if gst else None,
        tds=Decimal(tds) if tds else None,
        date_utc=date_utc or datetime(2026, 8, 15, tzinfo=timezone.utc),
        raw_ref=raw_ref or {},
    )


# =======================================================================
# CANDIDATE GENERATION -- ISOLATED UNIT TESTS
# =======================================================================

def test_exact_utr_match_found():
    pg = make_normalized("TXN_001", "pg", utr="UTR123", amount="976.40")
    bank = make_normalized("TXN_001", "bank", utr="UTR123", amount="976.40")
    index = CandidateIndex([bank], [])

    candidates, match_type, evidence = find_bank_candidates(pg, index)
    assert len(candidates) == 1
    assert match_type == "exact_utr"
    assert evidence["utr"] == "UTR123"


def test_fuzzy_fallback_recovers_corrupted_utr():
    """Fuzzy tier specifically: bank has NO resolved txn_id (simulating
    a real bank feed with no structured bank_ref) AND a corrupted UTR
    -- this forces the search past tiers 1 and 2, into the guarded
    fuzzy fallback."""
    pg = make_normalized(
        "TXN_002", "pg", utr="UTR123456789", amount="500.00",
        raw_ref={"utr": "UTR123456789"},
    )
    bank = make_normalized(
        None, "bank", utr="UTR123456780",  # txn_id=None, UTR corrupted
        amount="500.00",
        raw_ref={"narration": "NEFT CR UTR123456789 MERCH_001"},
    )
    index = CandidateIndex([bank], [])

    candidates, match_type, evidence = find_bank_candidates(pg, index)
    assert len(candidates) == 1
    assert match_type == "fuzzy"
    assert evidence["candidate_count"] == 1


def test_fuzzy_never_fires_without_amount_date_agreement():
    """Guardrail check: even with identical narration text, fuzzy
    must NOT fire if amount disagrees. Bank has NO resolved txn_id
    (simulating an unstructured real bank feed), so this genuinely
    forces the search into the fuzzy tier rather than short-
    circuiting through the exact_txn tier."""
    pg = make_normalized(
        "TXN_003", "pg", utr="UTR999", amount="500.00",
        raw_ref={"utr": "UTR999"},
    )
    bank = make_normalized(
        None, "bank", utr="UTR000",  # txn_id=None -- forces past tier 2
        amount="9999.00",  # wildly different amount
        raw_ref={"narration": "NEFT CR UTR999 MERCH_001"},
    )
    index = CandidateIndex([bank], [])

    candidates, match_type, evidence = find_bank_candidates(pg, index)
    assert len(candidates) == 0
    assert match_type == "none"


def test_fuzzy_guard_compares_expected_net_not_raw_gross():
    """Regression test for the gross-vs-net comparison bug: the fuzzy
    tier's amount guard must compare the PG record's EXPECTED NET
    (gross - fee - gst - tds) against the bank's net amount, not raw
    gross against net -- otherwise every genuine candidate is
    rejected before fuzzy scoring runs."""
    pg = make_normalized(
        None, "pg", utr="UTR123456789", amount="1000.00",
        fee="20.00", gst="3.60", tds="0.00",
        raw_ref={"utr": "UTR123456789"},
    )
    bank = make_normalized(
        None, "bank", utr="UTR123456780",  # corrupted last digit
        amount="976.40",  # NET, correctly = 1000 - 20 - 3.60
        raw_ref={"narration": "NEFT CR UTR123456789 MERCH_001"},
    )
    index = CandidateIndex([bank], [])

    candidates, match_type, evidence = find_bank_candidates(pg, index)
    assert len(candidates) == 1, (
        "Expected the genuine candidate to pass the amount guard using "
        "expected net, not be rejected by comparing raw gross to net"
    )
    assert match_type == "fuzzy"


def test_invoice_candidate_exact_txn_only():
    pg = make_normalized("TXN_004", "pg", amount="700.00")
    invoice = make_normalized("TXN_004", "invoice", amount="126.00")
    index = CandidateIndex([], [invoice])

    candidates, match_type, evidence = find_invoice_candidates(pg, index)
    assert len(candidates) == 1
    assert match_type == "exact_txn"


def test_missing_source_returns_empty_not_error():
    """missing_in_source category: no candidate should raise, it
    should cleanly return an empty result for the decision engine
    to classify."""
    pg = make_normalized("TXN_005", "pg", amount="300.00")
    index = CandidateIndex([], [])  # no bank, no invoice at all

    bank_candidates, bank_type, _ = find_bank_candidates(pg, index)
    invoice_candidates, invoice_type, _ = find_invoice_candidates(pg, index)

    assert bank_candidates == []
    assert bank_type == "none"
    assert invoice_candidates == []
    assert invoice_type == "none"


# =======================================================================
# SCORING -- ISOLATED UNIT TESTS
# =======================================================================

def test_perfect_match_scores_100():
    pg = make_normalized(
        "TXN_006", "pg", utr="UTR1", amount="1000.00", fee="20.00", gst="3.60", tds="0.00",
        raw_ref={"net_payout": "976.40", "pg_fee": "20.00", "gst_on_fee": "3.60"},
    )
    bank = make_normalized(
        "TXN_006", "bank", utr="UTR1", amount="976.40",
        raw_ref={"bank_charges": "0.00"},
    )
    invoice = make_normalized(
        "TXN_006", "invoice", amount="23.60", fee="20.00",
    )

    score = score_candidate(pg, bank, invoice)
    assert score.total_score == 100
    assert classify_confidence(score) == ConfidenceTier.HIGH


def test_missing_bank_still_scores_meaningfully():
    """PARTIAL_MATCH precursor: invoice present, bank absent -- score
    should reflect only invoice-side signals, not crash or zero out."""
    pg = make_normalized(
        "TXN_007", "pg", amount="1000.00", fee="20.00", gst="3.60",
    )
    invoice = make_normalized("TXN_007", "invoice", amount="23.60", fee="20.00")

    score = score_candidate(pg, None, invoice)
    assert score.bank_present is False
    assert score.invoice_present is True
    assert score.total_score > 0
    assert score.total_score <= 100


def test_score_never_exceeds_100():
    pg = make_normalized(
        "TXN_008", "pg", utr="UTR1", amount="1000.00", fee="20.00", gst="3.60",
        raw_ref={"net_payout": "976.40", "pg_fee": "20.00", "gst_on_fee": "3.60"},
    )
    bank = make_normalized("TXN_008", "bank", utr="UTR1", amount="976.40",
                            raw_ref={"bank_charges": "0.00"})
    invoice = make_normalized("TXN_008", "invoice", amount="23.60", fee="20.00")

    score = score_candidate(pg, bank, invoice)
    assert 0 <= score.total_score <= 100


def test_stale_tax_rate_breaks_fee_consistency_not_amount_match():
    """A tax_mismatch record: PG and bank still agree on settlement
    amounts, but invoice fee diverges from PG fee -- fee_consistency
    should fail while amount signals stay intact."""
    pg = make_normalized(
        "TXN_009", "pg", utr="UTR1", amount="1000.00", fee="20.00", gst="3.60",
        raw_ref={"net_payout": "976.40", "pg_fee": "20.00", "gst_on_fee": "3.60"},
    )
    bank = make_normalized("TXN_009", "bank", utr="UTR1", amount="976.40",
                            raw_ref={"bank_charges": "0.00"})
    # invoice fee wrongly inflated (simulating a stale-rate error)
    invoice = make_normalized("TXN_009", "invoice", amount="23.60", fee="35.00")

    score = score_candidate(pg, bank, invoice)
    assert score.fee_consistent is False
    assert score.amount_matched_bank is True  # bank-side amount still fine


# =======================================================================
# ENGINE -- DETERMINISTIC TIE-BREAKING
# =======================================================================

def test_ambiguous_candidates_resolved_deterministically_and_repeatably():
    """Two equally plausible bank candidates -- selection must be
    deterministic (same winner every run), not dependent on list
    order, and the loser must be preserved as a rejected candidate.

    Tie-break now uses the typed `utr` field (not raw_ref bank_ref),
    so the fixtures must differ on utr to actually exercise the
    final tiebreaker step."""
    pg = make_normalized("TXN_010", "pg", amount="500.00",
                         date_utc=datetime(2026, 8, 15, tzinfo=timezone.utc))
    bank_a = make_normalized("TXN_010", "bank", amount="500.00",
                             utr="UTR_B_LATER",
                             date_utc=datetime(2026, 8, 15, tzinfo=timezone.utc))
    bank_b = make_normalized("TXN_010", "bank", amount="500.00",
                             utr="UTR_A_EARLIER",
                             date_utc=datetime(2026, 8, 15, tzinfo=timezone.utc))

    normalized_pool = [pg, bank_a, bank_b]

    results_run_1 = run_matching(normalized_pool)
    results_run_2 = run_matching(normalized_pool)

    r1 = results_run_1[0]
    r2 = results_run_2[0]

    assert r1.is_ambiguous is True
    assert r1.bank_candidate_count == 2
    assert len(r1.rejected_bank_candidates) == 1

    # same run twice -> same winner both times (determinism check)
    winner_utr_1 = r1.bank_record.utr
    winner_utr_2 = r2.bank_record.utr
    assert winner_utr_1 == winner_utr_2

    # lexicographic tiebreak on utr: "UTR_A_EARLIER" < "UTR_B_LATER"
    assert winner_utr_1 == "UTR_A_EARLIER"


def test_ambiguous_result_never_auto_matchable():
    """Even if the chosen candidate's raw score would qualify as
    HIGH, genuine ambiguity must demote confidence to LOW."""
    pg = make_normalized("TXN_011", "pg", utr="UTR1", amount="500.00", fee="10.00", gst="1.80",
                         raw_ref={"net_payout": "488.20", "pg_fee": "10.00", "gst_on_fee": "1.80"})
    bank_a = make_normalized("TXN_011", "bank", utr="UTR1", amount="488.20",
                             raw_ref={"bank_charges": "0.00", "bank_ref": "BANKREF_A"})
    bank_b = make_normalized("TXN_011", "bank", utr="UTR1", amount="488.20",
                             raw_ref={"bank_charges": "0.00", "bank_ref": "BANKREF_B"})

    results = run_matching([pg, bank_a, bank_b])
    r = results[0]
    assert r.is_ambiguous is True
    assert r.confidence != ConfidenceTier.HIGH
    assert r.confidence != ConfidenceTier.MEDIUM


def test_match_result_invariants_hold():
    """__post_init__ assertions must never fire on a normal run."""
    pg = make_normalized("TXN_012", "pg", amount="200.00")
    results = run_matching([pg])
    assert results[0].sources_present == ["pg"]
    assert results[0].bank_candidate_count == 0
    assert results[0].invoice_candidate_count == 0


# =======================================================================
# INTEGRATION -- AGAINST THE REAL GENERATED BATCH
# =======================================================================

def test_full_pipeline_runs_against_generated_data():
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    results = run_matching(normalized.records)

    pg_count = len([r for r in normalized.records if r.source == "pg"])
    assert len(results) == pg_count

    summary = summarize(results)
    assert summary.total == pg_count
    print("\n" + summary.report())


def test_reference_mismatch_category_recovered_via_alternate_signal():
    """Confirms the reference_mismatch_fuzzy synthetic category is
    genuinely recoverable end-to-end. In OUR data, this typically
    resolves via exact_txn (since bank_ref still encodes the correct
    txn_id even when UTR is corrupted) -- the fuzzy tier exists for
    the realistic case where no such structured convention is
    available, which the isolated unit test above verifies directly."""
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    results = run_matching(normalized.records)

    recovered_despite_utr_mismatch = [
        r for r in results
        if r.bank_record is not None
        and r.pg_record.utr != r.bank_record.utr
    ]
    assert len(recovered_despite_utr_mismatch) > 0, (
        "Expected at least one transaction to be correctly linked "
        "despite a UTR mismatch -- reference_mismatch_fuzzy category "
        "should exercise this."
    )


def test_missing_in_source_category_produces_no_candidate_not_crash():
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    results = run_matching(normalized.records)

    missing_bank_or_invoice = [
        r for r in results
        if r.bank_record is None or r.invoice_record is None
    ]
    assert len(missing_bank_or_invoice) > 0, (
        "Expected at least one transaction with a genuinely missing "
        "bank or invoice record -- missing_in_source category."
    )


def test_no_result_has_out_of_range_score():
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    results = run_matching(normalized.records)

    for r in results:
        assert 0 <= r.score.total_score <= 100, (
            f"{r.txn_id} scored {r.score.total_score}, out of valid range"
        )


if __name__ == "__main__":
    # Candidate generation
    test_exact_utr_match_found()
    test_fuzzy_fallback_recovers_corrupted_utr()
    test_fuzzy_never_fires_without_amount_date_agreement()
    test_fuzzy_guard_compares_expected_net_not_raw_gross()
    test_invoice_candidate_exact_txn_only()
    test_missing_source_returns_empty_not_error()

    # Scoring
    test_perfect_match_scores_100()
    test_missing_bank_still_scores_meaningfully()
    test_score_never_exceeds_100()
    test_stale_tax_rate_breaks_fee_consistency_not_amount_match()

    # Engine / deterministic tie-breaking
    test_ambiguous_candidates_resolved_deterministically_and_repeatably()
    test_ambiguous_result_never_auto_matchable()
    test_match_result_invariants_hold()

    # Integration against real generated batch
    test_full_pipeline_runs_against_generated_data()
    test_reference_mismatch_category_recovered_via_alternate_signal()
    test_missing_in_source_category_produces_no_candidate_not_crash()
    test_no_result_has_out_of_range_score()

    print("\nAll Phase 3 matching engine tests passed.")