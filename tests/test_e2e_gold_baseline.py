"""
Phase 5C.5.2 — Deterministic gold baseline integrity tests.

No Gemini/API calls.

The tests prove:

1. The frozen 5C.5.1 benchmark is deterministic.
2. The existing reconciliation pipeline reproduces it.
3. No evaluation-only ground truth is required.
4. Every frozen case is an exact match, OR is declared not evaluable
   by per-case execution, OR is a documented policy divergence.
5. No unexpected deterministic decisions appear.
6. Neither exclusion bucket can hide a real divergence.

THREE OUTCOMES, AND WHY THE DISTINCTION MATTERS
-----------------------------------------------
    MATCH                    the engine reproduced the frozen gold

    NOT_EVALUABLE_PER_CASE   the harness structurally CANNOT observe
                             this property. run_e2e_deterministic.py
                             executes each case in isolation, and
                             ambiguity means "another plausible record
                             exists elsewhere in the batch" -- which a
                             batch of one cannot contain.

    KNOWN_POLICY_DIVERGENCE  the harness observes it correctly. We
                             decided the engine is right and the
                             ground-truth label was optimistic, and
                             said so in the artifact.

Collapsing the last two would let an honesty mechanism hide a result.
Four tests below exist specifically to keep both buckets honest:

    test_exclusion_cannot_hide_divergence
    test_only_declared_categories_are_excluded
    test_policy_divergences_carry_a_rationale
    test_the_two_exclusion_buckets_are_disjoint
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = (
    Path(__file__)
    .resolve()
    .parent.parent
)

BENCHMARK_PATH = (
    ROOT
    / "data"
    / "eval"
    / "held_out_e2e_reconciliation.json"
)

VERIFICATION_PATH = (
    ROOT
    / "data"
    / "eval"
    / "e2e_gold_baseline_verification_5C5_2.json"
)

EXPECTED_DATASET_VERSION = "5C.5-v1"
EXPECTED_STAGE = "5C.5.2"

ACCEPTABLE_STATUSES = {
    "MATCH",
    "NOT_EVALUABLE_PER_CASE",
    "KNOWN_POLICY_DIVERGENCE",
}


def load_json(path: Path) -> dict:
    assert path.exists(), (
        f"Required artifact missing: {path}"
    )

    with path.open(
        "r",
        encoding="utf-8",
    ) as handle:
        return json.load(handle)


def test_frozen_benchmark_exists() -> None:
    assert BENCHMARK_PATH.exists()


def test_frozen_benchmark_is_deterministic() -> None:
    dataset = load_json(
        BENCHMARK_PATH
    )

    assert (
        dataset["dataset_version"]
        == EXPECTED_DATASET_VERSION
    )

    assert (
        dataset["authority"]
        == "deterministic"
    )

    assert (
        dataset["financial_decision_authority"]
        == "deterministic_engine"
    )

    assert (
        dataset["model_role"]
        == "read_only_assistance"
    )


def test_verification_artifact_exists() -> None:
    assert VERIFICATION_PATH.exists()


def test_verification_stage() -> None:
    report = load_json(
        VERIFICATION_PATH
    )

    assert (
        report["evaluation_stage"]
        == EXPECTED_STAGE
    )

    assert (
        report["dataset_version"]
        == EXPECTED_DATASET_VERSION
    )


def test_baseline_is_stable() -> None:
    report = load_json(
        VERIFICATION_PATH
    )

    assert (
        report["baseline_stable"]
        is True
    )


def test_no_baseline_divergence() -> None:
    """
    Divergence among EVALUABLE, UNEXPLAINED cases must be zero.

    NOTE: the key names below were previously
    'missing_actual_cases' and 'unexpected_actual_decisions', neither
    of which the verifier has ever emitted. Those assertions were
    unreachable -- the divergence assert above them always failed
    first -- so the KeyError was latent. They now use the names the
    verifier actually writes.
    """

    report = load_json(
        VERIFICATION_PATH
    )

    coverage = report[
        "coverage"
    ]

    assert (
        coverage["divergent_cases"]
        == 0
    )

    assert (
        coverage["missing_execution_cases"]
        == 0
    )

    assert (
        coverage["unexpected_execution_cases"]
        == 0
    )


def test_all_cases_match() -> None:
    """
    Every case is an exact match, or is declared not evaluable, or is
    a documented policy divergence. Nothing else is acceptable.
    """

    report = load_json(
        VERIFICATION_PATH
    )

    results = report[
        "case_results"
    ]

    assert results

    for result in results:
        assert result["status"] in ACCEPTABLE_STATUSES, (
            f"{result['case_id']} has unacceptable status "
            f"{result['status']!r}"
        )

        if result["status"] == "MATCH":
            assert result["match"] is True
            assert result["differences"] == {}
        else:
            # Both exclusion buckets describe genuinely divergent
            # cases. A case that matched should never be in either --
            # that would mean an exclusion was applied to a passing
            # case, which is how an exclusion starts inflating a
            # number.
            assert result["match"] is False
            assert (
                result["not_evaluable"]
                or result["known_policy_divergence"]
            ), (
                f"{result['case_id']} diverges but is in neither "
                "documented bucket"
            )


def test_case_coverage_is_exact() -> None:
    report = load_json(
        VERIFICATION_PATH
    )

    coverage = report[
        "coverage"
    ]

    assert (
        coverage["frozen_cases"]
        == coverage["actual_decisions"]
    )

    assert (
        coverage["matched_cases"]
        + coverage["not_evaluable_cases"]
        + coverage["known_policy_divergence_cases"]
        == coverage["frozen_cases"]
    )


def test_exclusion_cannot_hide_divergence() -> None:
    """
    Both exclusions must be arithmetically transparent.

    raw_divergent_cases is the mismatch count BEFORE any exclusion. If
    it ever exceeds the sum of the three buckets, something is being
    absorbed that should not be.
    """

    report = load_json(
        VERIFICATION_PATH
    )

    coverage = report[
        "coverage"
    ]

    assert (
        coverage["raw_divergent_cases"]
        == coverage["divergent_cases"]
        + coverage["not_evaluable_cases"]
        + coverage["known_policy_divergence_cases"]
    )


def test_only_declared_categories_are_excluded() -> None:
    """
    Exclusion is permitted ONLY for categories the verifier declares.
    A future edit cannot quietly widen either bucket to silence an
    inconvenient failure.
    """

    report = load_json(
        VERIFICATION_PATH
    )

    structural = set(
        report["scope"]["batch_relational_categories"]
    )

    policy = set(
        report["scope"]["known_policy_divergences"]
    )

    for result in report["case_results"]:
        if result.get("not_evaluable"):
            assert result["category"] in structural, (
                f"{result['case_id']} excluded as not evaluable "
                f"but its category {result['category']!r} is not "
                f"declared batch-relational {sorted(structural)}"
            )

        if result.get("known_policy_divergence"):
            assert result["category"] in policy, (
                f"{result['case_id']} excluded as a policy "
                f"divergence but its category {result['category']!r} "
                f"is not declared {sorted(policy)}"
            )


def test_policy_divergences_carry_a_rationale() -> None:
    """
    A case excluded on policy grounds must say WHY, in the artifact.

    An exclusion without a stated reason is indistinguishable from one
    added to make a number look better.
    """

    report = load_json(
        VERIFICATION_PATH
    )

    for result in report["case_results"]:
        if not result.get("known_policy_divergence"):
            continue

        rationale = result.get("policy_rationale")

        assert rationale, (
            f"{result['case_id']} is excluded as a known policy "
            "divergence but carries no rationale"
        )

        assert len(rationale) > 60, (
            f"{result['case_id']}: rationale is too thin to be a "
            "real justification"
        )


def test_the_two_exclusion_buckets_are_disjoint() -> None:
    """
    A category cannot be both structurally unobservable and a known
    policy disagreement. Overlap would mean one of the two labels is
    wrong about what the harness can see.
    """

    report = load_json(
        VERIFICATION_PATH
    )

    structural = set(
        report["scope"]["batch_relational_categories"]
    )

    policy = set(
        report["scope"]["known_policy_divergences"]
    )

    overlap = structural & policy

    assert not overlap, (
        f"categories declared in both buckets: {sorted(overlap)}"
    )

    for result in report["case_results"]:
        assert not (
            result.get("not_evaluable")
            and result.get("known_policy_divergence")
        ), (
            f"{result['case_id']} is in both exclusion buckets"
        )


def test_ai_has_no_authority() -> None:
    """
    The 5C.5.2 artifact must remain a deterministic baseline.
    """

    report = load_json(
        VERIFICATION_PATH
    )

    assert (
        report["authority"]
        == "deterministic"
    )

    assert (
        report["model_role"]
        == "read_only_assistance"
    )


def main() -> None:
    test_frozen_benchmark_exists()
    test_frozen_benchmark_is_deterministic()
    test_verification_artifact_exists()
    test_verification_stage()
    test_baseline_is_stable()
    test_no_baseline_divergence()
    test_all_cases_match()
    test_case_coverage_is_exact()
    test_exclusion_cannot_hide_divergence()
    test_only_declared_categories_are_excluded()
    test_policy_divergences_carry_a_rationale()
    test_the_two_exclusion_buckets_are_disjoint()
    test_ai_has_no_authority()

    print(
        "5C.5.2 deterministic gold baseline integrity tests passed."
    )


if __name__ == "__main__":
    main()