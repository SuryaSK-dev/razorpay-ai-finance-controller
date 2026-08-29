"""
Phase 5C.5.2 — Deterministic gold baseline integrity tests.

No Gemini/API calls.

The tests prove:

1. The frozen 5C.5.1 benchmark is deterministic.
2. The existing reconciliation pipeline reproduces it.
3. No evaluation-only ground truth is required.
4. Every frozen case has an exact deterministic counterpart.
5. No unexpected deterministic decisions appear.
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
        coverage["missing_actual_cases"]
        == 0
    )

    assert (
        coverage["unexpected_actual_decisions"]
        == 0
    )


def test_all_cases_match() -> None:
    report = load_json(
        VERIFICATION_PATH
    )

    results = report[
        "case_results"
    ]

    assert results

    for result in results:
        assert (
            result["match"]
            is True
        )
        assert (
            result["status"]
            == "MATCH"
        )
        assert (
            result["differences"]
            == {}
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
        == coverage["frozen_cases"]
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
    test_ai_has_no_authority()

    print(
        "5C.5.2 deterministic gold baseline integrity tests passed."
    )


if __name__ == "__main__":
    main()