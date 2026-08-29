"""
Phase 5C.5.2 — Deterministic gold baseline integrity tests.

No Gemini/API calls.

The tests prove:

1. The frozen 5C.5.1 benchmark is deterministic.
2. The existing reconciliation pipeline reproduces it.
3. No evaluation-only ground truth is required.
4. Every frozen case has an exact deterministic counterpart, OR is
   explicitly declared not evaluable by per-case execution.
5. No unexpected deterministic decisions appear.
6. The batch-relational exclusion cannot hide a real divergence.

Scope note
----------
run_e2e_deterministic.py executes each case in isolation. Properties
that depend on other records existing in the same batch -- ambiguity
and duplicate detection -- cannot be observed in a batch of one, so
the verifier reports them as NOT_EVALUABLE_PER_CASE rather than as
engine divergence. Those categories are covered by the full-batch
path in tests/test_matching.py instead.

test_exclusion_cannot_hide_divergence below is the guard that keeps
that exclusion honest.
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
    Divergence among EVALUABLE cases must be zero.

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
    Every case is either an exact match or explicitly declared not
    evaluable by per-case execution. Nothing else is acceptable.
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
            # A not-evaluable case must be genuinely divergent --
            # otherwise it should have been recorded as a MATCH.
            assert result["match"] is False
            assert result["not_evaluable"] is True


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
        == coverage["frozen_cases"]
    )


def test_exclusion_cannot_hide_divergence() -> None:
    """
    The batch-relational exclusion must be arithmetically transparent.

    raw_divergent_cases is the mismatch count BEFORE exclusion. If it
    ever exceeds divergent_cases + not_evaluable_cases, the exclusion
    is absorbing something it should not.
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
    )


def test_only_declared_categories_are_excluded() -> None:
    """
    Exclusion is permitted ONLY for the categories the verifier
    declares as batch-relational. A future edit cannot quietly widen
    the exclusion to silence an inconvenient failure.
    """

    report = load_json(
        VERIFICATION_PATH
    )

    declared = set(
        report["scope"]["batch_relational_categories"]
    )

    for result in report["case_results"]:
        if result.get("not_evaluable"):
            assert result["category"] in declared, (
                f"{result['case_id']} excluded as not evaluable "
                f"but its category {result['category']!r} is not "
                f"declared batch-relational {sorted(declared)}"
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
    test_ai_has_no_authority()

    print(
        "5C.5.2 deterministic gold baseline integrity tests passed."
    )


if __name__ == "__main__":
    main()