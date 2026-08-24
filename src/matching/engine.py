# src/matching/engine.py
"""
Batch orchestrator: ties candidate generation and scoring together to
produce one MatchResult per PG-anchored transaction. This is the
single entry point the decision engine (Phase 4) calls.

When a candidate set contains more than one plausible bank or invoice
record, the winner is chosen deterministically (never by arbitrary
list position), and the rejected alternatives are preserved on the
result -- not discarded -- so an auditor can always answer "why THIS
one, and not that one?"
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from src.models import NormalizedRecord
from src.matching.candidates import generate_candidate_sets, CandidateSet
from src.matching.scoring import score_candidate, classify_confidence, MatchScore, ConfidenceTier

MATCHER_VERSION = "v1"  # bump if candidate selection or scoring logic changes meaningfully


@dataclass
class MatchResult:
    """Complete matching outcome for one PG-anchored transaction --
    the object Phase 4's decision engine consumes directly."""
    txn_id: str
    pg_record: NormalizedRecord
    bank_record: Optional[NormalizedRecord]
    invoice_record: Optional[NormalizedRecord]
    rejected_bank_candidates: list[NormalizedRecord] = field(default_factory=list)
    rejected_invoice_candidates: list[NormalizedRecord] = field(default_factory=list)
    bank_candidate_count: int = 0
    invoice_candidate_count: int = 0
    bank_match_type: str = "none"
    invoice_match_type: str = "none"
    selection_reason: str = ""   # explains WHY the chosen candidate won,
                                  # when more than one existed
    score: Optional[MatchScore] = None
    confidence: ConfidenceTier = ConfidenceTier.NO_MATCH
    is_ambiguous: bool = False
    sources_present: list[str] = field(default_factory=list)
    matcher_version: str = MATCHER_VERSION

    def __post_init__(self):
        # Permanent regression guards -- fail loudly if orchestration
        # ever produces an internally inconsistent result.
        assert self.bank_candidate_count >= 0
        assert self.invoice_candidate_count >= 0
        assert "pg" in self.sources_present, (
            f"MatchResult for {self.txn_id} is missing 'pg' in "
            f"sources_present -- every result must be PG-anchored."
        )
        if self.bank_record is not None:
            assert "bank" in self.sources_present
        if self.invoice_record is not None:
            assert "invoice" in self.sources_present


def _select_best_bank_candidate(
    pg_record: NormalizedRecord, candidates: list[NormalizedRecord], match_type: str
) -> tuple[Optional[NormalizedRecord], list[NormalizedRecord], str]:
    """
    Deterministic selection among multiple bank candidates -- never
    picks by arbitrary list position. Tie-break order:
      1. Lowest date delta from the PG record
      2. Lowest amount delta from the PG record
      3. The record's own typed `utr` field (final deterministic
         tiebreaker). Deliberately NOT an optional raw_ref dict
         lookup -- utr is a real schema field on every
         NormalizedRecord, so this never silently falls back to an
         empty string for reasons unrelated to the actual tie.
    """
    if not candidates:
        return None, [], "no candidates found"

    if len(candidates) == 1:
        return candidates[0], [], f"only candidate found via {match_type}"

    def sort_key(bank_record: NormalizedRecord):
        date_delta = abs((bank_record.date_utc.date() - pg_record.date_utc.date()).days)
        amount_delta = abs(pg_record.amount - bank_record.amount)
        utr_key = bank_record.utr or ""
        return (date_delta, amount_delta, utr_key)

    ranked = sorted(candidates, key=sort_key)
    winner = ranked[0]
    rejected = ranked[1:]
    reason = (
        f"selected from {len(candidates)} candidates via {match_type} tier; "
        f"tie-broken by lowest date delta, then amount delta, then utr"
    )
    return winner, rejected, reason


def _select_best_invoice_candidate(
    pg_record: NormalizedRecord, candidates: list[NormalizedRecord], match_type: str
) -> tuple[Optional[NormalizedRecord], list[NormalizedRecord], str]:
    """Invoices only ever arrive via exact_txn match in our schema,
    so multiple candidates here would mean a genuine duplicate
    invoice for the same txn_id -- tie-break by lowest amount delta,
    then the record's own typed txn_id (stable, always present on a
    matched invoice candidate -- never an optional raw_ref lookup)."""
    if not candidates:
        return None, [], "no candidates found"

    if len(candidates) == 1:
        return candidates[0], [], f"only candidate found via {match_type}"

    def sort_key(invoice_record: NormalizedRecord):
        amount_delta = abs(pg_record.amount - invoice_record.amount)
        txn_key = invoice_record.txn_id or ""
        return (amount_delta, txn_key)

    ranked = sorted(candidates, key=sort_key)
    winner = ranked[0]
    rejected = ranked[1:]
    reason = (
        f"selected from {len(candidates)} candidates via {match_type} tier; "
        f"tie-broken by lowest amount delta, then txn_id"
    )
    return winner, rejected, reason


def _resolve_candidate_set(candidate_set: CandidateSet) -> MatchResult:
    bank_record, rejected_bank, bank_reason = _select_best_bank_candidate(
        candidate_set.pg_record, candidate_set.bank_candidates, candidate_set.bank_match_type
    )
    invoice_record, rejected_invoice, invoice_reason = _select_best_invoice_candidate(
        candidate_set.pg_record, candidate_set.invoice_candidates, candidate_set.invoice_match_type
    )

    is_ambiguous = len(candidate_set.bank_candidates) > 1 or len(candidate_set.invoice_candidates) > 1

    score = score_candidate(candidate_set.pg_record, bank_record, invoice_record)
    confidence = classify_confidence(score)

    # A genuinely ambiguous candidate set is never auto-matchable,
    # regardless of what the raw score says -- multiple equally
    # plausible candidates is itself the reason for uncertainty, even
    # though we now deterministically pick a "best" one for scoring.
    if is_ambiguous and confidence in (ConfidenceTier.HIGH, ConfidenceTier.MEDIUM):
        confidence = ConfidenceTier.LOW

    sources_present = ["pg"]
    if bank_record:
        sources_present.append("bank")
    if invoice_record:
        sources_present.append("invoice")

    selection_reason = "; ".join(filter(None, [
        f"bank: {bank_reason}" if bank_reason else None,
        f"invoice: {invoice_reason}" if invoice_reason else None,
    ]))

    return MatchResult(
        txn_id=candidate_set.pg_record.txn_id,
        pg_record=candidate_set.pg_record,
        bank_record=bank_record,
        invoice_record=invoice_record,
        rejected_bank_candidates=rejected_bank,
        rejected_invoice_candidates=rejected_invoice,
        bank_candidate_count=len(candidate_set.bank_candidates),
        invoice_candidate_count=len(candidate_set.invoice_candidates),
        bank_match_type=candidate_set.bank_match_type,
        invoice_match_type=candidate_set.invoice_match_type,
        selection_reason=selection_reason,
        score=score,
        confidence=confidence,
        is_ambiguous=is_ambiguous,
        sources_present=sources_present,
    )


def run_matching(normalized_records: list[NormalizedRecord]) -> list[MatchResult]:
    """Entry point for Phase 4: run full candidate generation +
    deterministic selection + scoring + confidence classification
    across the whole batch."""
    candidate_sets = generate_candidate_sets(normalized_records)
    return [_resolve_candidate_set(cs) for cs in candidate_sets]


@dataclass
class MatchingSummary:
    """Typed batch-level summary -- easier to extend and to assert
    against in tests than a bare dict."""
    total: int
    high: int
    medium: int
    low: int
    no_match: int
    ambiguous_flagged: int

    def report(self) -> str:
        lines = [
            "Matching Summary",
            "-" * 40,
            f"Total processed  : {self.total}",
            f"HIGH confidence  : {self.high}",
            f"MEDIUM confidence: {self.medium}",
            f"LOW confidence   : {self.low}",
            f"NO_MATCH         : {self.no_match}",
            f"Ambiguous flagged: {self.ambiguous_flagged}",
        ]
        return "\n".join(lines)


def summarize(results: list[MatchResult]) -> MatchingSummary:
    counts = {tier.value: 0 for tier in ConfidenceTier}
    ambiguous_count = 0
    for r in results:
        counts[r.confidence.value] += 1
        if r.is_ambiguous:
            ambiguous_count += 1

    return MatchingSummary(
        total=len(results),
        high=counts["HIGH"],
        medium=counts["MEDIUM"],
        low=counts["LOW"],
        no_match=counts["NO_MATCH"],
        ambiguous_flagged=ambiguous_count,
    )