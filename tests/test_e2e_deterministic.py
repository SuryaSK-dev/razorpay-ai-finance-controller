# tests/test_e2e_deterministic.py
"""
Phase 5C.5.3 deterministic E2E execution integrity tests.

These tests verify that the 5C.5.3 artifact:

1. exists,
2. was generated from the frozen 5C.5.1 benchmark,
3. contains exactly the same case IDs,
4. executed every case successfully,
5. contains deterministic decisions,
6. did not invoke an LLM,
7. did not perform gold comparison.

This test deliberately does NOT assert that actual decisions equal
the frozen gold decisions. That comparison belongs to the next
checkpoint.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

BENCHMARK_PATH = (
    ROOT
    / "data"
    / "eval"
    / "e2e_reconciliation_benchmark_5C5_1.json"
)

RESULT_PATH = (
    ROOT
    / "data"
    / "eval"
    / "e2e_deterministic_results_5C5_3.json"
)

EXPECTED_BENCHMARK_VERSION = "5C.5-v1"
EXPECTED_STAGE = "5C.5.3"


def load_json(path: Path) -> dict:
    assert path.exists(), (
        f"Missing artifact: {path}"
    )

    with path.open(
        "r",
        encoding="utf-8",
    ) as handle:
        data = json.load(handle)

    assert isinstance(data, dict)

    return data


def test_benchmark_contract() -> None:
    benchmark = load_json(
        BENCHMARK_PATH
    )

    assert (
        benchmark["dataset_version"]
        == EXPECTED_BENCHMARK_VERSION
    )

    cases = benchmark["cases"]

    assert isinstance(cases, list)
    assert len(cases) == 63


def test_result_metadata() -> None:
    result = load_json(
        RESULT_PATH
    )

    assert (
        result["report_version"]
        == "5C.5.3-v1"
    )

    assert (
        result["evaluation_stage"]
        == EXPECTED_STAGE
    )

    assert (
        result["authority"]
        == "deterministic"
    )

    assert (
        result["model_role"]
        == "read_only_candidate_and_explanation"
    )

    assert (
        result["ai_boundary"]["llm_invoked"]
        is False
    )

    assert (
        result["ai_boundary"]["ai_authority"]
        == "none"
    )

    assert (
        result["ai_boundary"]["financial_decision_authority"]
        == "deterministic"
    )


def test_exact_case_alignment() -> None:
    benchmark = load_json(
        BENCHMARK_PATH
    )

    result = load_json(
        RESULT_PATH
    )

    expected_ids = [
        case["case_id"]
        for case in benchmark["cases"]
    ]

    actual_ids = [
        case["case_id"]
        for case in result["cases"]
    ]

    assert actual_ids == expected_ids


def test_all_cases_executed_successfully() -> None:
    result = load_json(
        RESULT_PATH
    )

    cases = result["cases"]

    assert len(cases) == 63

    assert result["execution"]["total_cases"] == 63

    assert (
        result["execution"]["successful_cases"]
        == 63
    )

    assert (
        result["execution"]["execution_errors"]
        == 0
    )

    assert (
        result["execution"]["success_rate_percent"]
        == 100.0
    )

    for case in cases:
        assert case["status"] == "EXECUTED"
        assert case["error"] is None


def test_every_case_contains_deterministic_decision() -> None:
    result = load_json(
        RESULT_PATH
    )

    for case in result["cases"]:
        decision = case[
            "deterministic_decision"
        ]

        assert isinstance(
            decision,
            dict,
        )

        assert decision["txn_id"] == (
            case["source_transaction_id"]
        )

        assert "status" in decision
        assert "confidence_score" in decision
        assert "exception_code" in decision
        assert "reason_codes" in decision
        assert "matched_sources" in decision
        assert "evidence" in decision


def test_no_gold_comparison_was_performed() -> None:
    result = load_json(
        RESULT_PATH
    )

    comparison = result[
        "comparison"
    ]

    assert comparison["performed"] is False

    assert "gold comparison" in (
        comparison["reason"]
    )


def test_case_ids_are_unique() -> None:
    result = load_json(
        RESULT_PATH
    )

    ids = [
        case["case_id"]
        for case in result["cases"]
    ]

    assert len(ids) == len(set(ids))


if __name__ == "__main__":
    test_benchmark_contract()
    test_result_metadata()
    test_exact_case_alignment()
    test_all_cases_executed_successfully()
    test_every_case_contains_deterministic_decision()
    test_no_gold_comparison_was_performed()
    test_case_ids_are_unique()

    print(
        "5C.5.3 deterministic E2E execution "
        "integrity tests passed."
    )