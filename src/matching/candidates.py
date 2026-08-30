# src/matching/candidates.py
"""
Candidate generation for multi-source reconciliation.

Responsibilities
----------------
1. Build deterministic indexes over bank and invoice records.
2. Find authoritative candidates using strong identifiers first.
3. Recover legitimate fuzzy matches only when financial/date guards pass.
4. Separately identify competing candidates that make a transaction
   ambiguous, without treating weak ambiguity evidence as a match.
5. Preserve complete evidence for downstream matching and audit.

Important architectural rule
-----------------------------
Candidate generation narrows the search space.

It does NOT decide whether a record is financially reconciled.

In particular:

    amount + date
        -> MAY indicate ambiguity
        -> MUST NOT by itself create a match

This distinction prevents false-positive matching while still allowing
the decision engine to detect multiple plausible records.

Financial authority remains deterministic.
No LLM output participates in this deterministic path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from rapidfuzz import fuzz

from src.models import NormalizedRecord
from src.financial import settlement_expected_net
from src.config import (
    AMOUNT_TOLERANCE,
    DATE_TOLERANCE_DAYS,
    FUZZY_MIN_SIMILARITY,
)


# ======================================================================
# TYPES
# ======================================================================

MatchType = Literal[
    "exact_utr",
    "exact_txn",
    "fuzzy",
    "none",
]


# ======================================================================
# CANDIDATE SET
# ======================================================================


@dataclass
class CandidateSet:
    """
    Complete candidate-generation output for one PG anchor.

    bank_candidates / invoice_candidates
        Candidates that are legitimate inputs to deterministic matching.

    bank_ambiguity_candidates / invoice_ambiguity_candidates
        Additional records that create a competing-candidate signal but
        are NOT themselves accepted as matches.

    This separation is critical.

    Example:

        PG
            Bank A -> exact UTR
            Bank B -> same amount/date, different transaction

        Bank A is the deterministic match candidate.
        Bank B is ambiguity evidence.

    Bank B must never become a match merely because amount/date align.
    """

    pg_record: NormalizedRecord

    bank_candidates: list[NormalizedRecord] = field(
        default_factory=list
    )

    invoice_candidates: list[NormalizedRecord] = field(
        default_factory=list
    )

    bank_match_type: MatchType = "none"

    invoice_match_type: MatchType = "none"

    bank_evidence: dict = field(
        default_factory=dict
    )

    invoice_evidence: dict = field(
        default_factory=dict
    )

    bank_ambiguity_candidates: list[NormalizedRecord] = field(
        default_factory=list
    )

    invoice_ambiguity_candidates: list[NormalizedRecord] = field(
        default_factory=list
    )

    bank_ambiguity_evidence: dict = field(
        default_factory=dict
    )

    invoice_ambiguity_evidence: dict = field(
        default_factory=dict
    )


# ======================================================================
# INDEX
# ======================================================================


class CandidateIndex:
    """
    Pre-built indexes over the complete bank and invoice pools.

    Exact identifiers use O(1) amortized dictionary lookups.

    The complete source pools remain available for guarded secondary
    searches and ambiguity detection.
    """

    def __init__(
        self,
        bank_pool: list[NormalizedRecord],
        invoice_pool: list[NormalizedRecord],
    ):
        self.bank_pool = bank_pool
        self.invoice_pool = invoice_pool

        self.bank_by_utr: dict[
            str,
            list[NormalizedRecord],
        ] = {}

        self.bank_by_txn: dict[
            str,
            list[NormalizedRecord],
        ] = {}

        self.invoice_by_txn: dict[
            str,
            list[NormalizedRecord],
        ] = {}

        for bank_record in bank_pool:
            if bank_record.utr:
                self.bank_by_utr.setdefault(
                    bank_record.utr,
                    [],
                ).append(bank_record)

            if bank_record.txn_id:
                self.bank_by_txn.setdefault(
                    bank_record.txn_id,
                    [],
                ).append(bank_record)

        for invoice_record in invoice_pool:
            if invoice_record.txn_id:
                self.invoice_by_txn.setdefault(
                    invoice_record.txn_id,
                    [],
                ).append(invoice_record)


# ======================================================================
# COMMON GUARDS
# ======================================================================


def _within_date_window(
    pg_date,
    other_date,
    days: int = DATE_TOLERANCE_DAYS,
) -> bool:
    """
    Return True when two dates fall within the configured settlement
    tolerance.
    """

    return (
        abs(
            (other_date - pg_date).days
        )
        <= days
    )


def _within_amount_tolerance(
    a: Decimal,
    b: Decimal,
    tolerance: Decimal = AMOUNT_TOLERANCE,
) -> bool:
    """
    Return True when two monetary values are within the configured
    tolerance.
    """

    return abs(a - b) <= tolerance


# ======================================================================
# BANK CANDIDATE GENERATION
# ======================================================================


def find_bank_candidates(
    pg_record: NormalizedRecord,
    index: CandidateIndex,
) -> tuple[
    list[NormalizedRecord],
    MatchType,
    dict,
]:
    """
    Find authoritative bank candidates.

    Matching tiers:

        1. Exact UTR
        2. Exact resolved transaction ID
        3. Guarded fuzzy narration

    Important:

        amount + date alone is NEVER a matching tier.

    However, amount + date aligned records are separately inspected
    for ambiguity. That allows the system to say:

        "I have an exact match, but another bank record also looks
         financially plausible."

    without incorrectly selecting the second record.
    """

    pg_expected_net = settlement_expected_net(pg_record)

    # ---------------------------------------------------------------
    # Tier 1: exact UTR
    # ---------------------------------------------------------------

    if (
        pg_record.utr
        and pg_record.utr in index.bank_by_utr
    ):
        matches = index.bank_by_utr[
            pg_record.utr
        ]

        return (
            matches,
            "exact_utr",
            {
                "utr": pg_record.utr,
                "candidate_count": len(matches),
                "reason": (
                    "exact UTR match found in bank index"
                ),
            },
        )

    # ---------------------------------------------------------------
    # Tier 2: exact resolved transaction ID
    # ---------------------------------------------------------------

    if (
        pg_record.txn_id
        and pg_record.txn_id in index.bank_by_txn
    ):
        matches = index.bank_by_txn[
            pg_record.txn_id
        ]

        return (
            matches,
            "exact_txn",
            {
                "txn_id": pg_record.txn_id,
                "candidate_count": len(matches),
                "reason": (
                    "exact resolved txn_id match found "
                    "in bank index"
                ),
            },
        )

    # ---------------------------------------------------------------
    # Tier 3: guarded fuzzy narration
    #
    # amount + date are ONLY gates.
    # ---------------------------------------------------------------

    guarded_candidates: list[
        NormalizedRecord
    ] = []

    fuzzy_scores_checked: list[
        float
    ] = []

    for bank_record in index.bank_pool:

        amount_ok = _within_amount_tolerance(
            pg_expected_net,
            bank_record.amount,
        )

        date_ok = _within_date_window(
            pg_record.date_utc.date(),
            bank_record.date_utc.date(),
        )

        if not (
            amount_ok
            and date_ok
        ):
            continue

        pg_ref = str(
            pg_record.raw_ref.get("utr")
            or ""
        )

        bank_narration = str(
            bank_record.raw_ref.get("narration")
            or ""
        )

        similarity = fuzz.partial_ratio(
            pg_ref,
            bank_narration,
        )

        fuzzy_scores_checked.append(
            similarity
        )

        if similarity >= FUZZY_MIN_SIMILARITY:
            guarded_candidates.append(
                bank_record
            )

    if guarded_candidates:
        return (
            guarded_candidates,
            "fuzzy",
            {
                "candidate_count": len(
                    guarded_candidates
                ),
                "fuzzy_threshold": FUZZY_MIN_SIMILARITY,
                "reason": (
                    "recovered via guarded fuzzy narration "
                    "match (amount and date pre-aligned)"
                ),
            },
        )

    return (
        [],
        "none",
        {
            "reason": (
                "no candidate found at any deterministic "
                "matching tier"
            ),
            "fuzzy_candidates_checked": len(
                fuzzy_scores_checked
            ),
        },
    )


# ======================================================================
# BANK AMBIGUITY DETECTION
# ======================================================================


def find_bank_ambiguity_candidates(
    pg_record: NormalizedRecord,
    index: CandidateIndex,
    selected_candidates: list[
        NormalizedRecord
    ],
) -> tuple[
    list[NormalizedRecord],
    dict,
]:
    """
    Find additional bank records that are financially/date-plausible
    alternatives to the PG transaction.

    IMPORTANT:

        These are ambiguity candidates only.

        They are NOT returned by find_bank_candidates() and therefore
        cannot become a match solely because amount/date align.

    This specifically protects the system from:

        same amount
        + same date
        + no identifier

    while still surfacing genuine competing records for human review.
    """

    expected_net = settlement_expected_net(
        pg_record
    )

    selected_ids = {
        id(record)
        for record in selected_candidates
    }

    ambiguity_candidates: list[
        NormalizedRecord
    ] = []

    for bank_record in index.bank_pool:

        # Already authoritative.
        if id(bank_record) in selected_ids:
            continue

        amount_ok = _within_amount_tolerance(
            expected_net,
            bank_record.amount,
        )

        date_ok = _within_date_window(
            pg_record.date_utc.date(),
            bank_record.date_utc.date(),
        )

        if not (
            amount_ok
            and date_ok
        ):
            continue

        # A different bank row with the same transaction identity is
        # not an alternative transaction. If it exists alongside the
        # selected candidate, it is a duplicate and must remain inside
        # bank_candidates so the decision engine can detect it.
        if (
            pg_record.txn_id
            and bank_record.txn_id
            and bank_record.txn_id
            == pg_record.txn_id
        ):
            continue

        ambiguity_candidates.append(
            bank_record
        )

    return (
        ambiguity_candidates,
        {
            "candidate_count": len(
                ambiguity_candidates
            ),
            "reason": (
                "additional bank records matched expected net "
                "amount and settlement date; retained as "
                "ambiguity evidence only"
            ),
        },
    )


# ======================================================================
# INVOICE CANDIDATE GENERATION
# ======================================================================


def find_invoice_candidates(
    pg_record: NormalizedRecord,
    index: CandidateIndex,
) -> tuple[
    list[NormalizedRecord],
    MatchType,
    dict,
]:
    """
    Invoice matching uses exact transaction identity.

    Invoices do not receive amount/date-only matching because doing so
    would risk associating an invoice belonging to another transaction.
    """

    if (
        pg_record.txn_id
        and pg_record.txn_id in index.invoice_by_txn
    ):
        matches = index.invoice_by_txn[
            pg_record.txn_id
        ]

        return (
            matches,
            "exact_txn",
            {
                "txn_id": pg_record.txn_id,
                "candidate_count": len(
                    matches
                ),
                "reason": (
                    "exact txn_id match found in "
                    "invoice index"
                ),
            },
        )

    return (
        [],
        "none",
        {
            "txn_id": pg_record.txn_id,
            "reason": (
                "no invoice record found for "
                "this txn_id"
            ),
        },
    )


# ======================================================================
# INVOICE AMBIGUITY DETECTION
# ======================================================================


def find_invoice_ambiguity_candidates(
    pg_record: NormalizedRecord,
    index: CandidateIndex,
    selected_candidates: list[
        NormalizedRecord
    ],
) -> tuple[
    list[NormalizedRecord],
    dict,
]:
    """
    Invoice ambiguity is identity-based.

    A different invoice with the same txn_id is a competing invoice
    candidate and therefore must remain visible.

    Unlike bank records, invoices do NOT use amount/date-only ambiguity
    detection.
    """

    selected_ids = {
        id(record)
        for record in selected_candidates
    }

    if not pg_record.txn_id:
        return (
            [],
            {
                "candidate_count": 0,
                "reason": (
                    "PG transaction has no txn_id; "
                    "invoice ambiguity cannot be resolved "
                    "by identity"
                ),
            },
        )

    candidates: list[
        NormalizedRecord
    ] = []

    for invoice_record in index.invoice_by_txn.get(
        pg_record.txn_id,
        [],
    ):

        if id(invoice_record) in selected_ids:
            continue

        candidates.append(
            invoice_record
        )

    return (
        candidates,
        {
            "candidate_count": len(candidates),
            "reason": (
                "additional invoice records share the "
                "same transaction identity"
            ),
        },
    )


# ======================================================================
# BATCH CANDIDATE GENERATION
# ======================================================================


def generate_candidate_sets(
    normalized_records: list[NormalizedRecord],
) -> list[CandidateSet]:
    """
    Generate complete candidate sets for every PG-anchored transaction.

    Process:

        normalized records
            ↓
        source partitioning
            ↓
        deterministic indexes
            ↓
        authoritative candidate generation
            ↓
        separate ambiguity discovery
            ↓
        CandidateSet
    """

    pg_records = [
        record
        for record in normalized_records
        if record.source == "pg"
    ]

    bank_pool = [
        record
        for record in normalized_records
        if record.source == "bank"
    ]

    invoice_pool = [
        record
        for record in normalized_records
        if record.source == "invoice"
    ]

    index = CandidateIndex(
        bank_pool,
        invoice_pool,
    )

    candidate_sets: list[
        CandidateSet
    ] = []

    for pg_record in pg_records:

        # -----------------------------------------------------------
        # Primary deterministic candidates
        # -----------------------------------------------------------

        (
            bank_candidates,
            bank_type,
            bank_evidence,
        ) = find_bank_candidates(
            pg_record,
            index,
        )

        (
            invoice_candidates,
            invoice_type,
            invoice_evidence,
        ) = find_invoice_candidates(
            pg_record,
            index,
        )

        # -----------------------------------------------------------
        # Additional ambiguity evidence
        #
        # This does NOT alter the authoritative candidate lists.
        # -----------------------------------------------------------

        (
            bank_ambiguity_candidates,
            bank_ambiguity_evidence,
        ) = find_bank_ambiguity_candidates(
            pg_record,
            index,
            bank_candidates,
        )

        (
            invoice_ambiguity_candidates,
            invoice_ambiguity_evidence,
        ) = find_invoice_ambiguity_candidates(
            pg_record,
            index,
            invoice_candidates,
        )

        candidate_sets.append(
            CandidateSet(
                pg_record=pg_record,

                bank_candidates=bank_candidates,

                invoice_candidates=invoice_candidates,

                bank_match_type=bank_type,

                invoice_match_type=invoice_type,

                bank_evidence=bank_evidence,

                invoice_evidence=invoice_evidence,

                bank_ambiguity_candidates=(
                    bank_ambiguity_candidates
                ),

                invoice_ambiguity_candidates=(
                    invoice_ambiguity_candidates
                ),

                bank_ambiguity_evidence=(
                    bank_ambiguity_evidence
                ),

                invoice_ambiguity_evidence=(
                    invoice_ambiguity_evidence
                ),
            )
        )

    return candidate_sets


# ======================================================================
# OPTIONAL LLM-ASSISTED BANK LOOKUP
# ======================================================================


def find_bank_candidates_with_llm_assist(
    pg_record: NormalizedRecord,
    index: CandidateIndex,
    llm_call_fn=None,
) -> tuple[
    list[NormalizedRecord],
    MatchType,
    dict,
]:
    """
    Optional LLM-assisted recovery path.

    The LLM is permitted only to suggest an existing transaction ID.

    Its output is validated against the deterministic bank index.

    Therefore:

        LLM suggestion
            ↓
        deterministic index lookup
            ↓
        existing bank record only

    The LLM can never invent a financial record or independently
    authorize a reconciliation.
    """

    (
        candidates,
        match_type,
        evidence,
    ) = find_bank_candidates(
        pg_record,
        index,
    )

    if candidates or llm_call_fn is None:
        return (
            candidates,
            match_type,
            evidence,
        )

    narration = str(
        pg_record.raw_ref.get(
            "narration"
        )
        or ""
    )

    if not narration:
        return (
            candidates,
            match_type,
            evidence,
        )

    from src.agent.narration_extractor import (
        extract_txn_id_via_llm,
    )

    result = extract_txn_id_via_llm(
        narration,
        llm_call_fn,
    )

    if (
        not result.succeeded
        or result.value is None
    ):
        return (
            candidates,
            match_type,
            evidence,
        )

    llm_candidates = index.bank_by_txn.get(
        result.value,
        [],
    )

    if llm_candidates:
        return (
            llm_candidates,
            "fuzzy",
            {
                "llm_suggested_txn_id": result.value,
                "llm_latency_seconds": (
                    result.latency_seconds
                ),
                "reason": (
                    "LLM proposed a txn_id found in "
                    "the deterministic index after "
                    "rule-based matching found nothing"
                ),
            },
        )

    return (
        candidates,
        match_type,
        evidence,
    )