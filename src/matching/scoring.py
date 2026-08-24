# src/matching/scoring.py
"""
Weighted scoring across ALL THREE sources (PG + bank + invoice), plus
confidence tiering. Scoring and threshold classification live
together deliberately: both are small, tightly coupled, and both are
driven entirely by config.py constants.

Scoring reads ONLY from NormalizedRecord's canonical fields -- never
from raw_ref.

Confidence classification uses normalized_score, not the raw
total_score. A transaction with only one secondary source present
(e.g. invoice but no bank) can never reach 100 raw points even when
every available signal matches perfectly -- normalizing against the
maximum ACHIEVABLE score given which sources exist is what makes
PARTIAL_MATCH reachable at all, instead of every partial-source
transaction silently collapsing into NO_MATCH regardless of how well
it actually matches.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional

from src.models import NormalizedRecord
from src.config import (
    SCORE_TXN_ID_BANK,
    SCORE_TXN_ID_INVOICE,
    SCORE_AMOUNT_BANK,
    SCORE_AMOUNT_INVOICE,
    SCORE_DATE_PROXIMITY,
    SCORE_UTR_EXACT,
    SCORE_FEE_EXACT,
    AMOUNT_TOLERANCE,
    DATE_TOLERANCE_DAYS,
    CONFIDENCE_HIGH_THRESHOLD,
    CONFIDENCE_MEDIUM_THRESHOLD,
    CONFIDENCE_LOW_THRESHOLD,
)

SCORER_VERSION = "v2"  # bumped: v1 classified confidence against raw
                        # total_score, which made PARTIAL_MATCH
                        # unreachable for any transaction missing a
                        # secondary source. v2 classifies against
                        # normalized_score instead.


class ConfidenceTier(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NO_MATCH = "NO_MATCH"


@dataclass
class MatchScore:
    """Full breakdown of how a score was derived. Every point traces
    back to a specific canonical-field comparison -- this becomes the
    audit evidence attached to the eventual MatchDecision."""
    total_score: int
    normalized_score: int
    txn_id_matched_bank: bool
    txn_id_matched_invoice: bool
    amount_matched_bank: bool
    amount_matched_invoice: bool
    date_matched_bank: bool
    date_matched_invoice: bool
    utr_matched: bool
    fee_consistent: bool
    bank_present: bool
    invoice_present: bool
    scorer_version: str = SCORER_VERSION
    signals: dict = field(default_factory=dict)


def _proportional_date_score(delta_days: int, max_points: int = SCORE_DATE_PROXIMITY,
                              window: int = DATE_TOLERANCE_DAYS) -> int:
    if delta_days > window:
        return 0
    return max_points - delta_days


def _max_achievable_score(bank_present: bool, invoice_present: bool) -> int:
    """The ceiling a candidate could reach given which secondary
    sources actually exist. Confidence must be judged against this
    ceiling, not a fixed 100 that silently assumes all three sources
    are always available."""
    max_score = 0
    if bank_present:
        max_score += SCORE_TXN_ID_BANK + SCORE_AMOUNT_BANK + SCORE_UTR_EXACT
    if invoice_present:
        max_score += SCORE_TXN_ID_INVOICE + SCORE_AMOUNT_INVOICE + SCORE_FEE_EXACT
    if bank_present or invoice_present:
        max_score += SCORE_DATE_PROXIMITY
    return max_score


def score_candidate(pg_record: NormalizedRecord,
                     bank_record: Optional[NormalizedRecord],
                     invoice_record: Optional[NormalizedRecord]) -> MatchScore:
    signals: dict = {}
    score = 0

    bank_present = bank_record is not None
    invoice_present = invoice_record is not None

    signals["missing_bank"] = not bank_present
    signals["missing_invoice"] = not invoice_present

    txn_id_matched_bank = False
    txn_id_matched_invoice = False

    if bank_present:
        txn_id_matched_bank = (bank_record.txn_id is not None
                                and bank_record.txn_id == pg_record.txn_id)
        if txn_id_matched_bank:
            score += SCORE_TXN_ID_BANK
        signals["txn_id_bank"] = {"pg": pg_record.txn_id, "bank": bank_record.txn_id,
                                   "matched": txn_id_matched_bank,
                                   "points_awarded": SCORE_TXN_ID_BANK if txn_id_matched_bank else 0}

    if invoice_present:
        txn_id_matched_invoice = invoice_record.txn_id == pg_record.txn_id
        if txn_id_matched_invoice:
            score += SCORE_TXN_ID_INVOICE
        signals["txn_id_invoice"] = {"pg": pg_record.txn_id, "invoice": invoice_record.txn_id,
                                      "matched": txn_id_matched_invoice,
                                      "points_awarded": SCORE_TXN_ID_INVOICE if txn_id_matched_invoice else 0}

    amount_matched_bank = False
    if bank_present:
        pg_fee = pg_record.fee if pg_record.fee is not None else Decimal("0")
        pg_gst = pg_record.gst if pg_record.gst is not None else Decimal("0")
        pg_tds = pg_record.tds if pg_record.tds is not None else Decimal("0")
        pg_expected_net = pg_record.amount - pg_fee - pg_gst - pg_tds

        delta = abs(pg_expected_net - bank_record.amount)
        amount_matched_bank = delta <= AMOUNT_TOLERANCE
        if amount_matched_bank:
            score += SCORE_AMOUNT_BANK
        signals["amount_bank"] = {"pg_expected_net": str(pg_expected_net),
                                   "bank_amount": str(bank_record.amount),
                                   "delta": str(delta), "matched": amount_matched_bank,
                                   "points_awarded": SCORE_AMOUNT_BANK if amount_matched_bank else 0}

    amount_matched_invoice = False
    if invoice_present:
        pg_fee = pg_record.fee if pg_record.fee is not None else Decimal("0")
        pg_gst = pg_record.gst if pg_record.gst is not None else Decimal("0")
        pg_expected_invoice_amount = pg_fee + pg_gst

        delta = abs(pg_expected_invoice_amount - invoice_record.amount)
        amount_matched_invoice = delta <= AMOUNT_TOLERANCE
        if amount_matched_invoice:
            score += SCORE_AMOUNT_INVOICE
        signals["amount_invoice"] = {"pg_expected_fee_plus_gst": str(pg_expected_invoice_amount),
                                      "invoice_amount": str(invoice_record.amount),
                                      "delta": str(delta), "matched": amount_matched_invoice,
                                      "points_awarded": SCORE_AMOUNT_INVOICE if amount_matched_invoice else 0}

    date_matched_bank = False
    date_matched_invoice = False
    date_points_awarded = 0

    if bank_present:
        delta_days = abs((bank_record.date_utc.date() - pg_record.date_utc.date()).days)
        date_matched_bank = delta_days <= DATE_TOLERANCE_DAYS
        points = _proportional_date_score(delta_days)
        date_points_awarded = max(date_points_awarded, points)
        signals["date_bank"] = {"delta_days": delta_days, "matched": date_matched_bank,
                                 "points_if_used": points}

    if invoice_present:
        delta_days = abs((invoice_record.date_utc.date() - pg_record.date_utc.date()).days)
        date_matched_invoice = delta_days <= DATE_TOLERANCE_DAYS
        points = _proportional_date_score(delta_days)
        date_points_awarded = max(date_points_awarded, points)
        signals["date_invoice"] = {"delta_days": delta_days, "matched": date_matched_invoice,
                                    "points_if_used": points}

    score += date_points_awarded
    signals["date_points_awarded"] = date_points_awarded

    utr_matched = False
    if bank_present:
        utr_matched = bool(pg_record.utr) and pg_record.utr == bank_record.utr
        if utr_matched:
            score += SCORE_UTR_EXACT
        signals["utr"] = {"pg": pg_record.utr, "bank": bank_record.utr, "matched": utr_matched,
                           "points_awarded": SCORE_UTR_EXACT if utr_matched else 0}

    fee_consistent = False
    if invoice_present:
        pg_fee = pg_record.fee if pg_record.fee is not None else Decimal("0")
        invoice_fee = invoice_record.fee if invoice_record.fee is not None else None
        if invoice_fee is not None:
            delta = abs(pg_fee - invoice_fee)
            fee_consistent = delta <= AMOUNT_TOLERANCE
            if fee_consistent:
                score += SCORE_FEE_EXACT
            signals["fee_consistency"] = {"pg_fee": str(pg_fee), "invoice_fee": str(invoice_fee),
                                           "delta": str(delta), "matched": fee_consistent,
                                           "points_awarded": SCORE_FEE_EXACT if fee_consistent else 0}

    assert 0 <= score <= 100, (
        f"Score {score} out of valid range [0,100] for txn_id="
        f"{pg_record.txn_id} -- check config.py score weights sum "
        f"to 100 and no double-counting occurred. Signals: {signals}"
    )

    max_achievable = _max_achievable_score(bank_present, invoice_present)
    normalized_score = round((score / max_achievable) * 100) if max_achievable > 0 else 0

    return MatchScore(
        total_score=score,
        normalized_score=normalized_score,
        txn_id_matched_bank=txn_id_matched_bank,
        txn_id_matched_invoice=txn_id_matched_invoice,
        amount_matched_bank=amount_matched_bank,
        amount_matched_invoice=amount_matched_invoice,
        date_matched_bank=date_matched_bank,
        date_matched_invoice=date_matched_invoice,
        utr_matched=utr_matched,
        fee_consistent=fee_consistent,
        bank_present=bank_present,
        invoice_present=invoice_present,
        signals=signals,
    )


def classify_confidence(match_score: MatchScore) -> ConfidenceTier:
    score = match_score.normalized_score  # classify against normalized,
                                            # not raw total_score

    if score >= CONFIDENCE_HIGH_THRESHOLD:
        return ConfidenceTier.HIGH

    if score >= CONFIDENCE_MEDIUM_THRESHOLD:
        if match_score.txn_id_matched_bank or match_score.txn_id_matched_invoice:
            return ConfidenceTier.MEDIUM
        return ConfidenceTier.LOW

    if score >= CONFIDENCE_LOW_THRESHOLD:
        return ConfidenceTier.LOW

    return ConfidenceTier.NO_MATCH


def is_auto_matchable(tier: ConfidenceTier) -> bool:
    return tier in (ConfidenceTier.HIGH, ConfidenceTier.MEDIUM)