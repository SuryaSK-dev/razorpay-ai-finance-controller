# tests/test_decision_snapshot.py
"""
The 61 decisions this batch produces, pinned to a hash.

THE DEFECT THIS CLOSES
----------------------
Sections 62 through 68 each closed with a sentence of the form "the
decision snapshot is unchanged -- hash `1392ddf1a3c2ea1c`". That hash was
the load-bearing evidence that a refactor had not moved the money, and it
was quoted eight times across FAILURE_LOG.md and ROADMAP.md.

Nothing in the repository computed it. No test, no script, no fixture. It
came from an ad-hoc probe that was run and thrown away, so no reviewer --
including the author -- could recompute it. A search over 1,720,110
candidate recipes (seven hash functions across every subset, separator and
ordering of the decision fields, over both the 61-record and 37-record
sets) reproduced nothing. The original value is unrecoverable.

That is section 63's defect exactly: a claim enforced by something that
could not enforce it. The strongest-stated invariant in the project was
the one with no mechanism.

WHAT CHANGED
------------
The hash is now `d8134bab221d1046`, and it differs from the published one
only because the recipe below is a new one, written down. It is NOT
evidence that the decisions moved -- they did not. Every derived figure is
unchanged and separately asserted elsewhere: 24/61 matched, 37 exceptions,
55/61 status and exception-code accuracy, and all four cash buckets.

`1392ddf1a3c2ea1c` stays in FAILURE_LOG.md sections 63, 65, 66, 67 and
68 as the historical record of runs that happened. A published number
has a tense (section 61.1), and those sentences are about the past.
This file is about the present.

THE RECIPE
----------
Decisions sorted by `txn_id` (unique across the batch, so the order is
total), one line each, newline-joined, UTF-8, blake2b at digest_size=8:

    txn_id|status|exception_code|reason_codes|confidence_score|matched_rule

`matched_rule` is included deliberately. A policy edit that reached the
same status by a different rule is still a change to how this batch was
decided, and a snapshot that could not see it would be quietly narrower
than the sentence it is asked to support.

THE CONTROLS
------------
A pin that cannot fail is decoration. `test_every_field_of_the_recipe_is_
load_bearing` mutates each of the six fields in turn and asserts the hash
moves for every one; three further tests cover a dropped record, an added
record, and reason codes in a different order.
"""

import ast
import copy
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from src.exceptions.manager import decide_batch
from src.ingestion.loader import load_batch
from src.matching.engine import run_matching
from src.models import DecisionStatus, ExceptionCode
from src.normalization.engine import normalize_batch

RAW_DIR = ROOT / "data" / "raw"

SNAPSHOT_HASH = "d8134bab221d1046"
EXPECTED_RECORDS = 61


# ======================================================================
# THE RECIPE
# ======================================================================

def snapshot_line(decision) -> str:
    """One decision, rendered. Every field a decision asserts about money."""
    return "|".join([
        decision.txn_id,
        decision.status.value,
        decision.exception_code.value,
        ",".join(code.value for code in decision.reason_codes),
        str(decision.confidence_score),
        str((decision.evidence or {}).get("matched_rule")),
    ])


def snapshot(decisions) -> str:
    """
    The whole batch, in a canonical order.

    Sorted by `txn_id` rather than left in pipeline order, so that a
    change to iteration order inside the engine does not read as a change
    to the decisions themselves.
    """
    return "\n".join(
        snapshot_line(d) for d in sorted(decisions, key=lambda d: d.txn_id)
    )


def snapshot_hash(decisions) -> str:
    return hashlib.blake2b(
        snapshot(decisions).encode("utf-8"), digest_size=8
    ).hexdigest()


def run_pipeline():
    batch = load_batch(RAW_DIR)
    return decide_batch(run_matching(normalize_batch(batch).records))


@pytest.fixture(scope="module")
def decisions():
    return run_pipeline()


def mutated(decisions, index=0, **changes):
    """A deep copy with one record altered. Never touches the fixture."""
    rows = copy.deepcopy(list(decisions))
    for field, value in changes.items():
        setattr(rows[index], field, value)
    return rows


# ======================================================================
# THE PIN
# ======================================================================

def test_the_decision_snapshot_is_unchanged(decisions):
    """
    THE ASSERTION.

    If this fails, something changed what this batch decides. That is not
    automatically wrong -- but it must be explained and the hash updated
    deliberately, never adjusted to make a run pass.
    """
    assert snapshot_hash(decisions) == SNAPSHOT_HASH, (
        f"decision snapshot moved: {snapshot_hash(decisions)} != "
        f"{SNAPSHOT_HASH}. Diff the snapshot() output against the previous "
        f"commit before touching this constant."
    )


def test_the_snapshot_covers_every_decision_exactly_once(decisions):
    """
    Guards against the pin becoming vacuous. A snapshot over an empty or
    truncated set would hash consistently and assert nothing.
    """
    lines = snapshot(decisions).splitlines()

    assert len(decisions) == EXPECTED_RECORDS
    assert len(lines) == EXPECTED_RECORDS
    assert len({line.split("|")[0] for line in lines}) == EXPECTED_RECORDS


def test_every_line_carries_all_six_fields(decisions):
    for line in snapshot(decisions).splitlines():
        assert len(line.split("|")) == 6, f"malformed snapshot line: {line}"


def test_the_matched_rule_is_present_on_every_record(decisions):
    """
    The rule is part of the recipe, so a batch where it were absent would
    hash 61 copies of "None" and silently lose a sixth of the signal.
    """
    for decision in decisions:
        rule = (decision.evidence or {}).get("matched_rule")
        assert rule, f"{decision.txn_id} carries no matched_rule"


# ======================================================================
# DETERMINISM
# ======================================================================

def test_two_pipeline_runs_produce_the_same_hash():
    assert snapshot_hash(run_pipeline()) == snapshot_hash(run_pipeline())


def test_input_order_does_not_change_the_hash(decisions):
    """
    The sort is what makes the hash a property of the decisions rather
    than of the engine's iteration order.

    Compared against the batch's own hash, not against SNAPSHOT_HASH, so
    that a genuine decision change fails the pin above and this test alone
    keeps reporting on ordering.
    """
    reversed_order = list(reversed(list(decisions)))

    assert snapshot_hash(reversed_order) == snapshot_hash(decisions)


# ======================================================================
# THE CONTROLS -- proof the pin can fail
# ======================================================================

@pytest.mark.parametrize("field,value", [
    ("txn_id", "TXN_99999"),
    ("status", DecisionStatus.HUMAN_REVIEW),
    ("exception_code", ExceptionCode.AMOUNT_MISMATCH),
    ("reason_codes", [ExceptionCode.AMOUNT_MISMATCH]),
    ("confidence_score", 42),
    ("evidence", {"matched_rule": "a_rule_that_does_not_exist"}),
])
def test_every_field_of_the_recipe_is_load_bearing(decisions, field, value):
    """
    THE CONTROL.

    Each of the six fields is changed on one record and the hash must
    move. A field that could change without moving the hash is a field the
    snapshot does not actually cover, and the sentence "the decisions are
    unchanged" would be that much weaker than it sounds.
    """
    first = sorted(decisions, key=lambda d: d.txn_id)[0]
    assert getattr(first, field) != value, (
        f"{field} already equals the mutation value -- this control would "
        f"pass vacuously"
    )

    assert snapshot_hash(
        mutated(decisions, 0, **{field: value})
    ) != SNAPSHOT_HASH


def test_a_dropped_decision_moves_the_hash(decisions):
    assert snapshot_hash(list(decisions)[1:]) != SNAPSHOT_HASH


def test_an_added_decision_moves_the_hash(decisions):
    extra = copy.deepcopy(list(decisions)[0])
    extra.txn_id = "TXN_00099"

    assert snapshot_hash(list(decisions) + [extra]) != SNAPSHOT_HASH


def test_reordering_reason_codes_moves_the_hash(decisions):
    """
    Reason codes are a sequence, not a set. `decide()` emits them in
    priority order and that order is information -- the primary violation
    comes first (section 4).
    """
    multi = [d for d in decisions if len(d.reason_codes) > 1]
    assert multi, "no record carries multiple reason codes -- control is vacuous"

    index = list(decisions).index(multi[0])
    flipped = list(reversed(multi[0].reason_codes))

    assert snapshot_hash(
        mutated(decisions, index, reason_codes=flipped)
    ) != SNAPSHOT_HASH


# ======================================================================
# BOUNDARY
# ======================================================================

def test_this_module_reaches_only_for_the_pipeline():
    """
    The hash must be a property of what the engine decided, not of what it
    was supposed to decide. A snapshot that reached for the labels would
    pin the target instead of the result, and would then agree with itself
    no matter what the engine did.

    `test_architecture_boundary.py` enforces this across `src/`. It does
    not cover `tests/`, and this is the file in `tests/` that would be
    tempted, so the check is repeated here over this module's own imports.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)

    forbidden = sorted(
        name for name in imported
        if "ground" in name.lower() or "accuracy" in name.lower()
    )

    assert not forbidden, (
        f"the snapshot module imports from the evaluation layer: {forbidden}"
    )
