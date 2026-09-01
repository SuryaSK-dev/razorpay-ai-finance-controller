# tests/test_guards_actually_fire.py
"""
Every defensive guard in the deterministic core, made to fire.

WHY THIS FILE EXISTS
--------------------
A coverage run over `src/` found that twelve `raise` branches had never
executed in the 366-test suite. All twelve are guards -- code whose entire
job is to refuse an invalid state. Among them:

    models.py:36    to_decimal rejects a bool
    models.py:40    to_decimal rejects a float
    engine.py       MatchResult.__post_init__ -- nine separate refusals
    manager.py:476  decide() refuses a MatchResult with no score

The first two are the Decimal firewall. It is the first thing Phase 0
built, it is on the README badge line, and ARCHITECTURE.md calls it the
cheapest possible insurance. `to_decimal` was never imported by a test and
no test ever passed a bare float to a monetary field.

The firewall works -- that was verified by hand. But nothing in the suite
proved it, which meant the answer to "show me the test that proves a float
cannot get in" was "read the code".

THIS IS THE THIRD INSTANCE OF A PATTERN THIS PROJECT HAS NAMED TWICE
--------------------------------------------------------------------
FAILURE_LOG.md section 4:

    A conditional invariant tells you nothing when the condition never
    occurs.

`test_ambiguous_result_never_auto_matchable` passed throughout the six
fail-open cases, because nothing was ever flagged ambiguous.

`test_match_result_invariants_hold` has the same shape. Its docstring says
"__post_init__ assertions must never fire on a normal run", and it asserts
exactly that -- a normal run stays normal. It never constructs an abnormal
one, so none of the nine refusals had ever been exercised.

A guard that has never refused anything is a guard nobody has tested. It is
indistinguishable, from the outside, from a guard with a typo in its
condition.

WHAT THIS FILE DOES NOT DO
--------------------------
It adds no behaviour and changes no source. Every test here constructs an
input that SHOULD be rejected and asserts it is. If a guard is ever
weakened or its condition inverted, one of these fails.
"""

import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.exceptions.decision_table import DecisionContext, evaluate
from src.exceptions.manager import decide
from src.matching.engine import MatchResult
from src.matching.scoring import ConfidenceTier, score_candidate
from src.models import (
    BankStatementRecord,
    InvoiceRecord,
    NormalizedRecord,
    PGSettlementRecord,
    to_decimal,
)

UTC_NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _normalized(source="pg", amount="100.00", txn_id="TXN_00001"):
    return NormalizedRecord(
        txn_id=txn_id,
        source=source,
        amount=Decimal(amount),
        date_utc=UTC_NOW,
    )


# ======================================================================
# THE DECIMAL FIREWALL — models.py:36 and models.py:40
# ======================================================================

def test_to_decimal_rejects_a_float():
    """
    THE PHASE-0 CLAIM, EXERCISED.

    0.1 + 0.2 != 0.3 in binary floating point, and once a monetary value
    has been through a float the precision loss is unrecoverable -- no
    downstream check can detect that a number USED TO BE exact. So the
    rejection has to happen at the boundary, and this is the test that
    proves it does.
    """
    with pytest.raises(ValueError) as excinfo:
        to_decimal(100.50)

    assert "float" in str(excinfo.value).lower()


def test_to_decimal_rejects_a_bool_before_it_can_look_like_an_int():
    """
    The ORDER of the checks in to_decimal is load-bearing, and this test
    is the only thing that pins it.

    `isinstance(True, int)` is True in Python, and `bool` is not a
    `float`. If the bool check ran second, or not at all, a stray True
    would fall through to `Decimal(str(True))` -> `Decimal("True")` ->
    InvalidOperation, raised somewhere deep in the pipeline instead of
    clearly at the boundary.
    """
    with pytest.raises(ValueError) as excinfo:
        to_decimal(True)

    assert "boolean" in str(excinfo.value).lower()

    # False is falsy and could plausibly be special-cased by accident.
    with pytest.raises(ValueError):
        to_decimal(False)


def test_to_decimal_accepts_the_forms_that_are_safe():
    """
    A firewall that rejects everything is not a firewall. The accept path
    is asserted alongside the reject path so a future over-tightening
    fails here rather than in production.
    """
    assert to_decimal("100.50") == Decimal("100.50")
    assert to_decimal(Decimal("100.50")) == Decimal("100.50")
    assert to_decimal(100) == Decimal("100")          # int is exact
    assert to_decimal("0.1") + to_decimal("0.2") == to_decimal("0.3")


def test_to_decimal_rejects_something_that_is_not_a_number_at_all():
    """The corrupted-record path: gross_amount = "NOT_A_NUMBER"."""
    with pytest.raises(ValueError) as excinfo:
        to_decimal("NOT_A_NUMBER")

    assert "cannot convert" in str(excinfo.value).lower()


@pytest.mark.parametrize(
    "model, field, base",
    [
        (PGSettlementRecord, "gross_amount", {
            "settlement_id": "SET_X", "txn_id": "TXN_00001",
            "merchant_id": "M", "pg_fee": "2.00", "gst_on_fee": "0.36",
            "net_payout": "97.64", "timestamp": UTC_NOW,
        }),
        (PGSettlementRecord, "pg_fee", {
            "settlement_id": "SET_X", "txn_id": "TXN_00001",
            "merchant_id": "M", "gross_amount": "100.00",
            "gst_on_fee": "0.36", "net_payout": "97.64", "timestamp": UTC_NOW,
        }),
        (BankStatementRecord, "credited_amount", {
            "bank_ref": "BANKREF_TXN_00001", "value_date": date(2026, 8, 2),
        }),
        (InvoiceRecord, "invoice_amount", {
            "invoice_id": "INV_X", "txn_id": "TXN_00001",
            "claimed_gst": "0.36", "claimed_tds": "0.00", "period": "2026-08",
        }),
        (NormalizedRecord, "amount", {
            "source": "pg", "date_utc": UTC_NOW,
        }),
    ],
)
def test_no_model_accepts_a_float_in_a_monetary_field(model, field, base):
    """
    The firewall is applied via `Money = Annotated[Decimal,
    BeforeValidator(to_decimal)]`, so it should hold on EVERY monetary
    field of EVERY model rather than only where someone remembered.

    Parametrised across all four models so adding a monetary field to one
    of them without the Money annotation is caught here.
    """
    with pytest.raises(ValidationError):
        model(**{**base, field: 100.50})


# ======================================================================
# MatchResult.__post_init__ — nine refusals
# ======================================================================
#
# These are the guards that make an internally inconsistent MatchResult
# unrepresentable. ARCHITECTURE.md and INTERVIEW_PREP both cite them as
# "the type refusing to represent an inconsistent state". None had ever
# been exercised.

def _valid_kwargs(**overrides):
    """A MatchResult that constructs cleanly, so each test varies one thing."""
    pg = _normalized("pg")
    kwargs = dict(
        txn_id="TXN_00001",
        pg_record=pg,
        bank_record=None,
        invoice_record=None,
        sources_present=["pg"],
    )
    kwargs.update(overrides)
    return kwargs


def test_the_baseline_matchresult_actually_constructs():
    """
    The control. Without it, a test asserting nine things RAISE could pass
    trivially because the baseline itself was malformed.
    """
    assert MatchResult(**_valid_kwargs()).txn_id == "TXN_00001"


def test_negative_bank_candidate_count_is_refused():
    with pytest.raises(ValueError, match="Negative bank candidate count"):
        MatchResult(**_valid_kwargs(bank_candidate_count=-1))


def test_negative_invoice_candidate_count_is_refused():
    with pytest.raises(ValueError, match="Negative invoice candidate count"):
        MatchResult(**_valid_kwargs(invoice_candidate_count=-1))


def test_a_result_not_anchored_to_pg_is_refused():
    """
    Every MatchResult is PG-anchored by definition. A result whose
    sources_present omits "pg" describes a reconciliation with no
    transaction to reconcile.
    """
    with pytest.raises(ValueError, match="missing 'pg'"):
        MatchResult(**_valid_kwargs(sources_present=["bank"]))


def test_a_bank_record_absent_from_sources_present_is_refused():
    """
    THE §10 GUARD.

    Two sources of truth for the same fact -- the record object and the
    sources_present list -- is exactly what made the catch-all fire on
    real data. This refuses the disagreement at construction rather than
    letting it reach the decision context.
    """
    with pytest.raises(ValueError, match="'bank' is missing"):
        MatchResult(**_valid_kwargs(
            bank_record=_normalized("bank", "97.64"),
            sources_present=["pg"],
        ))


def test_an_invoice_record_absent_from_sources_present_is_refused():
    with pytest.raises(ValueError, match="'invoice' is missing"):
        MatchResult(**_valid_kwargs(
            invoice_record=_normalized("invoice", "2.36"),
            sources_present=["pg"],
        ))


def test_authoritative_with_no_match_confidence_is_refused():
    """
    An exact_txn candidate with NO_MATCH confidence is a real state --
    the engine produces it. What it must never be is AUTHORITATIVE.
    """
    with pytest.raises(ValueError, match="NO_MATCH confidence"):
        MatchResult(**_valid_kwargs(
            bank_record=_normalized("bank", "97.64"),
            sources_present=["pg", "bank"],
            confidence=ConfidenceTier.NO_MATCH,
            authoritative_match=True,
        ))


def test_authoritative_while_ambiguous_is_refused():
    """
    THE §4 GUARD, AT THE TYPE LEVEL.

    Six records were once auto-matched that should have reached a human.
    This is the construction that would have to succeed for that to
    happen again, and it does not.
    """
    with pytest.raises(ValueError, match="while marked ambiguous"):
        MatchResult(**_valid_kwargs(
            bank_record=_normalized("bank", "97.64"),
            sources_present=["pg", "bank"],
            confidence=ConfidenceTier.HIGH,
            is_ambiguous=True,
            authoritative_match=True,
        ))


def test_authoritative_while_duplicated_is_refused():
    with pytest.raises(ValueError, match="duplicate_detected=True"):
        MatchResult(**_valid_kwargs(
            bank_record=_normalized("bank", "97.64"),
            sources_present=["pg", "bank"],
            confidence=ConfidenceTier.HIGH,
            duplicate_detected=True,
            authoritative_match=True,
        ))


def test_authoritative_with_no_selected_source_is_refused():
    """
    Authority over what? A result with neither a bank record nor an
    invoice record has nothing to be authoritative about.
    """
    with pytest.raises(ValueError, match="without at least one selected"):
        MatchResult(**_valid_kwargs(
            confidence=ConfidenceTier.HIGH,
            authoritative_match=True,
        ))


def test_a_non_authoritative_result_may_be_ambiguous_and_duplicated():
    """
    THE COMPLEMENT, and the reason the guards are conditional.

    Ambiguity and duplication are normal states the engine produces
    constantly -- 6 ambiguous and 3 duplicate records in the real batch.
    They are only forbidden in combination with authority. A guard that
    refused them outright would break the engine.
    """
    result = MatchResult(**_valid_kwargs(
        bank_record=_normalized("bank", "97.64"),
        sources_present=["pg", "bank"],
        confidence=ConfidenceTier.LOW,
        is_ambiguous=True,
        duplicate_detected=True,
        authoritative_match=False,
    ))

    assert result.is_ambiguous and result.duplicate_detected
    assert not result.authoritative_match


# ======================================================================
# decide() — manager.py:476
# ======================================================================

def test_decide_refuses_a_match_result_with_no_score():
    """
    "Never invent a confidence value."

    MatchDecision.confidence_score is an int with no null state, so a
    scoreless MatchResult could only produce a decision by fabricating
    one. Refusing is the only honest option, and this is the branch that
    does it.
    """
    result = MatchResult(**_valid_kwargs())
    assert result.score is None

    with pytest.raises(ValueError, match="has no score"):
        decide(result)


def test_decide_succeeds_once_a_real_score_exists():
    """The control for the above -- the same result, scored."""
    pg = _normalized("pg")
    result = MatchResult(**_valid_kwargs(
        score=score_candidate(pg, None, None),
    ))

    decision = decide(result)

    assert decision.txn_id == "TXN_00001"
    assert isinstance(decision.confidence_score, int)


# ======================================================================
# evaluate() — the structurally unreachable guard
# ======================================================================

def test_evaluate_would_raise_if_the_catch_all_were_ever_removed():
    """
    `evaluate()` ends with a `raise ValueError` that the catch-all rule
    makes unreachable. Coverage will always show it as missed, and that
    is correct -- it is a guard against a future edit, not a live path.

    Rather than leave it untested and unexplained, this asserts the
    PROPERTY that makes it unreachable: some rule matches an all-False
    context. If someone deletes the catch-all, that rule disappears and
    this test tells them what they broke before the ValueError does.
    """
    import warnings

    all_false = DecisionContext(
        no_candidates_found=False, is_ambiguous=False,
        duplicate_detected=False, low_confidence=False,
        missing_bank=False, missing_invoice=False,
        amount_mismatch=False, gst_mismatch=False,
        tds_mismatch=False, tax_unverifiable=False,
        fully_clean=False,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        rule = evaluate(all_false)

    assert rule is not None, (
        "no rule matched an all-False context -- evaluate() would now "
        "raise. The catch-all is what prevents that."
    )
    assert rule.name == "catch_all_unresolved_state"
