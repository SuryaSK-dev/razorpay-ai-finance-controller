# src/matching/engine.py
"""
Deterministic matching engine for the reconciliation system.

Responsibilities
----------------
1. Generate candidate sets from normalized records.
2. Select the best candidate deterministically.
3. Preserve rejected candidates for auditability.
4. Distinguish duplicate records from genuinely ambiguous candidates.
5. Detect ambiguity from the authoritative candidate lists.
6. Score the selected pairing.
7. Determine whether the selected pairing is authoritative enough for
   downstream financial reconciliation.
8. Produce a MatchResult consumed by the deterministic decision engine.

Financial authority remains deterministic.

No LLM output participates in candidate generation, candidate selection,
scoring, ambiguity classification, duplicate detection, or authoritative
match classification.

Important architectural rule
-----------------------------
Candidate selection, structural classification, and authoritative
reconciliation are separate concerns.

A deterministic winner may exist while the transaction is ambiguous,
duplicated, or otherwise not authoritative.

Example:

    PG
      |
      +---- Bank A -> exact UTR
      |
      +---- Bank B -> another plausible candidate

Bank A may be selected as the deterministic winner.

Bank B must still cause:

    is_ambiguous = True

The selected record exists for deterministic scoring and auditability,
but it must not automatically become an authoritative financial match.

Duplicate records are different from ambiguity:

    A, A
        -> duplicate_detected=True
        -> is_ambiguous=False

    A, B
        -> duplicate_detected=False
        -> is_ambiguous=True

Authoritative matching therefore requires all of the following:

    - a usable candidate was found
    - confidence is not NO_MATCH
    - candidate set is not ambiguous
    - candidate set is not duplicated

This distinction is critical for preventing downstream financial
controls from treating a merely selected candidate as a confirmed
reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from src.models import NormalizedRecord

from src.matching.candidates import (
    CandidateSet,
    generate_candidate_sets,
)

from src.matching.scoring import (
    ConfidenceTier,
    MatchScore,
    classify_confidence,
    score_candidate,
)


MATCHER_VERSION = "v4"


# ======================================================================
# RESULT MODEL
# ======================================================================


@dataclass
class MatchResult:
    """
    Complete deterministic matching outcome for one PG-anchored
    transaction.

    This is the direct input to Phase 4's deterministic decision engine.

    Important fields:

        is_ambiguous
            Multiple distinct plausible candidates exist.

        duplicate_detected
            Multiple source records represent the same underlying
            financial event.

        authoritative_match
            The selected pairing is safe for downstream deterministic
            financial reconciliation.

    `authoritative_match` is intentionally separate from
    `bank_record` / `invoice_record`.

    A selected record can exist purely for deterministic evidence while
    the pairing remains non-authoritative.
    """

    txn_id: str

    pg_record: NormalizedRecord

    bank_record: Optional[NormalizedRecord]

    invoice_record: Optional[NormalizedRecord]

    rejected_bank_candidates: list[NormalizedRecord] = field(
        default_factory=list
    )

    rejected_invoice_candidates: list[NormalizedRecord] = field(
        default_factory=list
    )

    bank_candidate_count: int = 0

    invoice_candidate_count: int = 0

    bank_match_type: str = "none"

    invoice_match_type: str = "none"

    selection_reason: str = ""

    score: Optional[MatchScore] = None

    confidence: ConfidenceTier = ConfidenceTier.NO_MATCH

    is_ambiguous: bool = False

    duplicate_detected: bool = False

    authoritative_match: bool = False

    sources_present: list[str] = field(default_factory=list)

    matcher_version: str = MATCHER_VERSION

    def __post_init__(self) -> None:
        """
        Permanent consistency guards.

        A MatchResult must always be PG anchored and its source list
        must agree with the actual selected records.

        `authoritative_match` is also validated here so downstream
        financial logic never receives an internally inconsistent result.
        """

        if self.bank_candidate_count < 0:
            raise ValueError(
                f"Negative bank candidate count for {self.txn_id}"
            )

        if self.invoice_candidate_count < 0:
            raise ValueError(
                f"Negative invoice candidate count for {self.txn_id}"
            )

        if "pg" not in self.sources_present:
            raise ValueError(
                f"MatchResult for {self.txn_id} is missing 'pg' "
                f"in sources_present."
            )

        if (
            self.bank_record is not None
            and "bank" not in self.sources_present
        ):
            raise ValueError(
                f"MatchResult for {self.txn_id} contains a bank record "
                f"but 'bank' is missing from sources_present."
            )

        if (
            self.invoice_record is not None
            and "invoice" not in self.sources_present
        ):
            raise ValueError(
                f"MatchResult for {self.txn_id} contains an invoice record "
                f"but 'invoice' is missing from sources_present."
            )

        # A structurally uncertain or NO_MATCH result can never be
        # authoritative.
        if self.authoritative_match:
            if self.confidence == ConfidenceTier.NO_MATCH:
                raise ValueError(
                    f"MatchResult {self.txn_id} cannot be authoritative "
                    "with NO_MATCH confidence."
                )

            if self.is_ambiguous:
                raise ValueError(
                    f"MatchResult {self.txn_id} cannot be authoritative "
                    "while marked ambiguous."
                )

            if self.duplicate_detected:
                raise ValueError(
                    f"MatchResult {self.txn_id} cannot be authoritative "
                    "while duplicate_detected=True."
                )

            if (
                self.bank_record is None
                and self.invoice_record is None
            ):
                raise ValueError(
                    f"MatchResult {self.txn_id} cannot be authoritative "
                    "without at least one selected source candidate."
                )


# ======================================================================
# DETERMINISTIC CANDIDATE SELECTION
# ======================================================================


def _select_best_bank_candidate(
    pg_record: NormalizedRecord,
    candidates: list[NormalizedRecord],
    match_type: str,
) -> tuple[
    Optional[NormalizedRecord],
    list[NormalizedRecord],
    str,
]:
    """
    Deterministically select the best bank candidate.

    Selection order:

        1. Lowest date delta from PG
        2. Lowest settlement amount delta from PG expected net
        3. Lowest stable UTR value

    No arbitrary list-position selection is permitted.

    All rejected candidates remain available for auditability.

    Important financial convention:

        PG amount is gross.
        Bank amount is credited/net settlement.

    Therefore the raw PG gross amount is not directly compared with
    the bank credited amount for candidate ranking.
    """

    if not candidates:
        return (
            None,
            [],
            "no candidates found",
        )

    if len(candidates) == 1:
        return (
            candidates[0],
            [],
            f"only candidate found via {match_type}",
        )

    pg_fee = pg_record.fee or Decimal("0")
    pg_gst = pg_record.gst or Decimal("0")
    pg_tds = pg_record.tds or Decimal("0")

    pg_expected_net = (
        pg_record.amount
        - pg_fee
        - pg_gst
        - pg_tds
    )

    def sort_key(
        bank_record: NormalizedRecord,
    ) -> tuple[int, Decimal, str]:
        date_delta = abs(
            (
                bank_record.date_utc.date()
                - pg_record.date_utc.date()
            ).days
        )

        amount_delta = abs(
            pg_expected_net - bank_record.amount
        )

        utr_key = bank_record.utr or ""

        return (
            date_delta,
            amount_delta,
            utr_key,
        )

    ranked = sorted(
        candidates,
        key=sort_key,
    )

    winner = ranked[0]
    rejected = ranked[1:]

    reason = (
        f"selected from {len(candidates)} candidates via "
        f"{match_type} tier; tie-broken by lowest date delta, "
        f"then expected-net amount delta, then utr"
    )

    return (
        winner,
        rejected,
        reason,
    )


def _select_best_invoice_candidate(
    pg_record: NormalizedRecord,
    candidates: list[NormalizedRecord],
    match_type: str,
) -> tuple[
    Optional[NormalizedRecord],
    list[NormalizedRecord],
    str,
]:
    """
    Deterministically select the best invoice candidate.

    Invoice candidates are expected to arrive through exact transaction
    identity matching.

    Selection order:

        1. Lowest invoice amount delta from PG fee + GST
        2. Stable transaction ID

    Rejected candidates are preserved.
    """

    if not candidates:
        return (
            None,
            [],
            "no candidates found",
        )

    if len(candidates) == 1:
        return (
            candidates[0],
            [],
            f"only candidate found via {match_type}",
        )

    pg_fee = pg_record.fee or Decimal("0")
    pg_gst = pg_record.gst or Decimal("0")

    pg_expected_invoice_amount = (
        pg_fee + pg_gst
    )

    def sort_key(
        invoice_record: NormalizedRecord,
    ) -> tuple[Decimal, str]:
        amount_delta = abs(
            pg_expected_invoice_amount
            - invoice_record.amount
        )

        txn_key = invoice_record.txn_id or ""

        return (
            amount_delta,
            txn_key,
        )

    ranked = sorted(
        candidates,
        key=sort_key,
    )

    winner = ranked[0]
    rejected = ranked[1:]

    reason = (
        f"selected from {len(candidates)} candidates via "
        f"{match_type} tier; tie-broken by lowest expected-invoice "
        f"amount delta, then txn_id"
    )

    return (
        winner,
        rejected,
        reason,
    )


# ======================================================================
# DUPLICATE / AMBIGUITY CLASSIFICATION
# ======================================================================


def _record_identity(
    record: NormalizedRecord,
) -> tuple:
    """
    Return the deterministic financial identity of a normalized record.

    A duplicate means two source rows describe the same underlying
    financial event.

    Stable typed fields are used rather than raw source dictionaries.
    """

    return (
        record.txn_id,
        record.utr,
        record.amount,
        record.date_utc,
    )


def _are_duplicate_candidates(
    candidates: list[NormalizedRecord],
) -> bool:
    """
    Return True when multiple candidates represent the same underlying
    financial record.

    Examples:

        A, A, A
            -> duplicate_detected=True
            -> is_ambiguous=False at this helper level

        A, B
            -> duplicate_detected=False
            -> is_ambiguous=True

    This helper intentionally treats the entire candidate list as the
    authoritative set produced by candidate generation.
    """

    if len(candidates) < 2:
        return False

    identities = {
        _record_identity(candidate)
        for candidate in candidates
    }

    return len(identities) == 1


def _candidate_set_is_duplicate(
    candidate_set: CandidateSet,
) -> bool:
    """
    Determine whether either source contains duplicate copies of the
    same financial record.

    Duplicate detection is performed against the authoritative
    candidate lists.
    """

    return (
        _are_duplicate_candidates(
            candidate_set.bank_candidates
        )
        or _are_duplicate_candidates(
            candidate_set.invoice_candidates
        )
    )


def _candidate_set_is_ambiguous(
    candidate_set: CandidateSet,
    duplicate_detected: bool = False,
) -> bool:
    """
    Determine whether candidate generation produced a genuinely
    ambiguous reconciliation state.

    Ambiguity means either:

        - more than one plausible primary bank candidate,
        - more than one plausible primary invoice candidate, OR
        - an additional ambiguity-only bank candidate,
        - an additional ambiguity-only invoice candidate,

    provided those candidates are not merely duplicate copies of the
    same financial record.

    Candidate generation may deliberately keep a deterministic primary
    candidate separate from additional ambiguity evidence. That separation
    must not cause the ambiguity signal to be lost here.

    Duplicate records do not simultaneously become ambiguity.

    Important:

        This function intentionally does NOT infer ambiguity from:
            - confidence
            - amount mismatch
            - date mismatch
            - match_type
            - number of rejected candidates

        An explicitly generated ambiguity-only candidate is different:
        it is direct structural evidence from candidate generation and
        therefore MUST be honored.

        Those are different concepts.
    """

    if duplicate_detected:
        return False

    # Primary candidate competition.
    primary_competition = (
        len(candidate_set.bank_candidates) > 1
        or len(candidate_set.invoice_candidates) > 1
    )

    # Some candidate-generation versions intentionally keep the
    # strongest candidate in the authoritative list while storing
    # additional plausible candidates separately as ambiguity evidence.
    #
    # These candidates MUST still make the result ambiguous. Otherwise
    # the deterministic winner can incorrectly become an authoritative
    # match merely because the stronger candidate was selected first.
    ambiguity_bank_candidates = getattr(
        candidate_set,
        "bank_ambiguity_candidates",
        [],
    )
    ambiguity_invoice_candidates = getattr(
        candidate_set,
        "invoice_ambiguity_candidates",
        [],
    )

    ambiguity_only_competition = (
        len(ambiguity_bank_candidates) > 0
        or len(ambiguity_invoice_candidates) > 0
    )

    return primary_competition or ambiguity_only_competition


# ======================================================================
# AUTHORITATIVE MATCH CLASSIFICATION
# ======================================================================


def _is_authoritative_match(
    confidence: ConfidenceTier,
    is_ambiguous: bool,
    duplicate_detected: bool,
    bank_record: Optional[NormalizedRecord],
    invoice_record: Optional[NormalizedRecord],
) -> bool:
    """
    Determine whether the selected pairing is authoritative enough for
    downstream financial reconciliation.

    This is intentionally separate from candidate selection.

    A selected candidate is NOT automatically an authoritative match.

    Conditions for authority:

        1. confidence must not be NO_MATCH
        2. candidate set must not be ambiguous
        3. candidate set must not contain duplicates
        4. at least one source candidate must actually exist

    This prevents a weakly selected candidate from being treated as
    financially confirmed merely because a record was found through an
    identifier.

    In particular:

        exact_txn candidate
        + NO_MATCH confidence
        = non-authoritative

    The selected record remains available for audit evidence.
    """

    if confidence == ConfidenceTier.NO_MATCH:
        return False

    if is_ambiguous:
        return False

    if duplicate_detected:
        return False

    if (
        bank_record is None
        and invoice_record is None
    ):
        return False

    return True


# ======================================================================
# AMBIGUITY EVIDENCE
# ======================================================================


def _ambiguity_evidence(
    candidate_set: CandidateSet,
    duplicate_detected: bool,
    is_ambiguous: bool,
) -> dict:
    """
    Build compact deterministic evidence describing the structural
    matching state.

    CandidateSet intentionally contains only the authoritative
    candidate lists and their match/evidence metadata.

    All ambiguity counts are derived directly from those fields.
    """

    bank_evidence = getattr(
        candidate_set,
        "bank_evidence",
        getattr(candidate_set, "bank_ambiguity_evidence", None),
    )
    invoice_evidence = getattr(
        candidate_set,
        "invoice_evidence",
        getattr(candidate_set, "invoice_ambiguity_evidence", None),
    )

    bank_ambiguity_candidates = getattr(
        candidate_set,
        "bank_ambiguity_candidates",
        [],
    )
    invoice_ambiguity_candidates = getattr(
        candidate_set,
        "invoice_ambiguity_candidates",
        [],
    )

    return {
        "is_ambiguous": is_ambiguous,
        "duplicate_detected": duplicate_detected,
        "bank_candidate_count": len(
            candidate_set.bank_candidates
        ),
        "invoice_candidate_count": len(
            candidate_set.invoice_candidates
        ),
        "additional_bank_ambiguity_count": len(
            bank_ambiguity_candidates
        ),
        "additional_invoice_ambiguity_count": len(
            invoice_ambiguity_candidates
        ),
        "bank_match_type": candidate_set.bank_match_type,
        "invoice_match_type": candidate_set.invoice_match_type,
        "bank_candidate_evidence": bank_evidence,
        "invoice_candidate_evidence": invoice_evidence,
    }


# ======================================================================
# CANDIDATE-SET RESOLUTION
# ======================================================================


def _resolve_candidate_set(
    candidate_set: CandidateSet,
) -> MatchResult:
    """
    Resolve one PG-anchored candidate set into a deterministic
    MatchResult.

    Resolution order:

        1. Detect duplicate / ambiguity state.
        2. Select deterministic candidates.
        3. Score selected pairing.
        4. Downgrade confidence when structural uncertainty exists.
        5. Determine authoritative_match.
        6. Build source presence.
        7. Preserve complete audit evidence.

    Important:

        deterministic winner != authoritative reconciliation

    A winner is retained even when the result is not authoritative so
    that scoring, reproducibility, and audit evidence remain deterministic.
    """

    # ---------------------------------------------------------------
    # 1. Detect structural state BEFORE selection.
    # ---------------------------------------------------------------

    duplicate_detected = _candidate_set_is_duplicate(
        candidate_set
    )

    is_ambiguous = _candidate_set_is_ambiguous(
        candidate_set,
        duplicate_detected,
    )

    # ---------------------------------------------------------------
    # 2. Deterministically select candidates.
    # ---------------------------------------------------------------

    (
        bank_record,
        rejected_bank,
        bank_reason,
    ) = _select_best_bank_candidate(
        candidate_set.pg_record,
        candidate_set.bank_candidates,
        candidate_set.bank_match_type,
    )

    (
        invoice_record,
        rejected_invoice,
        invoice_reason,
    ) = _select_best_invoice_candidate(
        candidate_set.pg_record,
        candidate_set.invoice_candidates,
        candidate_set.invoice_match_type,
    )

    # ---------------------------------------------------------------
    # 3. Score selected pairing.
    #
    # Scoring does NOT override ambiguity / duplicate state.
    # ---------------------------------------------------------------

    score = score_candidate(
        candidate_set.pg_record,
        bank_record,
        invoice_record,
    )

    confidence = classify_confidence(score)

    # ---------------------------------------------------------------
    # 4. Structural uncertainty prevents automatic confidence.
    #
    # A duplicate or ambiguity condition must never be converted into
    # HIGH/MEDIUM confidence merely because the deterministic winner
    # happens to score highly.
    # ---------------------------------------------------------------

    if (
        is_ambiguous
        and confidence
        in (
            ConfidenceTier.HIGH,
            ConfidenceTier.MEDIUM,
        )
    ):
        confidence = ConfidenceTier.LOW

    if (
        duplicate_detected
        and confidence
        in (
            ConfidenceTier.HIGH,
            ConfidenceTier.MEDIUM,
        )
    ):
        confidence = ConfidenceTier.LOW

    # ---------------------------------------------------------------
    # 5. Determine authoritative reconciliation state.
    #
    # This is intentionally AFTER confidence classification.
    # ---------------------------------------------------------------

    authoritative_match = _is_authoritative_match(
        confidence=confidence,
        is_ambiguous=is_ambiguous,
        duplicate_detected=duplicate_detected,
        bank_record=bank_record,
        invoice_record=invoice_record,
    )

    # ---------------------------------------------------------------
    # 6. Build source-presence information.
    #
    # `sources_present` describes selected records, not authority.
    # Therefore a selected but non-authoritative candidate can still
    # appear here for audit purposes.
    # ---------------------------------------------------------------

    sources_present = ["pg"]

    if bank_record is not None:
        sources_present.append("bank")

    if invoice_record is not None:
        sources_present.append("invoice")

    # ---------------------------------------------------------------
    # 7. Build deterministic selection reason.
    # ---------------------------------------------------------------

    selection_reason = "; ".join(
        filter(
            None,
            [
                (
                    f"bank: {bank_reason}"
                    if bank_reason
                    else None
                ),
                (
                    f"invoice: {invoice_reason}"
                    if invoice_reason
                    else None
                ),
            ],
        )
    )

    if duplicate_detected:
        selection_reason = (
            f"{selection_reason}; "
            "duplicate candidate set detected"
        )

    elif is_ambiguous:
        selection_reason = (
            f"{selection_reason}; "
            "multiple competing candidates detected"
        )

    if not authoritative_match:
        selection_reason = (
            f"{selection_reason}; "
            "selected pairing is non-authoritative for "
            "downstream financial reconciliation"
        )

    # ---------------------------------------------------------------
    # 8. Construct deterministic result.
    # ---------------------------------------------------------------

    ambiguity_evidence = _ambiguity_evidence(
        candidate_set,
        duplicate_detected,
        is_ambiguous,
    )

    return MatchResult(
        txn_id=candidate_set.pg_record.txn_id,
        pg_record=candidate_set.pg_record,
        bank_record=bank_record,
        invoice_record=invoice_record,
        rejected_bank_candidates=rejected_bank,
        rejected_invoice_candidates=rejected_invoice,
        bank_candidate_count=len(
            candidate_set.bank_candidates
        ),
        invoice_candidate_count=len(
            candidate_set.invoice_candidates
        ),
        bank_match_type=candidate_set.bank_match_type,
        invoice_match_type=candidate_set.invoice_match_type,
        selection_reason=selection_reason,
        score=score,
        confidence=confidence,
        is_ambiguous=is_ambiguous,
        duplicate_detected=duplicate_detected,
        authoritative_match=authoritative_match,
        sources_present=sources_present,
        matcher_version=MATCHER_VERSION,
    )


# ======================================================================
# BATCH ENTRY POINT
# ======================================================================


def run_matching(
    normalized_records: list[NormalizedRecord],
) -> list[MatchResult]:
    """
    Run deterministic candidate generation, candidate resolution,
    scoring, and confidence classification across the batch.

    One MatchResult is produced per PG-anchored transaction.
    """

    candidate_sets = generate_candidate_sets(
        normalized_records
    )

    return [
        _resolve_candidate_set(candidate_set)
        for candidate_set in candidate_sets
    ]


# ======================================================================
# BATCH SUMMARY
# ======================================================================


@dataclass
class MatchingSummary:
    """
    Typed batch-level matching summary.
    """

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


def summarize(
    results: list[MatchResult],
) -> MatchingSummary:
    """
    Produce deterministic batch-level matching statistics.
    """

    counts = {
        tier.value: 0
        for tier in ConfidenceTier
    }

    ambiguous_count = 0

    for result in results:
        counts[result.confidence.value] += 1

        if result.is_ambiguous:
            ambiguous_count += 1

    return MatchingSummary(
        total=len(results),
        high=counts["HIGH"],
        medium=counts["MEDIUM"],
        low=counts["LOW"],
        no_match=counts["NO_MATCH"],
        ambiguous_flagged=ambiguous_count,
    )