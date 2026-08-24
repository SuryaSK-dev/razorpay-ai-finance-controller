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

from src.matching.engine import MatchResult


def seller_gross_after_transaction(match_result: MatchResult) -> Decimal:
    """
    The merchant's cumulative gross INCLUDING this transaction --
    opening balance (as recorded by the generator) plus this
    transaction's own gross amount. This is what determines whether
    THIS specific transaction should have had TDS applied.
    """
    opening = match_result.pg_record.raw_ref.get("merchant_ytd_gross_opening")
    opening_decimal = Decimal(str(opening)) if opening is not None else Decimal("0")
    return opening_decimal + match_result.pg_record.amount


def build_seller_annual_gross(match_results: list[MatchResult]) -> dict[str, Decimal]:
    """
    Per-record, reconstruction-free lookup: each transaction's own
    cumulative gross (opening balance + this transaction), computed
    directly from data the generator already wrote -- no batch
    ordering assumption of any kind. Same return shape as before
    (dict[txn_id -> Decimal]), so exceptions/manager.py's
    decide_batch() requires no changes.
    """
    return {
        result.txn_id: seller_gross_after_transaction(result)
        for result in match_results
    }