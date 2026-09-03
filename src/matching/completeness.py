# src/matching/completeness.py
"""
Every ingested bank row must be accounted for.

THE DEFECT THIS CLOSES
----------------------
Reconciliation is PG-anchored. `run_matching()` produces one MatchResult
per PG record, and `decide_batch()` iterates those. Bank rows are only
ever visited as CANDIDATES for a PG anchor. Nothing scanned the bank pool
for rows that no anchor claimed.

On the shipped batch that is 64 bank rows against 61 PG records, and five
of those rows appeared in no decision, no exception and no error count.

Section 65 closed the negative case at ingestion: a refund is refused
before it reaches matching. This closes the general case -- a perfectly
well-formed positive credit that nothing claims.

WHY THIS IS NOT A DECISION
--------------------------
An unclaimed bank row is not a decision ABOUT a PG transaction. It has no
`txn_id` of its own to anchor to, and inventing a MatchDecision for one
would change the 61-record denominator that every published percentage
rests on. It is a different KIND of finding, so it gets its own output
and its own report -- the same treatment ingestion rejections already
get: counted, printed, never dropped.

THE PARTITION
-------------
Classification is purely structural. It needs the normalized records and
the match results, and deliberately NOT the decisions, so that
completeness does not become coupled to decision policy:

    SELECTED           the row is the chosen bank_record of some
                       MatchResult

    DUPLICATE_CREDIT   not selected, but ANOTHER row resolving to the
                       same txn_id was. This is the second credit of a
                       duplicate pair, already surfaced on the PG record
                       as DUPLICATE_DETECTED. Accounted for, not lost --
                       reporting it again as unclaimed would double-count
                       a finding the decision table already made.

    ORPHANED           not selected, and nothing resolving to its txn_id
                       was selected either. Nothing in the batch claims
                       this money. THIS is the previously invisible class.

The three are disjoint and exhaustive over the bank pool. That is the
assertion; the counts are a consequence of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from src.models import NormalizedRecord

SELECTED = "SELECTED"
DUPLICATE_CREDIT = "DUPLICATE_CREDIT"
ORPHANED = "ORPHANED"


@dataclass(frozen=True)
class BankRowAccount:
    """
    One bank row and what became of it.

    `bank_ref` is the key rather than object identity: it is unique and
    always present across the batch, and an identity-keyed report would
    silently mis-handle two structurally equal rows.
    """

    bank_ref: str
    disposition: str
    resolved_txn_id: Optional[str]
    amount: Decimal
    utr: Optional[str]
    narration: Optional[str]


@dataclass(frozen=True)
class CompletenessReport:
    """
    The accounting over the whole bank pool.

    `is_complete` is the property worth asserting: every ingested row
    landed in exactly one bucket. The counts are downstream of it.
    """

    total_bank_rows: int
    accounts: tuple[BankRowAccount, ...] = field(default_factory=tuple)

    def _of(self, disposition: str) -> tuple[BankRowAccount, ...]:
        return tuple(a for a in self.accounts if a.disposition == disposition)

    @property
    def selected(self) -> tuple[BankRowAccount, ...]:
        return self._of(SELECTED)

    @property
    def duplicate_credits(self) -> tuple[BankRowAccount, ...]:
        return self._of(DUPLICATE_CREDIT)

    @property
    def orphaned(self) -> tuple[BankRowAccount, ...]:
        """Rows nothing in the batch claims. The previously silent class."""
        return self._of(ORPHANED)

    @property
    def is_complete(self) -> bool:
        """Every row accounted for exactly once."""
        return (
            len(self.accounts) == self.total_bank_rows
            and len({a.bank_ref for a in self.accounts}) == self.total_bank_rows
        )

    @property
    def orphaned_value(self) -> Decimal:
        """
        Total value nothing claims.

        Reported separately from the cash position on purpose: this money
        is not part of the 61-record settlement expectation, so folding it
        into those buckets would change figures that mean something else.
        """
        return sum((a.amount for a in self.orphaned), Decimal("0"))


def account_for_bank_rows(
    normalized_records: list[NormalizedRecord],
    match_results: list,
) -> CompletenessReport:
    """
    Partition the bank pool by what became of each row.

    Takes the same inputs the matching layer already has. Adds no
    financial computation -- every value here is read off a record that
    ingestion already validated.
    """
    bank_rows = [r for r in normalized_records if r.source == "bank"]

    selected_refs: set[str] = set()
    claimed_txn_ids: set[str] = set()

    for result in match_results:
        chosen = getattr(result, "bank_record", None)
        if chosen is None:
            continue
        ref = (chosen.raw_ref or {}).get("bank_ref")
        if ref is not None:
            selected_refs.add(ref)
        if chosen.txn_id is not None:
            claimed_txn_ids.add(chosen.txn_id)

    accounts: list[BankRowAccount] = []

    for row in bank_rows:
        raw = row.raw_ref or {}
        ref = raw.get("bank_ref")

        if ref in selected_refs:
            disposition = SELECTED
        elif row.txn_id is not None and row.txn_id in claimed_txn_ids:
            # Another credit for this transaction WAS selected, so this is
            # the second leg of a duplicate. decide() already surfaced it
            # as DUPLICATE_DETECTED on the PG record; calling it unclaimed
            # here would report one finding twice.
            disposition = DUPLICATE_CREDIT
        else:
            disposition = ORPHANED

        accounts.append(
            BankRowAccount(
                bank_ref=ref,
                disposition=disposition,
                resolved_txn_id=row.txn_id,
                amount=row.amount,
                utr=row.utr,
                narration=raw.get("narration"),
            )
        )

    return CompletenessReport(
        total_bank_rows=len(bank_rows),
        accounts=tuple(accounts),
    )
