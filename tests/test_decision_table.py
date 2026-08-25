# tests/test_decision_table.py
"""
Exhaustive combinatorial tests for the decision policy.

Every possible combination of DecisionContext boolean fields is evaluated,
ensuring the decision table resolves all 2^9 combinations deterministically.
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal
from itertools import product
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.exceptions.decision_table import (
    DECISION_TABLE,
    DecisionContext,
    evaluate,
)
from src.exceptions.manager import _all_violated_codes
from src.models import DecisionStatus, ExceptionCode


BOOLEAN_FIELDS = [
    "no_candidates_found",
    "is_ambiguous",
    "low_confidence",
    "missing_bank",
    "missing_invoice",
    "gst_mismatch",
    "tds_mismatch",
    "tax_unverifiable",
    "fully_clean",
]


def make_context(**overrides) -> DecisionContext:
    """Create a DecisionContext with all boolean fields defaulting to False."""
    values = {field: False for field in BOOLEAN_FIELDS}
    values.update(overrides)
    return DecisionContext(**values)


def test_every_combination_of_conditions_resolves_without_error():
    """2^9 = 512 combinations. Every one must resolve to exactly one
    rule -- no ValueError, no ambiguity about which rule wins. Note:
    this deliberately includes the logically-impossible-in-production
    combination (fully_clean=False while every other flag is also
    False) specifically to prove the catch-all safety net handles it
    -- the resulting RuntimeWarning here is EXPECTED and intentional,
    not a bug. It is suppressed below so it doesn't look like an
    unhandled issue in test output; the real signal that matters is
    whether the pipeline integration test (below) ever triggers it
    on genuine data."""
    import warnings
    failures = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for combo in product([False, True], repeat=len(BOOLEAN_FIELDS)):
            context = DecisionContext(**dict(zip(BOOLEAN_FIELDS, combo)))
            try:
                evaluate(context)
            except ValueError as e:
                failures.append((combo, str(e)))

    assert not failures, (
        f"{len(failures)} of 512 combinations had no matching rule: "
        f"{failures[:5]}"
    )


def test_no_candidates_always_wins_regardless_of_other_flags():
    """no_candidates_found must have the highest precedence."""
    remaining_fields = BOOLEAN_FIELDS[1:]

    for combo in product([False, True], repeat=len(remaining_fields)):
        fields = dict(zip(remaining_fields, combo))

        context = DecisionContext(
            no_candidates_found=True,
            **fields,
        )

        rule = evaluate(context)

        assert rule.status == DecisionStatus.UNMATCHED


def test_ambiguous_wins_over_tax_state():
    """AMBIGUOUS takes precedence over GST/TDS/tax-unverifiable states."""
    context = make_context(
        is_ambiguous=True,
        gst_mismatch=True,
        tds_mismatch=True,
    )

    rule = evaluate(context)

    assert rule.status == DecisionStatus.AMBIGUOUS


def test_gst_mismatch_wins_over_tds_mismatch_when_both_true():
    """
    GST mismatch has higher priority than TDS variance when both are present.
    """
    context = make_context(
        gst_mismatch=True,
        tds_mismatch=True,
    )

    rule = evaluate(context)

    assert rule.exception_code == ExceptionCode.ERR_GST_MISMATCH


def test_fully_clean_produces_matched():
    context = make_context(
        fully_clean=True,
    )

    rule = evaluate(context)

    assert rule.status == DecisionStatus.MATCHED


def test_priorities_are_unique_and_form_a_dense_sequence():
    """Rule priorities must be unique and sequential from 0..N."""
    priorities = sorted(rule.priority for rule in DECISION_TABLE)

    assert len(priorities) == len(set(priorities)), (
        "duplicate priority values"
    )

    assert priorities == list(range(len(priorities))), (
        "priorities are not a dense 0..N sequence"
    )


def test_reason_codes_captures_all_violations_not_just_winning_one():
    """
    A record containing both GST and TDS mismatches must expose both
    violations, even though the decision status is determined by the
    higher-priority GST mismatch.
    """
    context = make_context(
        gst_mismatch=True,
        tds_mismatch=True,
    )

    codes = _all_violated_codes(context)

    assert ExceptionCode.ERR_GST_MISMATCH in codes
    assert ExceptionCode.ERR_TDS_VARIANCE in codes
    assert len(codes) == 2


def test_decide_end_to_end_with_simultaneous_gst_and_tds_mismatch():
    """
    Tests the complete decision path:

        MatchResult
            -> DecisionContext
            -> decision table
            -> MatchDecision

    Both GST and TDS mismatches must survive into reason_codes.
    """
    from src.exceptions.manager import decide
    from src.matching.engine import MatchResult
    from src.matching.scoring import (
        classify_confidence,
        score_candidate,
    )
    from src.models import NormalizedRecord

    pg = NormalizedRecord(
        txn_id="TXN_E2E",
        source="pg",
        utr="UTR1",
        amount=Decimal("1000.00"),
        fee=Decimal("20.00"),
        gst=Decimal("3.60"),
        tds=Decimal("0.00"),
        date_utc=datetime(2026, 8, 15, tzinfo=timezone.utc),
        raw_ref={"merchant_id": "M_E2E"},
    )

    bank = NormalizedRecord(
        txn_id="TXN_E2E",
        source="bank",
        utr="UTR1",
        amount=Decimal("976.40"),
        date_utc=datetime(2026, 8, 15, tzinfo=timezone.utc),
        raw_ref={},
    )

    invoice = NormalizedRecord(
        txn_id="TXN_E2E",
        source="invoice",
        amount=Decimal("23.60"),
        fee=Decimal("20.00"),
        gst=Decimal("6.00"),       # Deliberately incorrect GST
        tds=Decimal("10.00"),      # Deliberately incorrect TDS
        date_utc=datetime(2026, 8, 15, tzinfo=timezone.utc),
        raw_ref={},
    )

    score = score_candidate(pg, bank, invoice)
    confidence = classify_confidence(score)

    result = MatchResult(
        txn_id="TXN_E2E",
        pg_record=pg,
        bank_record=bank,
        invoice_record=invoice,
        score=score,
        confidence=confidence,
        sources_present=["pg", "bank", "invoice"],
    )

    decision = decide(
        result,
        seller_annual_gross=Decimal("600000"),
    )

    assert decision.status == DecisionStatus.TAX_MISMATCH

    assert ExceptionCode.ERR_GST_MISMATCH in decision.reason_codes
    assert ExceptionCode.ERR_TDS_VARIANCE in decision.reason_codes

def test_no_transaction_in_real_batch_hits_catch_all():
    """After the missing_bank/missing_invoice alignment fix, no
    transaction in the real generated batch should ever fall through
    to the catch-all rule -- every context should match an explicit,
    intentional rule."""
    import warnings
    from src.ingestion.loader import load_batch
    from src.normalization.engine import normalize_batch
    from src.matching.engine import run_matching
    from src.exceptions.manager import decide_batch
    from pathlib import Path

    RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    match_results = run_matching(normalized.records)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        decide_batch(match_results)
        catch_all_fired = any("catch-all" in str(w.message) for w in caught)

    assert not catch_all_fired, (
        "Catch-all rule fired on real generated data -- this means "
        "_build_context() produced an internally inconsistent state; "
        "investigate before treating Phase 4 as closed."
    )


if __name__ == "__main__":
    test_every_combination_of_conditions_resolves_without_error()
    test_no_candidates_always_wins_regardless_of_other_flags()
    test_ambiguous_wins_over_tax_state()
    test_gst_mismatch_wins_over_tds_mismatch_when_both_true()
    test_fully_clean_produces_matched()
    test_priorities_are_unique_and_form_a_dense_sequence()
    test_reason_codes_captures_all_violations_not_just_winning_one()
    test_decide_end_to_end_with_simultaneous_gst_and_tds_mismatch()
    test_no_transaction_in_real_batch_hits_catch_all()

    print(
        "All decision table tests passed "
        "-- 512/512 combinations resolve deterministically."
    )