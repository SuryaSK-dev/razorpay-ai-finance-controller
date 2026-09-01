# src/tax/seller_ledger.py
"""
Determines TDS threshold applicability using each PG record's own
merchant_ytd_gross_opening field -- the merchant's true cumulative
gross balance immediately BEFORE this specific transaction, written
directly by the generator at the moment it built the record.

This deliberately does NOT reconstruct a running total by walking
the batch in any assumed order (chronological, generation, or
otherwise). An earlier version of this module sorted by date_utc and
accumulated across the batch -- that approach silently produced
wrong results, because this dataset's day_cursor cycles and resets
across synthetic categories, so transaction dates do not reliably
reflect true generation order. No batch-level ordering can be
trusted to recover the generator's actual sequence.

Since each record already carries its own correct opening balance as
real, generator-written data, no reconstruction is needed at all --
this is both simpler and strictly more accurate than the
ordering-based approach it replaces.
"""

from __future__ import annotations
from decimal import Decimal
from typing import Optional

from src.matching.engine import MatchResult


def seller_gross_after_transaction(
    match_result: MatchResult,
) -> Optional[Decimal]:
    """
    The merchant's cumulative gross INCLUDING this transaction --
    opening balance (as recorded by the generator) plus this
    transaction's own gross amount. This is what determines whether
    THIS specific transaction should have had TDS applied.

    Returns None when the opening balance is absent.

    WHY NONE AND NOT ZERO
    ---------------------
    This defaulted to Decimal("0") until section 63. That is a
    fail-OPEN in a tax control, and it is the only one that was left in
    the system:

        no opening balance
            -> cumulative gross reads as this transaction alone
            -> seller looks BELOW the INR 5,00,000 threshold
            -> expected TDS becomes zero
            -> a genuine under-withholding is reported as CORRECT

    Every other threshold in this system prefers routing to a human
    over auto-approving. This one preferred "no tax due", which is the
    wrong direction, and it contradicted the comment in
    src/financial.py about not conflating "absent" with "present and
    zero".

    None is not a new failure path. verify_tds() already refuses to
    guess when the cumulative figure is unknown, and decide()
    already turns that into tax_unverifiable -- that branch simply
    could not be reached from the production path while this function
    always returned a Decimal. Returning None connects the guard that
    was already written.
    """
    opening = match_result.pg_record.raw_ref.get("merchant_ytd_gross_opening")

    if opening is None:
        return None

    return Decimal(str(opening)) + match_result.pg_record.amount


def build_seller_annual_gross(
    match_results: list[MatchResult],
) -> dict[str, Optional[Decimal]]:
    """
    Per-record, reconstruction-free lookup: each transaction's own
    cumulative gross (opening balance + this transaction), computed
    directly from data the generator already wrote -- no batch
    ordering assumption of any kind.

    A value may be None (see seller_gross_after_transaction). That is
    the same shape decide_batch() already handles: it reads this dict
    with .get(), which returns None for a missing key, and passes the
    result straight into verify_tax() which treats None as
    "unverifiable" rather than "zero".
    """
    return {
        result.txn_id: seller_gross_after_transaction(result)
        for result in match_results
    }