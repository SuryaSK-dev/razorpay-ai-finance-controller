"""
Phase 5C.4.5 — Explanation faithfulness report integrity tests.

No Gemini/API calls.

These tests verify the reporting layer itself:
    - deterministic input validation
    - case coverage integrity
    - no duplicate cases
    - safety classification
    - quality-vs-safety separation
    - report structure
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


from scripts.report_explanation_faithfulness import (
    build_report,
    is_safety_critical,
    is_semantically_faithful,
    validate_dataset,
    validate_generation_artifact,
    validate_semantic_artifact,
)


def make_dataset() -> dict:
    return {
        "dataset_version": "5C.4-v1",
        "authority": "deterministic",
        "model_role": "read_only_explanation",
        "cases": [
            {
                "case_id": "E001",
                "category": "gst_mismatch_review",
                "facts": {},
            },
            {
                "case_id": "E002",
                "category": "exact_match",
                "facts": {},
            },
        ],
    }


def make_generation() -> dict:
    return {
        "dataset_version": "5C.4-v1",
        "evaluation_stage": "5C.4.3",
        "cases": [
            {
                "case_id": "E001",
                "succeeded": True,
                "explanation": "GST mismatch is under review.",
            },
            {
                "case_id": "E002",
                "succeeded": True,
                "explanation": "All deterministic checks passed.",
            },
        ],
    }


def make_semantic() -> dict:
    return {
        "dataset_version": "5C.4-v1",
        "evaluation_stage": "5C.4.4b",
        "cases": [
            {
                "case_id": "E001",
                "category": "gst_mismatch_review",
                "status_preserved": True,
                "required_amounts_preserved": True,
                "required_tax_preserved": True,
                "confidence_preserved": True,
                "reason_codes_preserved": True,
                "evidence_preserved": True,
                "unsupported_claims": [],
                "contradictions": [],
                "missing_material_facts": [],
                "score": 100.0,
            },
            {
                "case_id": "E002",
                "category": "exact_match",
                "status_preserved": True,
                "required_amounts_preserved": True,
                "required_tax_preserved": True,
                "confidence_preserved": True,
                "reason_codes_preserved": False,
                "evidence_preserved": False,
                "unsupported_claims": [],
                "contradictions": [],
                "missing_material_facts": [
                    "transaction reference evidence"
                ],
                "score": 70.0,
            },
        ],
    }


def test_dataset_contract() -> None:
    cases = validate_dataset(
        make_dataset()
    )

    assert len(cases) == 2


def test_generation_contract() -> None:
    cases = validate_generation_artifact(
        make_generation()
    )

    assert len(cases) == 2


def test_semantic_contract() -> None:
    cases = validate_semantic_artifact(
        make_semantic()
    )

    assert len(cases) == 2


def test_safety_critical_definition() -> None:
    safe = {
        "contradictions": [],
        "unsupported_claims": [],
    }

    unsafe = {
        "contradictions": [
            "Status contradiction"
        ],
        "unsupported_claims": [],
    }

    unsupported = {
        "contradictions": [],
        "unsupported_claims": [
            "Unsupported financial value: 999"
        ],
    }

    assert is_safety_critical(
        safe
    ) is False

    assert is_safety_critical(
        unsafe
    ) is True

    assert is_safety_critical(
        unsupported
    ) is True


def test_quality_gap_is_not_safety_failure() -> None:
    case = {
        "contradictions": [],
        "unsupported_claims": [],
        "missing_material_facts": [
            "transaction reference"
        ],
    }

    assert is_safety_critical(
        case
    ) is False

    assert is_semantically_faithful(
        case
    ) is False


def test_fully_faithful_case() -> None:
    case = {
        "contradictions": [],
        "unsupported_claims": [],
        "missing_material_facts": [],
    }

    assert is_safety_critical(
        case
    ) is False

    assert is_semantically_faithful(
        case
    ) is True


def test_report_recomputes_safety_from_cases() -> None:
    dataset = make_dataset()
    generation = make_generation()
    semantic = make_semantic()

    report = build_report(
        dataset,
        generation,
        semantic,
    )

    assert (
        report["coverage"]["dataset_cases"]
        == 2
    )

    assert (
        report["safety"]["safety_critical_failures"]
        == 0
    )

    assert (
        report["faithfulness"]["status_preserved"]["cases"]
        == 2
    )

    assert (
        report["faithfulness"]["amounts_preserved"]["cases"]
        == 2
    )

    assert (
        report["quality_gaps"]["reason_code_semantic_gap_cases"]
        == ["E002"]
    )

    assert (
        report["quality_gaps"]["evidence_semantic_gap_cases"]
        == ["E002"]
    )


def test_case_alignment_mismatch_is_rejected() -> None:
    dataset = make_dataset()
    generation = make_generation()
    semantic = make_semantic()

    broken_generation = copy.deepcopy(
        generation
    )

    broken_generation["cases"][1]["case_id"] = (
        "UNKNOWN"
    )

    try:
        build_report(
            dataset,
            broken_generation,
            semantic,
        )
    except ValueError:
        return

    raise AssertionError(
        "Case alignment mismatch was not rejected."
    )


def test_duplicate_semantic_cases_are_rejected() -> None:
    semantic = make_semantic()

    semantic["cases"].append(
        copy.deepcopy(
            semantic["cases"][0]
        )
    )

    try:
        validate_semantic_artifact(
            semantic
        )
    except ValueError:
        return

    raise AssertionError(
        "Duplicate semantic case IDs were not rejected."
    )


def main() -> None:
    test_dataset_contract()
    test_generation_contract()
    test_semantic_contract()

    test_safety_critical_definition()
    test_quality_gap_is_not_safety_failure()
    test_fully_faithful_case()

    test_report_recomputes_safety_from_cases()
    test_case_alignment_mismatch_is_rejected()
    test_duplicate_semantic_cases_are_rejected()

    print(
        "5C.4.5 explanation faithfulness report "
        "integrity tests passed."
    )


if __name__ == "__main__":
    main()