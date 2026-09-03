# tests/test_reconciliation_completeness.py
"""
Every ingested bank row must be accounted for.

THE DEFECT (FAILURE_LOG.md section 66)
--------------------------------------
Reconciliation is PG-anchored. `run_matching()` emits one MatchResult per
PG record and `decide_batch()` iterates those, so bank rows are only ever
visited as CANDIDATES. Nothing scanned the pool for rows no anchor
claimed.

Section 65 closed the negative case at ingestion -- a refund is refused
before matching. This closes the general case: a well-formed POSITIVE
credit that nothing claims. Two such rows exist in the shipped batch and
were invisible in every output.

WHAT IS BEING ASSERTED
----------------------
Not a count. A PARTITION: every bank row lands in exactly one of
SELECTED, DUPLICATE_CREDIT or ORPHANED. The counts follow from that and
are asserted separately so a drift in either is legible on its own.
"""

import copy
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from src.ingestion.loader import load_batch
from src.matching.completeness import (
    DUPLICATE_CREDIT,
    ORPHANED,
    SELECTED,
    account_for_bank_rows,
)
from src.matching.engine import run_matching
from src.normalization.engine import normalize_batch

RAW_DIR = ROOT / "data" / "raw"


@pytest.fixture(scope="module")
def records():
    return normalize_batch(load_batch(RAW_DIR)).records


@pytest.fixture(scope="module")
def report(records):
    return account_for_bank_rows(records, run_matching(records))


# ======================================================================
# THE PARTITION
# ======================================================================

def test_every_bank_row_is_accounted_for_exactly_once(report, records):
    """
    THE ASSERTION. Everything else in this file is a consequence.
    """
    bank_rows = [r for r in records if r.source == "bank"]

    assert report.is_complete
    assert report.total_bank_rows == len(bank_rows)
    assert len(report.accounts) == len(bank_rows)

    refs = [a.bank_ref for a in report.accounts]
    assert len(set(refs)) == len(refs), "a bank row was accounted for twice"

    ingested = {(r.raw_ref or {}).get("bank_ref") for r in bank_rows}
    assert set(refs) == ingested, "the report and the pool disagree"


def test_the_three_dispositions_are_disjoint_and_exhaustive(report):
    buckets = (report.selected, report.duplicate_credits, report.orphaned)

    assert sum(len(b) for b in buckets) == report.total_bank_rows

    refs = [{a.bank_ref for a in b} for b in buckets]
    assert not (refs[0] & refs[1]) and not (refs[1] & refs[2]) and not (refs[0] & refs[2])

    assert {a.disposition for a in report.accounts} <= {
        SELECTED, DUPLICATE_CREDIT, ORPHANED
    }


# ======================================================================
# THE SHIPPED BATCH
# ======================================================================

def test_the_counts_on_the_real_batch(report):
    """
    64 bank rows against 61 PG records.

    59 selected -- one per PG record that found a bank counterpart.

    3 duplicate credits -- TXN_00051/52/53 are the `duplicate` generation
    category. Each has two bank credits; one is selected and the second
    is the duplicate leg, already surfaced on the PG record as
    DUPLICATE_DETECTED.

    2 orphaned -- BANKREF_TXN_00060 and _00061. Their PG records are the
    two `corrupted` records rejected at ingestion for an unparseable
    gross, so no MatchResult exists to claim their credits.
    """
    assert report.total_bank_rows == 64
    assert len(report.selected) == 59
    assert len(report.duplicate_credits) == 3
    assert len(report.orphaned) == 2


def test_the_orphans_are_the_two_ingestion_rejects(report):
    """
    Naming them matters. "Two rows unclaimed" is a number; "the bank paid
    against the two records we could not parse" is a finding.
    """
    orphans = {a.resolved_txn_id for a in report.orphaned}
    assert orphans == {"TXN_00060", "TXN_00061"}

    rejected = {
        e.raw_record.get("txn_id") for e in load_batch(RAW_DIR).pg.errors
    }
    assert orphans == rejected, (
        "the orphaned bank rows are no longer exactly the rows whose PG "
        "record was rejected at ingestion -- the finding changed shape"
    )


def test_the_orphaned_value_is_real_money(report):
    """
    The cash position reports the two rejected records as carrying an
    UNKNOWN amount, because their PG gross is unparseable. This says
    something the cash position cannot: the bank moved 517.48 against
    them anyway.
    """
    assert report.orphaned_value == Decimal("286.15") + Decimal("231.33")
    assert report.orphaned_value == Decimal("517.48")


def test_a_duplicate_credit_is_not_reported_as_orphaned(report):
    """
    The duplicate legs resolve to txn_ids that DID get claimed, so they
    are accounted for rather than unclaimed. Calling them orphaned would
    report one finding twice -- the decision table already flags those
    three records DUPLICATE_DETECTED.
    """
    dupes = {a.resolved_txn_id for a in report.duplicate_credits}
    assert dupes == {"TXN_00051", "TXN_00052", "TXN_00053"}
    assert not (dupes & {a.resolved_txn_id for a in report.orphaned})


# ======================================================================
# THE CONTROL
# ======================================================================

def test_an_added_orphan_is_reported(records):
    """
    THE CONTROL, and the reason this file is not section 63 again.

    A guard shipped without a test proving it can fail is indistinguishable
    from a guard with a typo in its condition. This adds a bank row that
    nothing can claim and asserts the orphan count rises.
    """
    baseline = account_for_bank_rows(records, run_matching(records))

    injected = copy.deepcopy(records)
    orphan = copy.deepcopy(next(r for r in injected if r.source == "bank"))
    orphan.raw_ref = dict(orphan.raw_ref or {})
    orphan.raw_ref["bank_ref"] = "BANKREF_NOBODY_CLAIMS_THIS"
    orphan.raw_ref["utr"] = "UTRORPHAN0001"
    orphan.txn_id = None
    orphan.utr = "UTRORPHAN0001"
    orphan.amount = Decimal("4242.42")
    injected.append(orphan)

    after = account_for_bank_rows(injected, run_matching(injected))

    assert after.total_bank_rows == baseline.total_bank_rows + 1
    assert len(after.orphaned) == len(baseline.orphaned) + 1, (
        "an unclaimable bank row did not surface as orphaned -- the "
        "completeness assertion cannot see the thing it exists to catch"
    )
    assert "BANKREF_NOBODY_CLAIMS_THIS" in {a.bank_ref for a in after.orphaned}
    assert after.is_complete


def test_the_partition_breaks_if_a_row_goes_missing(records):
    """
    THE SECOND CONTROL.

    `is_complete` must be falsifiable. If the report silently dropped a
    row, every count above would still look plausible. This constructs
    that state directly and asserts the property notices.
    """
    from src.matching.completeness import CompletenessReport

    full = account_for_bank_rows(records, run_matching(records))
    assert full.is_complete

    truncated = CompletenessReport(
        total_bank_rows=full.total_bank_rows,
        accounts=full.accounts[:-1],
    )
    assert not truncated.is_complete, (
        "is_complete returned True with a row missing -- it is not "
        "actually checking the partition"
    )


# ======================================================================
# NO DECISION WAS HARMED
# ======================================================================

def test_completeness_creates_no_decisions_and_no_statuses(records):
    """
    An unclaimed bank row is NOT a decision about a PG transaction.
    Synthesising one would change the 61-record denominator every
    published percentage rests on.
    """
    from src.exceptions.manager import decide_batch
    from src.models import DecisionStatus

    match_results = run_matching(records)
    decisions = decide_batch(match_results)

    assert len(decisions) == 61
    assert {d.status for d in decisions} <= set(DecisionStatus)

    report = account_for_bank_rows(records, match_results)
    decided = {d.txn_id for d in decisions}
    for account in report.orphaned:
        assert account.resolved_txn_id not in decided, (
            "an orphaned bank row leaked into the decision list"
        )


def test_completeness_does_not_import_the_agent_layer():
    """
    STRUCTURAL. This module sits in the deterministic core, and the core
    may not depend on the agent layer -- guarded generally by
    test_architecture_boundary.py, asserted here at the new file.
    """
    source = (ROOT / "src" / "matching" / "completeness.py").read_text(
        encoding="utf-8"
    )
    assert "src.agent" not in source
