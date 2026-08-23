# src/matching/candidates.py
"""
Candidate generation for multi-source reconciliation.

Anchors on PG settlement records. Builds indexed lookups once per
batch (O(1) amortized per PG record) rather than scanning the full
bank/invoice pool for every anchor.

This module narrows the search space only. It does NOT decide
whether a candidate IS a match -- that is scoring.py's job. It DOES
attach evidence for why each candidate was found, which becomes part
of the audit trail Phase 6 needs -- "here's the match" is a weaker
claim than "here's the match, and here's exactly why we found it."
"""

from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from rapidfuzz import fuzz

from src.models import NormalizedRecord
from src.config import (
    AMOUNT_TOLERANCE,
    DATE_TOLERANCE_DAYS,
    FUZZY_MIN_SIMILARITY,
)

MatchType = Literal["exact_utr", "exact_txn", "fuzzy", "none"]


@dataclass
class CandidateSet:
    """All plausible bank and invoice candidates found for one PG
    anchor record, plus the evidence explaining why each set of
    candidates was selected. Empty, single, or multiple candidates
    are all legitimate outcomes -- multiple is a genuine ambiguity
    signal, not a bug to work around."""
    pg_record: NormalizedRecord
    bank_candidates: list[NormalizedRecord] = field(default_factory=list)
    invoice_candidates: list[NormalizedRecord] = field(default_factory=list)
    bank_match_type: MatchType = "none"
    invoice_match_type: MatchType = "none"
    bank_evidence: dict = field(default_factory=dict)
    invoice_evidence: dict = field(default_factory=dict)


class CandidateIndex:
    """
    Pre-built indexes over the full bank and invoice pools, built once
    per batch. Every PG anchor then does O(1) dict lookups for the
    exact-match tiers, falling back to the O(n) pool only when no
    exact signal is found.
    """

    def __init__(self, bank_pool: list[NormalizedRecord],
                 invoice_pool: list[NormalizedRecord]):
        self.bank_pool = bank_pool
        self.invoice_pool = invoice_pool

        self.bank_by_utr: dict[str, list[NormalizedRecord]] = {}
        self.bank_by_txn: dict[str, list[NormalizedRecord]] = {}
        self.invoice_by_txn: dict[str, list[NormalizedRecord]] = {}

        for b in bank_pool:
            if b.utr:
                self.bank_by_utr.setdefault(b.utr, []).append(b)
            if b.txn_id:
                self.bank_by_txn.setdefault(b.txn_id, []).append(b)

        for inv in invoice_pool:
            if inv.txn_id:
                self.invoice_by_txn.setdefault(inv.txn_id, []).append(inv)


def _within_date_window(pg_date, other_date, days: int = DATE_TOLERANCE_DAYS) -> bool:
    return abs((other_date - pg_date).days) <= days


def _within_amount_tolerance(a: Decimal, b: Decimal,
                              tolerance: Decimal = AMOUNT_TOLERANCE) -> bool:
    return abs(a - b) <= tolerance


def find_bank_candidates(
    pg_record: NormalizedRecord, index: CandidateIndex
) -> tuple[list[NormalizedRecord], MatchType, dict]:
    """
    Search order, each tier consulted only if the previous tier found
    nothing:

      1. Exact UTR (O(1))             -- strongest possible signal
      2. Exact resolved txn_id (O(1))  -- from normalization's fallback chain
      3. Guarded fuzzy narration (O(n)) -- ONLY when amount + date already
         align; fuzzy text similarity is never trusted standalone

    There is deliberately NO standalone "amount + date alone" tier --
    with only 6 distinct amount values in our synthetic dataset,
    matching on amount+date with no identifier or narration evidence
    at all would be a genuine false-positive risk, not a legitimate
    match signal.
    """
    if pg_record.utr and pg_record.utr in index.bank_by_utr:
        matches = index.bank_by_utr[pg_record.utr]
        return matches, "exact_utr", {
            "utr": pg_record.utr,
            "candidate_count": len(matches),
            "reason": "exact UTR match found in bank index",
        }

    if pg_record.txn_id and pg_record.txn_id in index.bank_by_txn:
        matches = index.bank_by_txn[pg_record.txn_id]
        return matches, "exact_txn", {
            "txn_id": pg_record.txn_id,
            "candidate_count": len(matches),
            "reason": "exact resolved txn_id match found in bank index",
        }

    guarded_candidates = []
    fuzzy_scores_checked = []
    for b in index.bank_pool:
        amount_ok = _within_amount_tolerance(pg_record.amount, b.amount)
        date_ok = _within_date_window(pg_record.date_utc.date(), b.date_utc.date())
        if not (amount_ok and date_ok):
            continue  # fuzzy is irrelevant unless the anchor signals already agree

        pg_ref = str(pg_record.raw_ref.get("utr") or "")
        bank_narration = str(b.raw_ref.get("narration") or "")
        similarity = fuzz.partial_ratio(pg_ref, bank_narration)
        fuzzy_scores_checked.append(similarity)
        if similarity >= FUZZY_MIN_SIMILARITY:
            guarded_candidates.append(b)

    if guarded_candidates:
        return guarded_candidates, "fuzzy", {
            "candidate_count": len(guarded_candidates),
            "fuzzy_threshold": FUZZY_MIN_SIMILARITY,
            "reason": "recovered via guarded fuzzy narration match "
                      "(amount and date pre-aligned)",
        }

    return [], "none", {
        "reason": "no candidate found at any tier",
        "fuzzy_candidates_checked": len(fuzzy_scores_checked),
    }

def find_invoice_candidates(
    pg_record: NormalizedRecord, index: CandidateIndex
) -> tuple[list[NormalizedRecord], MatchType, dict]:
    """
    Invoices always carry txn_id directly per our schema -- no fuzzy
    step needed. A genuinely absent invoice is a MISSING_IN_INVOICE
    case for the decision engine, not a search failure to paper over.
    """
    if pg_record.txn_id and pg_record.txn_id in index.invoice_by_txn:
        matches = index.invoice_by_txn[pg_record.txn_id]
        return matches, "exact_txn", {
            "txn_id": pg_record.txn_id,
            "candidate_count": len(matches),
            "reason": "exact txn_id match found in invoice index",
        }
    return [], "none", {
        "txn_id": pg_record.txn_id,
        "reason": "no invoice record found for this txn_id",
    }


def generate_candidate_sets(normalized_records: list[NormalizedRecord]) -> list[CandidateSet]:
    """Entry point: split the flat normalized pool by source, build
    the index once, then find candidates for every PG-anchored
    transaction."""
    pg_records = [r for r in normalized_records if r.source == "pg"]
    bank_pool = [r for r in normalized_records if r.source == "bank"]
    invoice_pool = [r for r in normalized_records if r.source == "invoice"]

    index = CandidateIndex(bank_pool, invoice_pool)

    candidate_sets = []
    for pg_record in pg_records:
        bank_candidates, bank_type, bank_evidence = find_bank_candidates(pg_record, index)
        invoice_candidates, invoice_type, invoice_evidence = find_invoice_candidates(pg_record, index)
        candidate_sets.append(CandidateSet(
            pg_record=pg_record,
            bank_candidates=bank_candidates,
            invoice_candidates=invoice_candidates,
            bank_match_type=bank_type,
            invoice_match_type=invoice_type,
            bank_evidence=bank_evidence,
            invoice_evidence=invoice_evidence,
        ))

    return candidate_sets