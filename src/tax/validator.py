# src/tax/validator.py
"""
Deterministic tax verification: GST on the payment gateway fee, and
TDS under Section 393 (formerly 194-O). Pure arithmetic against
config.py constants -- no AI, no inference, no rounding shortcuts.

This module answers exactly one question per transaction: does the
tax math check out? It does not decide MATCHED/EXCEPTION -- that is
the decision engine's job, one layer up.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from src.models import NormalizedRecord
from src.config import (
    GST_RATE_ON_FEE,
    TDS_RATE_SECTION_393,
    TDS_ANNUAL_THRESHOLD,
    TAX_TOLERANCE,
    money,
)


@dataclass
class TaxVerification:
    """Full breakdown of tax verification for one transaction --
    every expected-vs-claimed value is preserved, not just a
    pass/fail bit, so the exception evidence is self-explanatory."""
    gst_verified: bool
    tds_verified: bool
    expected_gst: Decimal
    claimed_gst: Optional[Decimal]
    gst_delta: Optional[Decimal]
    expected_tds: Decimal
    claimed_tds: Optional[Decimal]
    tds_delta: Optional[Decimal]
    tds_threshold_applicable: Optional[bool]  # None if we couldn't determine
    signals: dict = field(default_factory=dict)

    @property
    def fully_verified(self) -> bool:
        return self.gst_verified and self.tds_verified


def verify_gst(pg_record: NormalizedRecord,
               invoice_record: Optional[NormalizedRecord]) -> tuple[bool, Decimal, Optional[Decimal], Optional[Decimal]]:
    """
    Expected GST = PG fee * 18%. Compared against the invoice's
    claimed GST -- the invoice is the statutory document being
    verified, not the PG record's own internal computation.
    """
    pg_fee = pg_record.fee if pg_record.fee is not None else Decimal("0")
    expected_gst = money(pg_fee * GST_RATE_ON_FEE)

    if invoice_record is None:
        return False, expected_gst, None, None

    claimed_gst = invoice_record.gst if invoice_record.gst is not None else Decimal("0")
    delta = abs(expected_gst - claimed_gst)
    verified = delta <= TAX_TOLERANCE

    return verified, expected_gst, claimed_gst, delta


def verify_tds(pg_record: NormalizedRecord,
               invoice_record: Optional[NormalizedRecord],
               seller_annual_gross: Optional[Decimal]) -> tuple[bool, Decimal, Optional[Decimal], Optional[Decimal], Optional[bool]]:
    """
    Expected TDS = gross * 0.1%, but ONLY if the seller's cumulative
    annual gross exceeds INR 5,00,000. Below it, expected TDS is
    correctly zero -- this function distinguishes "TDS correctly
    withheld at zero because under threshold" from "TDS wrongly
    missing despite being over threshold." If seller_annual_gross is
    unknown, verification is explicitly impossible -- never assumed.
    """
    if seller_annual_gross is None:
        return False, Decimal("0"), None, None, None

    threshold_applicable = seller_annual_gross > TDS_ANNUAL_THRESHOLD
    expected_tds = money(pg_record.amount * TDS_RATE_SECTION_393) if threshold_applicable else Decimal("0.00")

    if invoice_record is None:
        return False, expected_tds, None, None, threshold_applicable

    claimed_tds = invoice_record.tds if invoice_record.tds is not None else Decimal("0")
    delta = abs(expected_tds - claimed_tds)
    verified = delta <= TAX_TOLERANCE

    return verified, expected_tds, claimed_tds, delta, threshold_applicable


def verify_tax(pg_record: NormalizedRecord,
               invoice_record: Optional[NormalizedRecord],
               seller_annual_gross: Optional[Decimal]) -> TaxVerification:
    gst_verified, expected_gst, claimed_gst, gst_delta = verify_gst(pg_record, invoice_record)
    tds_verified, expected_tds, claimed_tds, tds_delta, threshold_applicable = verify_tds(
        pg_record, invoice_record, seller_annual_gross
    )

    signals = {
        "gst": {"expected": str(expected_gst), "claimed": str(claimed_gst) if claimed_gst is not None else None,
                "delta": str(gst_delta) if gst_delta is not None else None, "verified": gst_verified},
        "tds": {"expected": str(expected_tds), "claimed": str(claimed_tds) if claimed_tds is not None else None,
                "delta": str(tds_delta) if tds_delta is not None else None, "verified": tds_verified,
                "threshold_applicable": threshold_applicable},
    }

    return TaxVerification(
        gst_verified=gst_verified,
        tds_verified=tds_verified,
        expected_gst=expected_gst,
        claimed_gst=claimed_gst,
        gst_delta=gst_delta,
        expected_tds=expected_tds,
        claimed_tds=claimed_tds,
        tds_delta=tds_delta,
        tds_threshold_applicable=threshold_applicable,
        signals=signals,
    )