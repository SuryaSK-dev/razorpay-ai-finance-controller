"""
Phase 5C.5.1 — Frozen E2E benchmark integrity tests.

No Gemini/API calls.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BENCHMARK_PATH = (
    ROOT
    / "data"
    / "eval"
    / "e2e_reconciliation_benchmark_5C5_1.json"
)

EXPECTED_VERSION = "5C.5-v1"
EXPECTED_STAGE = "5C.5.1"


def load_benchmark() -> dict:
    assert BENCHMARK_PATH.exists(), (
        f"Missing benchmark artifact: "
        f"{BENCHMARK_PATH}"
    )

    with BENCHMARK_PATH.open(
        "r",
        encoding="utf-8",
    ) as handle:
        data = json.load(handle)

    assert isinstance(data, dict)
    return data


def test_metadata() -> None:
    data = load_benchmark()

    assert data["dataset_version"] == (
        EXPECTED_VERSION
    )

    assert data["evaluation_stage"] == (
        EXPECTED_STAGE
    )

    assert data["authority"] == (
        "deterministic"
    )

    assert data["model_role"] == (
        "read_only_candidate_and_explanation"
    )


def test_cases_exist() -> None:
    data = load_benchmark()

    cases = data["cases"]

    assert isinstance(cases, list)
    assert cases

    assert data["case_count"] == len(cases)


def test_case_ids_unique() -> None:
    data = load_benchmark()

    cases = data["cases"]

    ids = [
        case["case_id"]
        for case in cases
    ]

    assert len(ids) == len(set(ids))


def test_source_transaction_ids_unique() -> None:
    data = load_benchmark()

    cases = data["cases"]

    ids = [
        case["source_transaction_id"]
        for case in cases
    ]

    assert len(ids) == len(set(ids))


def test_required_fields() -> None:
    data = load_benchmark()

    required = {
        "case_id",
        "category",
        "input_transaction",
        "narration",
        "deterministic_expected_decision",
        "expected_reason_codes",
        "expected_financial_facts",
    }

    for case in data["cases"]:
        assert required.issubset(case.keys())


def test_input_transaction_has_sources() -> None:
    data = load_benchmark()

    for case in data["cases"]:
        source = case["input_transaction"]

        assert isinstance(source, dict)

        assert "pg" in source
        assert "bank" in source
        assert "invoice" in source

        assert isinstance(
            source["pg"],
            list,
        )

        assert isinstance(
            source["bank"],
            list,
        )

        assert isinstance(
            source["invoice"],
            list,
        )


def test_deterministic_authority_is_structured() -> None:
    data = load_benchmark()

    for case in data["cases"]:
        decision = (
            case[
                "deterministic_expected_decision"
            ]
        )

        assert isinstance(
            decision,
            dict,
        )

        assert decision


def test_reason_codes_are_structured() -> None:
    data = load_benchmark()

    for case in data["cases"]:
        reason_codes = (
            case["expected_reason_codes"]
        )

        assert isinstance(
            reason_codes,
            list,
        )

        for code in reason_codes:
            assert isinstance(
                code,
                str,
            )


def test_financial_facts_are_structured() -> None:
    data = load_benchmark()

    for case in data["cases"]:
        facts = (
            case[
                "expected_financial_facts"
            ]
        )

        assert isinstance(
            facts,
            dict,
        )


def test_narration_is_real_input() -> None:
    """
    The benchmark must preserve the source narration.

    It must NOT manufacture narration by inserting the known
    transaction ID into a template.
    """

    data = load_benchmark()

    for case in data["cases"]:
        narration = case["narration"]

        assert isinstance(
            narration,
            str,
        )

        source_bank = (
            case["input_transaction"]["bank"]
        )

        if source_bank:
            source_narrations = []

            for bank in source_bank:
                for field in (
                    "narration",
                    "description",
                ):
                    if bank.get(field):
                        source_narrations.append(
                            str(bank[field]).strip()
                        )

            if source_narrations:
                assert narration in (
                    source_narrations
                )


def test_ai_has_no_authority_field() -> None:
    """
    The frozen benchmark must not contain an AI decision.

    AI outputs are generated later by 5C.5.3.
    """

    data = load_benchmark()

    forbidden = {
        "ai_decision",
        "ai_final_decision",
        "llm_decision",
        "model_decision",
        "ai_status",
        "ai_exception_code",
    }

    for case in data["cases"]:
        assert not (
            forbidden
            & set(case.keys())
        )


def test_ground_truth_is_not_inside_ai_input() -> None:
    """
    Ground truth is evaluation authority, not model input.
    """

    data = load_benchmark()

    for case in data["cases"]:
        serialized = json.dumps(
            case["input_transaction"]
        ).lower()

        assert (
            "expected_status"
            not in serialized
        )

        assert (
            "expected_exception_code"
            not in serialized
        )

        assert (
            "expected_reason_codes"
            not in serialized
        )

        assert (
            "expected_financial_facts"
            not in serialized
        )


def test_no_financial_mutation_contract() -> None:
    """
    Benchmark cases must contain source data and expected facts,
    but no mutable AI financial output.
    """

    data = load_benchmark()

    for case in data["cases"]:
        assert (
            "final_decision"
            not in case
        )

        assert (
            "ai_assisted_decision"
            not in case
        )

        assert (
            "override_decision"
            not in case
        )


def main() -> None:
    test_metadata()
    test_cases_exist()
    test_case_ids_unique()
    test_source_transaction_ids_unique()
    test_required_fields()
    test_input_transaction_has_sources()
    test_deterministic_authority_is_structured()
    test_reason_codes_are_structured()
    test_financial_facts_are_structured()
    test_narration_is_real_input()
    test_ai_has_no_authority_field()
    test_ground_truth_is_not_inside_ai_input()
    test_no_financial_mutation_contract()

    print(
        "5C.5.1 frozen E2E benchmark integrity "
        "tests passed."
    )


if __name__ == "__main__":
    main()