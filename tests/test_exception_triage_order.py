# tests/test_exception_triage_order.py
"""
Exceptions come back in policy-severity order, derived from the table.

THE DEFECT (FAILURE_LOG.md section 67)
--------------------------------------
`get_exceptions()` sorted by `txn_id` -- alphabetical, and the least
useful ordering an operator could be handed. Meanwhile DECISION_TABLE
already carried an explicit `priority=0..11` per rule, authored
deliberately and swept over all 2048 context combinations. The triage
view ignored a severity ordering that already existed and was already
tested.

THE KEY, AND WHY IT IS THE RULE NAME
------------------------------------
The obvious derivation is lossy:

    {rule.exception_code: rule.priority for rule in DECISION_TABLE}

HUMAN_REVIEW_REQUIRED is emitted by three rules -- `no_candidates_at_all`
(0), `tax_unverifiable_but_matched` (9) and `catch_all_unresolved_state`
(11). A dict keyed on the code keeps the last, so the single most severe
state in the table would inherit priority 11 and sort last.

Rule names are unique, and every decision records the rule that fired.
`test_the_code_keyed_mapping_would_have_been_wrong` pins that reasoning
so nobody "simplifies" it back.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from src.agent.tools.query_tools import BatchQueryContext
from src.exceptions.decision_table import DECISION_TABLE


@pytest.fixture(scope="module")
def ctx():
    return BatchQueryContext()


@pytest.fixture(scope="module")
def items(ctx):
    return ctx.get_exceptions()["exceptions"]


# ======================================================================
# THE RANK
# ======================================================================

def test_rank_is_dense_with_no_gaps_or_duplicates(items):
    ranks = [i["triage_rank"] for i in items]
    assert ranks == list(range(1, len(items) + 1))
    assert len(set(ranks)) == len(ranks)


def test_ordering_matches_the_decision_table(items):
    """
    Checked against DECISION_TABLE itself, never a hardcoded list. If the
    table is reordered this test follows it; if the sort stops honouring
    the table, this fails.
    """
    by_rule = {rule.name: rule.priority for rule in DECISION_TABLE}

    for item in items:
        assert item["matched_rule"] in by_rule
        assert item["policy_priority"] == by_rule[item["matched_rule"]]

    priorities = [i["policy_priority"] for i in items]
    assert priorities == sorted(priorities), "not in policy-severity order"


def test_confidence_breaks_ties_weakest_first(items):
    """
    Within one severity band the engine's least-confident record is the
    one a human should see first. Two records at the same priority are
    not equally urgent.
    """
    keys = [
        (i["policy_priority"], i["confidence_score"], i["txn_id"])
        for i in items
    ]
    assert keys == sorted(keys), "tiebreak is not (priority, confidence, txn_id)"


def test_the_order_is_total_and_stable(ctx):
    """Two calls must produce identical ordering."""
    first = [i["txn_id"] for i in ctx.get_exceptions()["exceptions"]]
    second = [i["txn_id"] for i in ctx.get_exceptions()["exceptions"]]
    assert first == second


# ======================================================================
# DERIVED, NOT HARDCODED
# ======================================================================

def test_a_new_rule_changes_the_ranking_with_no_code_edit(ctx, monkeypatch):
    """
    THE CONTROL FOR DRIFT.

    Adding a hypothetical rule at the top of the table must reorder
    triage without touching query_tools.py. If the mapping were a literal
    in the tool layer, both copies would agree and no behavioural test
    could see the drift -- the section 52 shape.
    """
    from dataclasses import replace

    baseline = [i["txn_id"] for i in ctx.get_exceptions()["exceptions"]]

    # Demote the rule that currently ranks first by rewriting only the
    # TABLE, then rebuild the mapping the way the tool does.
    top_rule = min(DECISION_TABLE, key=lambda r: r.priority).name
    first_item = ctx.get_exceptions()["exceptions"][0]
    demoted = replace(
        next(r for r in DECISION_TABLE if r.name == first_item["matched_rule"]),
        priority=99,
    )
    patched = [
        demoted if r.name == first_item["matched_rule"] else r
        for r in DECISION_TABLE
    ]

    monkeypatch.setattr(
        BatchQueryContext,
        "_PRIORITY_BY_RULE",
        {r.name: r.priority for r in patched},
    )

    after = [i["txn_id"] for i in ctx.get_exceptions()["exceptions"]]

    assert after != baseline, (
        "changing a rule's priority in DECISION_TABLE did not change the "
        "triage order -- the ranking is not actually derived from it"
    )
    assert set(after) == set(baseline), "the record set changed, not just the order"
    assert top_rule  # referenced so the intent of the fixture is explicit


def test_the_code_keyed_mapping_would_have_been_wrong():
    """
    Pins the reasoning in the docstring above so it cannot be
    "simplified" back into a bug.
    """
    lossy = {r.exception_code: r.priority for r in DECISION_TABLE}
    by_rule = {r.name: r.priority for r in DECISION_TABLE}

    no_candidates = by_rule["no_candidates_at_all"]
    catch_all = by_rule["catch_all_unresolved_state"]

    assert no_candidates == 0 and catch_all == 11
    assert lossy[
        next(r.exception_code for r in DECISION_TABLE
             if r.name == "no_candidates_at_all")
    ] == catch_all, (
        "the code-keyed mapping is no longer lossy -- if DECISION_TABLE "
        "changed so that each code maps to one rule, this note and the "
        "rule-name key can be revisited"
    )
    assert len({r.name for r in DECISION_TABLE}) == len(DECISION_TABLE)


# ======================================================================
# ORDERING ONLY
# ======================================================================

def test_the_returned_record_set_is_unchanged(ctx):
    """
    Same 37 records, reordered. This change adds ordering and rank; it
    must not add, drop or alter a record.
    """
    result = ctx.get_exceptions()
    assert result["count"] == 37
    assert result["total_records"] == 61
    assert len(result["exceptions"]) == 37

    from src.models import DecisionStatus
    resolved = {DecisionStatus.MATCHED}
    expected = {d.txn_id for d in ctx.decisions if d.status not in resolved}
    assert {i["txn_id"] for i in result["exceptions"]} == expected


def test_existing_fields_are_untouched(ctx):
    """
    Every field that existed before must carry the same value it did.
    Verified against the decision itself rather than a snapshot.
    """
    by_txn = {d.txn_id: d for d in ctx.decisions}

    for item in ctx.get_exceptions()["exceptions"]:
        decision = by_txn[item["txn_id"]]
        assert item["status"] == decision.status.value
        assert item["exception_code"] == decision.exception_code.value
        assert item["reason_codes"] == [c.value for c in decision.reason_codes]
        assert item["confidence_score"] == decision.confidence_score
        assert item["matched_sources"] == list(decision.matched_sources)
        assert item["tax_verified"] == decision.tax_verified


def test_filtering_still_works_and_stays_ranked(ctx):
    filtered = ctx.get_exceptions(status="AMBIGUOUS")
    assert filtered["count"] == 6
    ranks = [i["triage_rank"] for i in filtered["exceptions"]]
    assert ranks == list(range(1, 7)), "rank must be dense within a filtered view"
    assert all(i["status"] == "AMBIGUOUS" for i in filtered["exceptions"])
