"""
Exhaustive combinatorial tests for the decision policy.

The original nine independent policy dimensions are exhaustively tested
as 2^9 = 512 combinations.

The newer duplicate_detected and amount_mismatch fields are explicit
DecisionContext facts, but default to False in the exhaustive sweep.
Dedicated tests cover those new policy paths separately.
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal
from itertools import product
from pathlib import Path

sys.path.append(
    str(Path(__file__).resolve().parent.parent)
)

from src.exceptions.decision_table import (
    DECISION_TABLE,
    DecisionContext,
    evaluate,
)
from src.exceptions.manager import _all_violated_codes
from src.models import DecisionStatus, ExceptionCode


# -----------------------------------------------------------------------
# Original Phase-4 policy dimensions.
#
# Keep this at nine dimensions so the established exhaustive test
# remains 2^9 = 512 combinations.
# -----------------------------------------------------------------------

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
    """
    Create a DecisionContext with every policy field defaulting to False.

    New deterministic fields:
        duplicate_detected
        amount_mismatch

    are also explicitly initialized here so tests cannot accidentally
    depend on dataclass defaults.
    """

    values = {
        field: False
        for field in BOOLEAN_FIELDS
    }

    values.update(
        {
            "duplicate_detected": False,
            "amount_mismatch": False,
        }
    )

    values.update(overrides)

    return DecisionContext(**values)


# =======================================================================
# EXHAUSTIVE POLICY COVERAGE
# =======================================================================


def test_every_combination_of_conditions_resolves_without_error():
    """
    2^9 = 512 combinations.

    Every established Phase-4 combination must resolve to exactly one
    decision rule.

    duplicate_detected and amount_mismatch are held at False here.
    Their explicit policy paths are tested independently below.
    """

    import warnings

    failures = []

    with warnings.catch_warnings():
        warnings.simplefilter(
            "ignore",
            RuntimeWarning,
        )

        for combo in product(
            [False, True],
            repeat=len(BOOLEAN_FIELDS),
        ):
            values = dict(
                zip(
                    BOOLEAN_FIELDS,
                    combo,
                )
            )

            context = make_context(
                **values
            )

            try:
                evaluate(context)

            except ValueError as error:
                failures.append(
                    (
                        combo,
                        str(error),
                    )
                )

    assert not failures, (
        f"{len(failures)} of 512 combinations had "
        f"no matching rule: {failures[:5]}"
    )


# =======================================================================
# PRIORITY TESTS
# =======================================================================


def test_no_candidates_always_wins_regardless_of_other_flags():
    """
    no_candidates_found must have the highest precedence.
    """

    remaining_fields = BOOLEAN_FIELDS[1:]

    for combo in product(
        [False, True],
        repeat=len(remaining_fields),
    ):
        fields = dict(
            zip(
                remaining_fields,
                combo,
            )
        )

        context = make_context(
            no_candidates_found=True,
            **fields,
        )

        rule = evaluate(context)

        assert rule.status == DecisionStatus.UNMATCHED


def test_duplicate_wins_over_ambiguous_and_tax_state():
    """
    A duplicate record condition has explicit policy precedence over
    ambiguity and downstream tax conditions.
    """

    context = make_context(
        duplicate_detected=True,
        is_ambiguous=True,
        gst_mismatch=True,
        tds_mismatch=True,
    )

    rule = evaluate(context)

    assert rule.status == DecisionStatus.HUMAN_REVIEW
    assert rule.exception_code == (
        ExceptionCode.DUPLICATE_DETECTED
    )


def test_ambiguous_wins_over_tax_state():
    """
    AMBIGUOUS takes precedence over GST/TDS/tax-unverifiable states.
    """

    context = make_context(
        is_ambiguous=True,
        gst_mismatch=True,
        tds_mismatch=True,
    )

    rule = evaluate(context)

    assert rule.status == DecisionStatus.AMBIGUOUS
    assert rule.exception_code == (
        ExceptionCode.AMBIGUOUS_MATCH
    )


def test_amount_mismatch_wins_over_tax_mismatch():
    """
    Amount mismatch has an explicit deterministic policy rule.

    This prevents an amount discrepancy from falling through to a
    reference-mismatch or unrelated low-confidence rule.
    """

    context = make_context(
        amount_mismatch=True,
        gst_mismatch=True,
        tds_mismatch=True,
    )

    rule = evaluate(context)

    assert rule.status == DecisionStatus.HUMAN_REVIEW
    assert rule.exception_code == (
        ExceptionCode.AMOUNT_MISMATCH
    )


def test_low_confidence_is_not_used_when_ambiguity_exists():
    """
    Ambiguity is a stronger structural uncertainty than low confidence.
    """

    context = make_context(
        is_ambiguous=True,
        low_confidence=True,
    )

    rule = evaluate(context)

    assert rule.status == DecisionStatus.AMBIGUOUS


def test_gst_mismatch_wins_over_tds_mismatch_when_both_true():
    """
    GST mismatch has higher priority than TDS variance when both
    are present.
    """

    context = make_context(
        gst_mismatch=True,
        tds_mismatch=True,
    )

    rule = evaluate(context)

    assert rule.exception_code == (
        ExceptionCode.ERR_GST_MISMATCH
    )


def test_fully_clean_produces_matched():
    context = make_context(
        fully_clean=True,
    )

    rule = evaluate(context)

    assert rule.status == DecisionStatus.MATCHED


# =======================================================================
# POLICY STRUCTURE
# =======================================================================


def test_priorities_are_unique_and_form_a_dense_sequence():
    """
    Rule priorities must be unique and sequential from 0..N.
    """

    priorities = sorted(
        rule.priority
        for rule in DECISION_TABLE
    )

    assert len(priorities) == len(
        set(priorities)
    ), "duplicate priority values"

    assert priorities == list(
        range(len(priorities))
    ), "priorities are not a dense 0..N sequence"


# =======================================================================
# REASON-CODE COVERAGE
# =======================================================================


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

    codes = _all_violated_codes(
        context
    )

    assert (
        ExceptionCode.ERR_GST_MISMATCH
        in codes
    )

    assert (
        ExceptionCode.ERR_TDS_VARIANCE
        in codes
    )

    assert len(codes) == 2


def test_reason_codes_include_amount_mismatch():
    context = make_context(
        amount_mismatch=True,
    )

    codes = _all_violated_codes(
        context
    )

    assert (
        ExceptionCode.AMOUNT_MISMATCH
        in codes
    )


def test_reason_codes_include_duplicate():
    context = make_context(
        duplicate_detected=True,
    )

    codes = _all_violated_codes(
        context
    )

    assert (
        ExceptionCode.DUPLICATE_DETECTED
        in codes
    )


# =======================================================================
# FULL DECISION PATH
# =======================================================================


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
        date_utc=datetime(
            2026,
            8,
            15,
            tzinfo=timezone.utc,
        ),
        raw_ref={
            "merchant_id": "M_E2E"
        },
    )

    bank = NormalizedRecord(
        txn_id="TXN_E2E",
        source="bank",
        utr="UTR1",
        amount=Decimal("976.40"),
        date_utc=datetime(
            2026,
            8,
            15,
            tzinfo=timezone.utc,
        ),
        raw_ref={},
    )

    invoice = NormalizedRecord(
        txn_id="TXN_E2E",
        source="invoice",
        amount=Decimal("23.60"),
        fee=Decimal("20.00"),
        gst=Decimal("6.00"),
        tds=Decimal("10.00"),
        date_utc=datetime(
            2026,
            8,
            15,
            tzinfo=timezone.utc,
        ),
        raw_ref={},
    )

    score = score_candidate(
        pg,
        bank,
        invoice,
    )

    confidence = classify_confidence(
        score
    )

    result = MatchResult(
        txn_id="TXN_E2E",
        pg_record=pg,
        bank_record=bank,
        invoice_record=invoice,
        score=score,
        confidence=confidence,
        sources_present=[
            "pg",
            "bank",
            "invoice",
        ],
    )

    decision = decide(
        result,
        seller_annual_gross=Decimal(
            "600000"
        ),
    )

    assert (
        decision.status
        == DecisionStatus.TAX_MISMATCH
    )

    assert (
        ExceptionCode.ERR_GST_MISMATCH
        in decision.reason_codes
    )

    assert (
        ExceptionCode.ERR_TDS_VARIANCE
        in decision.reason_codes
    )


# =======================================================================
# REAL-BATCH SAFETY CHECK
# =======================================================================


def test_no_transaction_in_real_batch_hits_catch_all():
    """
    No transaction in the real generated batch should fall through
    to the catch-all rule.

    Every production context must resolve through an intentional rule.
    """

    import warnings

    from src.ingestion.loader import load_batch
    from src.normalization.engine import normalize_batch
    from src.matching.engine import run_matching
    from src.exceptions.manager import decide_batch

    RAW_DIR = (
        Path(__file__).resolve().parent.parent
        / "data"
        / "raw"
    )

    batch = load_batch(
        RAW_DIR
    )

    normalized = normalize_batch(
        batch
    )

    match_results = run_matching(
        normalized.records
    )

    with warnings.catch_warnings(
        record=True
    ) as caught:

        warnings.simplefilter(
            "always"
        )

        decide_batch(
            match_results
        )

        catch_all_fired = any(
            "catch-all"
            in str(w.message)
            for w in caught
        )

    assert not catch_all_fired, (
        "Catch-all rule fired on real generated data -- "
        "this means _build_context() produced an internally "
        "inconsistent state; investigate before treating "
        "Phase 4 as closed."
    )


# =======================================================================
# TEST ENTRY POINT
# =======================================================================


if __name__ == "__main__":
    test_every_combination_of_conditions_resolves_without_error()
    test_no_candidates_always_wins_regardless_of_other_flags()
    test_duplicate_wins_over_ambiguous_and_tax_state()
    test_ambiguous_wins_over_tax_state()
    test_amount_mismatch_wins_over_tax_mismatch()
    test_low_confidence_is_not_used_when_ambiguity_exists()
    test_gst_mismatch_wins_over_tds_mismatch_when_both_true()
    test_fully_clean_produces_matched()
    test_priorities_are_unique_and_form_a_dense_sequence()
    test_reason_codes_captures_all_violations_not_just_winning_one()
    test_reason_codes_include_amount_mismatch()
    test_reason_codes_include_duplicate()
    test_decide_end_to_end_with_simultaneous_gst_and_tds_mismatch()
    test_no_transaction_in_real_batch_hits_catch_all()

    print(
        "All decision table tests passed "
        "-- 512/512 baseline combinations resolve "
        "deterministically."
    )