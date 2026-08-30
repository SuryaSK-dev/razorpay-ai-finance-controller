"""
Exhaustive combinatorial tests for the decision policy.

Two sweeps, deliberately kept separate:

    512  = 2^9   the original Phase-4 dimensions, holding
                 duplicate_detected and amount_mismatch at False.
                 Retained because FAILURE_LOG.md section 9 refers to
                 it -- this is the sweep that found a state with no
                 matching rule and caused the catch-all to be added.

    2048 = 2^11  the COMPLETE DecisionContext space, including
                 duplicate_detected and amount_mismatch.

The coverage figure quoted in documentation is 2048/2048. The 512
sweep on its own never established coverage of the space the engine
actually produces, because two real policy dimensions were pinned
False throughout it.

test_context_dimensions_match_the_swept_space() ties both sweeps to
the dataclass, so adding a twelfth field fails loudly rather than
silently making the published figure wrong.
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
# Kept at nine so the historical 2^9 = 512 sweep stays intact and
# comparable to what FAILURE_LOG.md section 9 describes. The full
# eleven-dimension space is swept separately, below.
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


def test_every_combination_of_the_full_context_space_resolves():
    """
    2^11 = 2048 combinations -- the COMPLETE DecisionContext space.

    The 512 sweep above is the historical Phase-4 test and is kept
    because FAILURE_LOG.md section 9 refers to it: it is what found
    the state with no matching rule and caused the catch-all to be
    added.

    But 512 holds duplicate_detected and amount_mismatch at False,
    so on its own it does not establish coverage of the space the
    engine actually produces -- both of those are real policy
    dimensions with their own rules. Documenting "512/512
    combinations" as the coverage figure overstated what had been
    swept.

    This test closes that gap by sweeping all eleven dimensions.
    Every combination must resolve to exactly one rule.
    """

    import warnings

    all_fields = BOOLEAN_FIELDS + [
        "duplicate_detected",
        "amount_mismatch",
    ]

    assert len(all_fields) == 11, (
        "DecisionContext gained or lost a policy dimension; update "
        "this sweep and the coverage figure quoted in README.md."
    )

    failures = []

    with warnings.catch_warnings():
        warnings.simplefilter(
            "ignore",
            RuntimeWarning,
        )

        for combo in product(
            [False, True],
            repeat=len(all_fields),
        ):
            context = make_context(
                **dict(
                    zip(
                        all_fields,
                        combo,
                    )
                )
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
        f"{len(failures)} of 2048 combinations had no matching "
        f"rule: {failures[:5]}"
    )


def test_context_dimensions_match_the_swept_space():
    """
    Ties the sweep to the dataclass.

    If someone adds a twelfth field to DecisionContext, the sweeps
    above silently stop being exhaustive and the coverage figure in
    README.md silently becomes wrong. This fails instead.
    """

    import dataclasses

    declared = {
        field.name
        for field in dataclasses.fields(DecisionContext)
    }

    swept = set(BOOLEAN_FIELDS) | {
        "duplicate_detected",
        "amount_mismatch",
    }

    assert declared == swept, (
        f"DecisionContext fields and the swept space disagree. "
        f"Only in context: {sorted(declared - swept)}. "
        f"Only in sweep: {sorted(swept - declared)}."
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


def test_reason_codes_include_low_confidence():
    """
    REGRESSION (FAILURE_LOG.md section 53).

    The low-confidence rule reports REFERENCE_MISMATCH as its primary
    exception code, but _all_violated_codes() had no branch for
    context.low_confidence. Because low_confidence is mutually
    exclusive with every identity and source-presence flag, these
    records had no other violation to fall back on -- so a
    HUMAN_REVIEW / REFERENCE_MISMATCH decision reached the operator
    with reason_codes=[NONE].

    Six records in the real batch were affected.
    """

    context = make_context(
        low_confidence=True,
    )

    codes = _all_violated_codes(
        context
    )

    assert (
        ExceptionCode.REFERENCE_MISMATCH
        in codes
    )

    assert codes != [ExceptionCode.NONE]


def test_primary_exception_code_is_always_preserved_in_reason_codes():
    """
    THE GENERAL INVARIANT the low-confidence gap violated.

    Whatever rule fires, its exception_code must also appear in
    reason_codes. status classifies; reason_codes explain -- and an
    explanation that omits the very thing being classified is not an
    explanation.

    Swept across the COMPLETE 2^11 = 2048 context space rather than
    the 2^9 baseline, because this property must hold for every
    reachable combination of every policy dimension, including
    duplicate_detected and amount_mismatch.

    Two rules are exempt by construction:

        fully_clean_match        exception_code is NONE, and
                                 reason_codes is correctly [NONE]

        catch_all_unresolved_state
                                 fires precisely when no violation
                                 flag is set, so there is nothing for
                                 it to preserve. It is a safety net
                                 for a state that should be
                                 unreachable, and a dedicated test
                                 asserts it never fires on real data.
    """

    import warnings

    all_fields = BOOLEAN_FIELDS + [
        "duplicate_detected",
        "amount_mismatch",
    ]

    failures = []

    with warnings.catch_warnings():
        warnings.simplefilter(
            "ignore",
            RuntimeWarning,
        )

        for combo in product(
            [False, True],
            repeat=len(all_fields),
        ):
            context = make_context(
                **dict(
                    zip(
                        all_fields,
                        combo,
                    )
                )
            )

            rule = evaluate(context)

            if rule.name == "catch_all_unresolved_state":
                continue

            if rule.exception_code == ExceptionCode.NONE:
                continue

            codes = _all_violated_codes(context)

            if rule.exception_code not in codes:
                failures.append(
                    (
                        rule.name,
                        rule.exception_code.value,
                        [c.value for c in codes],
                    )
                )

    assert not failures, (
        f"{len(failures)} of 2048 combinations produced a primary "
        f"exception_code absent from reason_codes. First five: "
        f"{failures[:5]}"
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
    test_every_combination_of_the_full_context_space_resolves()
    test_context_dimensions_match_the_swept_space()
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
    test_reason_codes_include_low_confidence()
    test_primary_exception_code_is_always_preserved_in_reason_codes()
    test_decide_end_to_end_with_simultaneous_gst_and_tds_mismatch()
    test_no_transaction_in_real_batch_hits_catch_all()

    print(
        "All decision table tests passed -- 2048/2048 context "
        "combinations resolve deterministically (512/512 on the "
        "historical nine-dimension sweep)."
    )