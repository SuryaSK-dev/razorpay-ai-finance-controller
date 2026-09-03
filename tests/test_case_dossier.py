# tests/test_case_dossier.py
"""
Per-record financial evidence on the exception rows.

THE DEFECT (FAILURE_LOG.md section 68)
--------------------------------------
`get_exceptions()` returned eight fields and not one of them was money.
The system could say INR 601,761.49 was blocked across 32 records and
could not say which record held how much.

That is why every multi-step agent proposal was rejected before
submission -- an agent asked "what should I work first?" had nothing to
reason over. This is the precondition that was missing. The information
model preceded the agent, deliberately.

WHAT IS AND IS NOT BEING CLAIMED
--------------------------------
No new financial computation. `expected_net` comes from
`settlement_expected_net()` in financial.py -- the single definition
`test_no_module_re_derives_expected_net_inline` exists to protect. Every
other value is read off the MatchResult that produced the decision.

The reconciliation test is the one that matters: the sum of
`expected_net` across exception rows must equal the cash position's
non-settled buckets EXACTLY, in Decimal. If the dossier recomputed
anything, that identity is where it would show.
"""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from src.agent.tools.query_tools import (
    CASH_BUCKET_BY_STATUS,
    RESOLVED_STATUSES,
    BatchQueryContext,
)
from src.config import money
from src.financial import settlement_expected_net

DOSSIER_FIELDS = (
    "expected_net", "observed_amount", "variance",
    "pg_date", "bank_date", "identifiers", "provenance",
)


@pytest.fixture(scope="module")
def ctx():
    return BatchQueryContext()


@pytest.fixture(scope="module")
def items(ctx):
    return ctx.get_exceptions()["exceptions"]


# ======================================================================
# THE FIELDS EXIST AND COME FROM THE CANONICAL DEFINITION
# ======================================================================

def test_every_exception_row_carries_the_dossier(items):
    assert items
    for item in items:
        for field in DOSSIER_FIELDS:
            assert field in item, f"{item['txn_id']} missing {field}"


def test_expected_net_equals_the_shared_definition(ctx, items):
    """
    Not "close to". Equal to a direct call to settlement_expected_net()
    for that record. If the dossier ever re-derives the formula, this is
    where it surfaces -- the section 52 guard, applied to a new surface.
    """
    by_txn = {m.txn_id: m for m in ctx.match_results}

    for item in items:
        result = by_txn[item["txn_id"]]
        expected = settlement_expected_net(result.pg_record)
        assert item["expected_net"] == str(money(expected)), (
            f"{item['txn_id']}: dossier says {item['expected_net']}, "
            f"settlement_expected_net() says {money(expected)}"
        )


def test_observed_amount_is_the_bank_record(ctx, items):
    by_txn = {m.txn_id: m for m in ctx.match_results}

    for item in items:
        bank = by_txn[item["txn_id"]].bank_record
        if bank is None:
            assert item["observed_amount"] is None
        else:
            assert item["observed_amount"] == str(money(bank.amount))


def test_variance_is_expected_minus_observed(items):
    for item in items:
        if item["expected_net"] is None or item["observed_amount"] is None:
            assert item["variance"] is None
            continue
        expected = Decimal(item["expected_net"])
        observed = Decimal(item["observed_amount"])
        assert Decimal(item["variance"]) == money(expected - observed)


# ======================================================================
# ABSENT IS NULL, NEVER ZERO  (section 63.2)
# ======================================================================

def test_records_with_no_bank_counterpart_carry_null_not_zero(ctx, items):
    """
    THE LESSON FROM 63.2, ENFORCED ON A NEW FIELD.

    A settlement with no bank credit has an UNKNOWN observed amount.
    Reporting 0.00 would make a missing credit indistinguishable from a
    zero credit, which is the exact conflation `financial.py` carries a
    comment about and the same rule the cash position already applies to
    the two unparseable records.
    """
    by_txn = {m.txn_id: m for m in ctx.match_results}
    without_bank = [i for i in items if by_txn[i["txn_id"]].bank_record is None]

    assert without_bank, (
        "no exception lacks a bank record, so this test proves nothing on "
        "this batch -- it is a conditional invariant whose condition never "
        "occurs, which section 4 records as worthless"
    )

    for item in without_bank:
        assert item["observed_amount"] is None, (
            f"{item['txn_id']}: absent bank amount reported as "
            f"{item['observed_amount']!r} rather than null"
        )
        assert item["variance"] is None
        assert item["bank_date"] is None
        assert item["provenance"]["observed_amount"] is None


def test_no_dossier_amount_is_ever_the_string_zero_by_default(ctx, items):
    """
    THE CONTROL for the test above.

    Zero is a legitimate value -- a UPI record has a zero fee. What must
    never happen is zero appearing where the source record is absent. This
    asserts every "0.00" is backed by a real record.
    """
    by_txn = {m.txn_id: m for m in ctx.match_results}

    for item in items:
        if item["observed_amount"] == "0.00":
            assert by_txn[item["txn_id"]].bank_record is not None, (
                f"{item['txn_id']}: observed_amount is 0.00 with no bank "
                f"record -- absent was reported as zero"
            )


# ======================================================================
# THE RECONCILIATION IDENTITY
# ======================================================================

def test_expected_net_reconciles_against_the_cash_position(ctx, items):
    """
    THE TEST THAT MATTERS.

    Exceptions are every non-MATCHED record, and MATCHED is the only
    status mapping to `settled_and_verified`. So the sum of `expected_net`
    across exception rows must equal every other bucket, combined,
    exactly -- in Decimal, not to a tolerance.

    A tolerance here would let a re-derivation hide inside rounding.
    """
    total = sum(Decimal(i["expected_net"]) for i in items)

    buckets = ctx.get_cash_position()["by_bucket"]
    non_settled = sum(
        Decimal(v["amount"])
        for k, v in buckets.items()
        if k != "settled_and_verified"
    )

    assert total == non_settled, (
        f"exception expected_net sums to {total}, cash position's "
        f"non-settled buckets sum to {non_settled}"
    )
    assert total == Decimal("707546.40")


def test_the_bucket_mapping_makes_that_identity_true(ctx):
    """
    Pins WHY the identity above holds, so a change to the mapping fails
    here with an explanation rather than failing there as a number.
    """
    settled = {
        s for s, b in CASH_BUCKET_BY_STATUS.items()
        if b == "settled_and_verified"
    }
    assert settled == set(RESOLVED_STATUSES)


# ======================================================================
# SHAPE AND BOUNDARY
# ======================================================================

def test_amounts_are_quantised_strings_not_floats(items):
    for item in items:
        for field in ("expected_net", "observed_amount", "variance"):
            value = item[field]
            if value is None:
                continue
            assert isinstance(value, str), f"{field} is {type(value).__name__}"
            assert Decimal(value) == money(Decimal(value))
            assert value == str(money(Decimal(value)))


def test_identifiers_and_provenance_are_present_and_honest(ctx, items):
    by_txn = {m.txn_id: m for m in ctx.match_results}

    for item in items:
        assert item["identifiers"]["txn_id"] == item["txn_id"]
        result = by_txn[item["txn_id"]]

        if result.invoice_record is None:
            assert item["identifiers"]["invoice_id"] is None
        if result.bank_record is None:
            assert item["identifiers"]["bank_ref"] is None

        assert item["provenance"]["expected_net"] == "pg"


def test_the_tool_still_exposes_no_mutation_surface(ctx):
    """
    INVARIANT 5. The dossier is read-only; it adds fields to a payload,
    never a write path.
    """
    public = [
        m for m in dir(ctx)
        if not m.startswith("_") and callable(getattr(ctx, m))
    ]
    forbidden = [
        m for m in public
        if any(m.startswith(v) for v in
               ("set_", "update_", "delete_", "write_", "save_", "mutate_"))
    ]
    assert not forbidden, forbidden


def test_the_record_set_and_counts_are_unchanged(ctx):
    result = ctx.get_exceptions()
    assert result["count"] == 37
    assert result["total_records"] == 61
    assert len(result["exceptions"]) == 37
