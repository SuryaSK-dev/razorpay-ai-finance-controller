# tests/test_ground_truth_labels.py
"""
Ground-truth label reachability tests.

THE CLASS OF BUG THESE GUARD AGAINST
------------------------------------
Three separate defects this session came from the same root cause:
ground truth asserting a status the decision table cannot produce for
the data the generator actually emits.

    FIX (L1) duplicate    -> labelled AMBIGUOUS; table produces
                             HUMAN_REVIEW / DUPLICATE_DETECTED
    FIX (L2) unresolvable -> labelled UNMATCHED; bank_ref linkage
                             survives, so a counterpart IS found and
                             the table produces HUMAN_REVIEW /
                             AMOUNT_MISMATCH
    (earlier) ambiguous   -> labelled AMBIGUOUS while the generator
                             emitted no colliding bank row at all

Every one of them made a CORRECT engine look broken. That is the
expensive failure mode: it burns review time chasing phantom defects
and teaches the team to distrust the harness. 162 unit tests passed
throughout, because unit tests exercise the decision table's rules --
not whether the ground truth describing a category matches the data
that category emits.

WHAT THESE TESTS DO
-------------------
Assert the invariant directly: for every category, the status the
generator claims must be a status the decision table can actually
reach, and (where determinable statically) the exception code must
match the rule that produces it.

These read data/ground_truth.json rather than regenerating, so they
also catch a stale ground-truth file left behind by a partial run.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.exceptions.decision_table import DECISION_TABLE
from src.models import DecisionStatus, ExceptionCode


ROOT = Path(__file__).resolve().parent.parent
GT_PATH = ROOT / "data" / "ground_truth.json"


# The status/exception each category must produce, per the decision
# table. This duplicates EXPECTED_STATUS_BY_CATEGORY in the generator
# ON PURPOSE: if the generator's own declaration drifts, this
# independent copy catches it. A self-consistent generator is not the
# same thing as a correct one.
CATEGORY_POLICY = {
    "exact_match": {
        "statuses": {"MATCHED"},
        "codes": {"NONE"},
    },
    "timing_difference": {
        "statuses": {"MATCHED"},
        "codes": {"NONE"},
    },
    "reference_mismatch_fuzzy": {
        "statuses": {"MATCHED"},
        "codes": {"NONE"},
    },
    "amount_fee_discrepancy": {
        "statuses": {"HUMAN_REVIEW"},
        "codes": {"AMOUNT_MISMATCH"},
    },
    "tax_mismatch": {
        "statuses": {"TAX_MISMATCH"},
        "codes": {"ERR_GST_MISMATCH", "ERR_TDS_VARIANCE"},
    },
    "missing_in_source": {
        "statuses": {"UNMATCHED", "PARTIAL_MATCH"},
        "codes": {"MISSING_IN_BANK", "MISSING_IN_INVOICE"},
    },
    "duplicate": {
        "statuses": {"HUMAN_REVIEW"},
        "codes": {"DUPLICATE_DETECTED"},
    },
    "ambiguous": {
        "statuses": {"AMBIGUOUS"},
        "codes": {"AMBIGUOUS_MATCH"},
    },
    "corrupted": {
        # Produced by the ingestion terminal path, not the decision
        # table -- the record never reaches decisioning.
        "statuses": {"UNMATCHED"},
        "codes": {"CORRUPTED_RECORD"},
    },
    "unresolvable": {
        "statuses": {"HUMAN_REVIEW"},
        "codes": {"AMOUNT_MISMATCH"},
    },
}

# Statuses the decision table cannot produce, and the ingestion path
# that legitimately produces them instead.
INGESTION_ONLY_CODES = {"CORRUPTED_RECORD"}


def load_ground_truth() -> list[dict]:
    assert GT_PATH.exists(), (
        f"ground_truth.json missing at {GT_PATH}; "
        "run scripts/generate_data.py"
    )
    with GT_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


# ======================================================================
# THE CORE INVARIANT
# ======================================================================

def test_every_label_status_is_reachable_by_the_decision_table():
    """
    No ground-truth entry may assert a status that no rule in
    DECISION_TABLE produces. This is the check that catches L1/L2
    before a harness run does.
    """
    reachable = {rule.status.value for rule in DECISION_TABLE}

    # CORRUPTED_RECORD cases terminate at ingestion and never reach the
    # table, so their status is exempt from this particular check --
    # covered separately by test_corrupted_uses_ingestion_terminal_path.
    for entry in load_ground_truth():
        if entry["expected_exception_code"] in INGESTION_ONLY_CODES:
            continue

        assert entry["expected_status"] in reachable, (
            f"{entry['txn_id']} ({entry['category']}) asserts status "
            f"{entry['expected_status']!r}, which no decision rule "
            f"produces. Reachable statuses: {sorted(reachable)}"
        )


def test_every_label_matches_its_category_policy():
    """
    Status AND exception code must both match what the category is
    declared to produce.
    """
    for entry in load_ground_truth():
        category = entry["category"]

        assert category in CATEGORY_POLICY, (
            f"{entry['txn_id']}: unknown category {category!r}"
        )

        policy = CATEGORY_POLICY[category]

        assert entry["expected_status"] in policy["statuses"], (
            f"{entry['txn_id']} ({category}): status "
            f"{entry['expected_status']!r} not in "
            f"{sorted(policy['statuses'])}"
        )

        assert entry["expected_exception_code"] in policy["codes"], (
            f"{entry['txn_id']} ({category}): exception code "
            f"{entry['expected_exception_code']!r} not in "
            f"{sorted(policy['codes'])}"
        )


def test_every_label_code_is_a_real_exception_code():
    """A typo'd exception code must not survive into ground truth."""
    valid = {code.value for code in ExceptionCode}

    for entry in load_ground_truth():
        assert entry["expected_exception_code"] in valid, (
            f"{entry['txn_id']}: {entry['expected_exception_code']!r} "
            "is not a member of ExceptionCode"
        )


def test_every_label_status_is_a_real_decision_status():
    valid = {status.value for status in DecisionStatus}

    for entry in load_ground_truth():
        assert entry["expected_status"] in valid, (
            f"{entry['txn_id']}: {entry['expected_status']!r} "
            "is not a member of DecisionStatus"
        )


# ======================================================================
# THE TWO SPECIFIC REGRESSIONS
# ======================================================================

def test_l1_duplicate_is_human_review_not_ambiguous():
    """
    FIX (L1). A duplicate is not an ambiguity: ambiguity means two
    DIFFERENT transactions compete, duplication means ONE transaction
    appears twice. The operational response differs -- disambiguate vs
    reverse a row -- so the statuses must stay distinct.
    """
    entries = [
        e for e in load_ground_truth()
        if e["category"] == "duplicate"
    ]

    assert entries, "no duplicate-category entries found"

    for entry in entries:
        assert entry["expected_status"] == "HUMAN_REVIEW", (
            f"{entry['txn_id']}: duplicate must be HUMAN_REVIEW "
            "(decision_table maps duplicate_detected at priority 1)"
        )
        assert entry["expected_exception_code"] == "DUPLICATE_DETECTED"


def test_l2_unresolvable_is_human_review_not_unmatched():
    """
    FIX (L2). build_unresolvable keeps bank_ref intact, so a
    counterpart IS found. UNMATCHED requires no_candidates_found --
    both bank AND invoice absent -- which this builder never produces.
    """
    entries = [
        e for e in load_ground_truth()
        if e["category"] == "unresolvable"
    ]

    assert entries, "no unresolvable-category entries found"

    for entry in entries:
        assert entry["expected_status"] == "HUMAN_REVIEW", (
            f"{entry['txn_id']}: a found-but-discrepant record is not "
            "UNMATCHED; UNMATCHED means no counterpart exists at all"
        )
        assert entry["expected_exception_code"] == "AMOUNT_MISMATCH"


def test_corrupted_uses_ingestion_terminal_path():
    """
    Corrupted records legitimately carry UNMATCHED even though the
    decision table would not produce it, because they are rejected at
    ingestion and never reach decisioning. Documented rather than
    silently exempted.
    """
    entries = [
        e for e in load_ground_truth()
        if e["category"] == "corrupted"
    ]

    assert entries, "no corrupted-category entries found"

    for entry in entries:
        assert entry["expected_status"] == "UNMATCHED"
        assert entry["expected_exception_code"] == "CORRUPTED_RECORD"


# ======================================================================
# STRUCTURAL CHECKS
# ======================================================================

def test_ambiguous_entries_come_in_pairs():
    """
    Ambiguity is relational. An odd count means a sibling was not
    emitted, which is exactly the bug that produced six fail-open
    cases -- ground truth asserting AMBIGUOUS with nothing to collide
    against.
    """
    counts = Counter(
        e["category"] for e in load_ground_truth()
    )

    ambiguous = counts["ambiguous"]

    assert ambiguous > 0, "no ambiguous entries generated"
    assert ambiguous % 2 == 0, (
        f"{ambiguous} ambiguous entries is odd; every ambiguous "
        "record needs a colliding sibling"
    )


def test_txn_ids_are_unique():
    entries = load_ground_truth()
    ids = [e["txn_id"] for e in entries]

    duplicates = [
        txn_id for txn_id, n in Counter(ids).items() if n > 1
    ]

    assert not duplicates, f"duplicate txn_ids in ground truth: {duplicates}"


def test_every_entry_has_required_fields():
    required = {
        "txn_id",
        "expected_status",
        "expected_exception_code",
        "category",
        "notes",
    }

    for entry in load_ground_truth():
        missing = required - set(entry)
        assert not missing, (
            f"{entry.get('txn_id')}: missing fields {sorted(missing)}"
        )


def main() -> None:
    test_every_label_status_is_reachable_by_the_decision_table()
    test_every_label_matches_its_category_policy()
    test_every_label_code_is_a_real_exception_code()
    test_every_label_status_is_a_real_decision_status()
    test_l1_duplicate_is_human_review_not_ambiguous()
    test_l2_unresolvable_is_human_review_not_unmatched()
    test_corrupted_uses_ingestion_terminal_path()
    test_ambiguous_entries_come_in_pairs()
    test_txn_ids_are_unique()
    test_every_entry_has_required_fields()

    print("Ground-truth label reachability tests passed.")


if __name__ == "__main__":
    main()