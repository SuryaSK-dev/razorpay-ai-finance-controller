# src/financial.py
"""
Settlement arithmetic. One definition, imported everywhere.

WHY THIS FILE EXISTS
--------------------
`expected_net = gross - fee - GST - TDS` is the single most
financially important expression in this system. Every layer that
touches a bank amount needs it:

    matching/candidates.py   to gate the fuzzy tier and to find
                             competing candidates for ambiguity
    matching/engine.py       to rank candidates by amount delta
    matching/scoring.py      to award SCORE_AMOUNT_BANK
    exceptions/manager.py    to raise AMOUNT_MISMATCH

Until this module existed, each of those four had its own inline
copy. They agreed, and nothing enforced that they would keep
agreeing. That is the defect class this project's failure log is
built to catch, and it was invisible to every instrument in the
repository precisely BECAUSE all four copies were identical --
no test can observe a divergence that has not happened yet.

The risk is not theoretical. The moment a settlement term is added
-- a refund, a chargeback, an adjustment, the negative line items
that make real settlement reconciliation hard -- a partial edit
would leave the matcher, the scorer and the amount control
reconciling against three different definitions of the same
settlement. Candidate selection would rank on one number, the
confidence score would be computed from a second, and
AMOUNT_MISMATCH would fire against a third. Nothing would crash.
The batch would simply be wrong, quietly, in the layer with the
least test visibility.

So: one function, imported. Adding a settlement term is now a
one-line change in one place, and
tests/test_financial_invariants.py asserts across the real batch
that every consumer still agrees.

SCOPE
-----
Pure arithmetic over NormalizedRecord. No matching, no tax
verification, no decisioning, no I/O. Money in, money out.

Rounding deliberately does NOT happen here. Every input is already
quantised by config.money() at generation/normalization time, and
these are differences of already-rounded values. Re-quantising a
subtraction would imply a precision decision this module has no
authority to make.
"""

from __future__ import annotations

from decimal import Decimal

from src.models import NormalizedRecord


ZERO = Decimal("0")


def _or_zero(value: Decimal | None) -> Decimal:
    """
    Treat an absent component as zero.

    Explicit `is None` rather than `value or ZERO`: Decimal("0") is
    falsy, so the `or` form conflates "absent" with "present and
    zero". Both happen to yield Decimal("0") today, but they are
    different facts, and a future component that can legitimately be
    negative (a refund, a chargeback) would make the `or` form
    silently wrong.
    """
    return value if value is not None else ZERO


def settlement_expected_net(pg_record: NormalizedRecord) -> Decimal:
    """
    The amount the bank is expected to credit for one PG transaction.

        expected_net = gross - fee - GST on fee - TDS withheld

    PG carries GROSS. The bank credits NET. Comparing the two
    directly is the single most common reconciliation error in this
    domain, and it was a real defect here once already: the fuzzy
    amount guard compared PG gross against bank net, so the correct
    candidate was rejected before similarity was ever computed
    (FAILURE_LOG.md section 3, TP=0 -> TP=6).

    EXTENSION POINT. Real settlements net many captures into one
    transfer and subtract refunds, chargebacks and adjustments. Those
    terms belong here, in this expression, and nowhere else.
    """
    return (
        pg_record.amount
        - _or_zero(pg_record.fee)
        - _or_zero(pg_record.gst)
        - _or_zero(pg_record.tds)
    )


def expected_invoice_amount(pg_record: NormalizedRecord) -> Decimal:
    """
    The amount the merchant invoice is expected to carry.

        expected_invoice_amount = fee + GST on fee

    The invoice documents the payment gateway's own fee income and
    the GST levied on it -- NOT the merchant's underlying sale. TDS
    is deliberately absent: it is withheld from the settlement, not
    billed on the invoice.
    """
    return _or_zero(pg_record.fee) + _or_zero(pg_record.gst)
