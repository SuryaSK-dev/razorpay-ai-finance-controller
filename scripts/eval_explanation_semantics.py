"""
Phase 5C.4.4b — Offline semantic scoring of captured Gemini outputs.

This stage NEVER calls Gemini.

Inputs:
    data/eval/held_out_explanations.json
    data/eval/real_gemini_explanation_run_5C4.json

Output:
    data/eval/explanation_semantic_scores_5C4_4b.json

Architecture invariant:

    deterministic fact pack
            |
            +----> frozen benchmark
            |
            +----> captured Gemini explanation
                         |
                         v
                 deterministic scorer
                         |
                         v
                    evaluation only

The Gemini output is never authoritative.
The scorer does not modify financial facts.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


from scripts.score_explanation_quality import score_case


DATASET_PATH = (
    ROOT
    / "data"
    / "eval"
    / "held_out_explanations.json"
)

RUN_OUTPUT_PATH = (
    ROOT
    / "data"
    / "eval"
    / "real_gemini_explanation_run_5C4.json"
)

SCORES_OUTPUT_PATH = (
    ROOT
    / "data"
    / "eval"
    / "explanation_semantic_scores_5C4_4b.json"
)


EXPECTED_DATASET_VERSION = "5C.4-v1"
EXPECTED_GENERATION_STAGE = "5C.4.3"


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Required artifact does not exist: {path}"
        )

    if path.stat().st_size == 0:
        raise ValueError(
            f"Required artifact is empty: {path}"
        )

    with path.open(
        "r",
        encoding="utf-8",
    ) as f:
        return json.load(f)


def validate_dataset(
    dataset: dict,
) -> list[dict]:
    """
    Validate the frozen authoritative benchmark.
    """

    if dataset.get("dataset_version") != (
        EXPECTED_DATASET_VERSION
    ):
        raise ValueError(
            "Unexpected explanation dataset version: "
            f"{dataset.get('dataset_version')!r}"
        )

    if dataset.get("authority") != "deterministic":
        raise ValueError(
            "Explanation benchmark must declare "
            "deterministic authority."
        )

    if dataset.get("model_role") != (
        "read_only_explanation"
    ):
        raise ValueError(
            "Explanation benchmark must declare "
            "read_only_explanation model role."
        )

    cases = dataset.get("cases")

    if not isinstance(cases, list) or not cases:
        raise ValueError(
            "Explanation benchmark contains no cases."
        )

    case_ids = [
        case.get("case_id")
        for case in cases
    ]

    if any(
        not isinstance(case_id, str)
        or not case_id
        for case_id in case_ids
    ):
        raise ValueError(
            "Every benchmark case must have a valid case_id."
        )

    if len(case_ids) != len(set(case_ids)):
        raise ValueError(
            "Duplicate case IDs found in benchmark."
        )

    return cases


def validate_generation_artifact(
    artifact: dict,
    expected_case_ids: set[str],
) -> list[dict]:
    """
    Validate the captured 5C.4.3 artifact.

    This prevents partial or mismatched model runs from
    being silently treated as complete evaluations.
    """

    if artifact.get("dataset_version") != (
        EXPECTED_DATASET_VERSION
    ):
        raise ValueError(
            "Captured Gemini artifact uses an unexpected "
            "dataset version."
        )

    if artifact.get("evaluation_stage") != (
        EXPECTED_GENERATION_STAGE
    ):
        raise ValueError(
            "Captured Gemini artifact is not a "
            "5C.4.3 generation artifact."
        )

    if artifact.get("authority") != "deterministic":
        raise ValueError(
            "Captured artifact must preserve "
            "deterministic authority."
        )

    if artifact.get("model_role") != (
        "read_only_explanation"
    ):
        raise ValueError(
            "Captured artifact must preserve "
            "read_only_explanation role."
        )

    cases = artifact.get("cases")

    if not isinstance(cases, list):
        raise ValueError(
            "Captured Gemini artifact has no valid cases list."
        )

    actual_case_ids = [
        case.get("case_id")
        for case in cases
    ]

    if any(
        not isinstance(case_id, str)
        or not case_id
        for case_id in actual_case_ids
    ):
        raise ValueError(
            "Captured artifact contains an invalid case ID."
        )

    if len(actual_case_ids) != len(
        set(actual_case_ids)
    ):
        raise ValueError(
            "Captured artifact contains duplicate case IDs."
        )

    actual_ids = set(actual_case_ids)

    missing = expected_case_ids - actual_ids
    unexpected = actual_ids - expected_case_ids

    if missing:
        raise ValueError(
            "Captured Gemini artifact is incomplete. "
            f"Missing cases: {sorted(missing)}"
        )

    if unexpected:
        raise ValueError(
            "Captured Gemini artifact contains unexpected "
            f"cases: {sorted(unexpected)}"
        )

    evaluated_cases = artifact.get(
        "evaluated_cases"
    )

    if evaluated_cases != len(cases):
        raise ValueError(
            "evaluated_cases does not match actual "
            "captured case count."
        )

    return cases


def normalize_case_map(
    cases: list[dict],
) -> dict[str, dict]:
    return {
        case["case_id"]: case
        for case in cases
    }


def score_captured_case(
    benchmark_case: dict,
    captured_case: dict,
) -> dict[str, Any]:
    """
    Run the deterministic semantic scorer against one
    captured Gemini explanation.
    """

    explanation = captured_case.get(
        "explanation"
    )

    succeeded = bool(
        captured_case.get("succeeded")
    )

    if not succeeded:
        return {
            "case_id": benchmark_case["case_id"],
            "category": benchmark_case["category"],
            "captured": True,
            "model_succeeded": False,
            "scored": False,
            "explanation": None,
            "safety_critical_failure": True,
            "status_preserved": False,
            "reason_codes_preserved": False,
            "evidence_preserved": False,
            "required_amounts_preserved": False,
            "required_tax_preserved": False,
            "unsupported_claims": [],
            "contradictions": [],
            "scoring_error": (
                captured_case.get("error")
                or "Gemini generation failed."
            ),
        }

    if not isinstance(explanation, str):
        return {
            "case_id": benchmark_case["case_id"],
            "category": benchmark_case["category"],
            "captured": True,
            "model_succeeded": True,
            "scored": False,
            "explanation": None,
            "safety_critical_failure": True,
            "status_preserved": False,
            "reason_codes_preserved": False,
            "evidence_preserved": False,
            "required_amounts_preserved": False,
            "required_tax_preserved": False,
            "unsupported_claims": [],
            "contradictions": [],
            "scoring_error": (
                "Successful model call contained "
                "no valid explanation text."
            ),
        }

    try:
        result = score_case(
            benchmark_case,
            explanation,
        )

        return {
            "case_id": benchmark_case["case_id"],
            "category": benchmark_case["category"],
            "captured": True,
            "model_succeeded": True,
            "scored": True,
            "explanation": explanation,
            "safety_critical_failure": (
                result.safety_critical_failure
            ),
            "status_preserved": (
                result.status_preserved
            ),
            "reason_codes_preserved": (
                result.reason_codes_preserved
            ),
            "evidence_preserved": (
                result.evidence_preserved
            ),
            "required_amounts_preserved": (
                result.required_amounts_preserved
            ),
            "required_tax_preserved": (
                result.required_tax_preserved
            ),
            "unsupported_claims": list(
                result.unsupported_claims
            ),
            "contradictions": list(
                result.contradictions
            ),
            "scoring_error": None,
        }

    except Exception as exc:
        return {
            "case_id": benchmark_case["case_id"],
            "category": benchmark_case["category"],
            "captured": True,
            "model_succeeded": True,
            "scored": False,
            "explanation": explanation,
            "safety_critical_failure": True,
            "status_preserved": False,
            "reason_codes_preserved": False,
            "evidence_preserved": False,
            "required_amounts_preserved": False,
            "required_tax_preserved": False,
            "unsupported_claims": [],
            "contradictions": [],
            "scoring_error": (
                f"{type(exc).__name__}: {exc}"
            ),
        }


def calculate_summary(
    results: list[dict],
) -> dict[str, Any]:
    total = len(results)

    scored = sum(
        result["scored"]
        for result in results
    )

    model_successes = sum(
        result["model_succeeded"]
        for result in results
    )

    safety_failures = sum(
        result["safety_critical_failure"]
        for result in results
    )

    status_preserved = sum(
        result["status_preserved"]
        for result in results
        if result["scored"]
    )

    reason_codes_preserved = sum(
        result["reason_codes_preserved"]
        for result in results
        if result["scored"]
    )

    evidence_preserved = sum(
        result["evidence_preserved"]
        for result in results
        if result["scored"]
    )

    amounts_preserved = sum(
        result["required_amounts_preserved"]
        for result in results
        if result["scored"]
    )

    tax_preserved = sum(
        result["required_tax_preserved"]
        for result in results
        if result["scored"]
    )

    unsupported_claim_count = sum(
        len(result["unsupported_claims"])
        for result in results
    )

    contradiction_count = sum(
        len(result["contradictions"])
        for result in results
    )

    scoring_errors = sum(
        result["scoring_error"] is not None
        for result in results
    )

    return {
        "total_cases": total,
        "model_successes": model_successes,
        "scored_cases": scored,
        "scoring_errors": scoring_errors,
        "safety_critical_failures": safety_failures,
        "status_preserved_cases": status_preserved,
        "reason_codes_preserved_cases": (
            reason_codes_preserved
        ),
        "evidence_preserved_cases": (
            evidence_preserved
        ),
        "required_amounts_preserved_cases": (
            amounts_preserved
        ),
        "required_tax_preserved_cases": (
            tax_preserved
        ),
        "unsupported_claim_count": (
            unsupported_claim_count
        ),
        "contradiction_count": (
            contradiction_count
        ),
        "safety_failure_rate": (
            safety_failures / total
            if total
            else 0.0
        ),
    }


def persist_artifact(
    dataset: dict,
    generation_artifact: dict,
    results: list[dict],
) -> None:
    """
    Persist the complete 5C.4.4b deterministic scoring artifact.
    """

    artifact = {
        "evaluation_stage": "5C.4.4b",
        "dataset_version": (
            dataset["dataset_version"]
        ),
        "authority": "deterministic",
        "model_role": "read_only_explanation",
        "source_generation_stage": (
            generation_artifact[
                "evaluation_stage"
            ]
        ),
        "provider": generation_artifact.get(
            "provider"
        ),
        "model": generation_artifact.get(
            "model"
        ),
        "total_cases": len(
            dataset["cases"]
        ),
        "scored_cases": len(results),
        "summary": calculate_summary(results),
        "cases": results,
    }

    SCORES_OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = (
        SCORES_OUTPUT_PATH.with_suffix(
            ".json.tmp"
        )
    )

    temporary_path.write_text(
        json.dumps(
            artifact,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    os.replace(
        temporary_path,
        SCORES_OUTPUT_PATH,
    )


def print_case(result: dict) -> None:
    status = (
        "SAFETY-FAIL"
        if result["safety_critical_failure"]
        else "SCORED"
    )

    print(
        f"{result['case_id']:<6} "
        f"{status:<11} "
        f"{result['category']}"
    )

    if result["unsupported_claims"]:
        print(
            "       unsupported="
            f"{result['unsupported_claims']}"
        )

    if result["contradictions"]:
        print(
            "       contradictions="
            f"{result['contradictions']}"
        )

    if result["scoring_error"]:
        print(
            "       scoring_error="
            f"{result['scoring_error']}"
        )


def main() -> None:
    print("=" * 72)
    print(
        "5C.4.4b OFFLINE GEMINI EXPLANATION SEMANTIC SCORING"
    )
    print("=" * 72)

    dataset = load_json(
        DATASET_PATH
    )

    benchmark_cases = validate_dataset(
        dataset
    )

    generation_artifact = load_json(
        RUN_OUTPUT_PATH
    )

    captured_cases = (
        validate_generation_artifact(
            generation_artifact,
            {
                case["case_id"]
                for case in benchmark_cases
            },
        )
    )

    benchmark_map = normalize_case_map(
        benchmark_cases
    )

    captured_map = normalize_case_map(
        captured_cases
    )

    results: list[dict] = []

    for case_id in sorted(
        benchmark_map
    ):
        benchmark_case = (
            benchmark_map[case_id]
        )

        captured_case = (
            captured_map[case_id]
        )

        result = score_captured_case(
            benchmark_case,
            captured_case,
        )

        results.append(result)

        print_case(result)

    persist_artifact(
        dataset=dataset,
        generation_artifact=generation_artifact,
        results=results,
    )

    summary = calculate_summary(
        results
    )

    print()
    print("=" * 72)
    print(
        "5C.4.4b SEMANTIC SCORING SUMMARY"
    )
    print("=" * 72)

    print(
        f"Total cases:                  "
        f"{summary['total_cases']}"
    )

    print(
        f"Model successes:              "
        f"{summary['model_successes']}"
    )

    print(
        f"Scored cases:                 "
        f"{summary['scored_cases']}"
    )

    print(
        f"Scoring errors:               "
        f"{summary['scoring_errors']}"
    )

    print(
        f"Safety-critical failures:     "
        f"{summary['safety_critical_failures']}"
    )

    print(
        f"Status preserved:             "
        f"{summary['status_preserved_cases']}"
    )

    print(
        f"Reason codes preserved:       "
        f"{summary['reason_codes_preserved_cases']}"
    )

    print(
        f"Evidence preserved:           "
        f"{summary['evidence_preserved_cases']}"
    )

    print(
        f"Amounts preserved:            "
        f"{summary['required_amounts_preserved_cases']}"
    )

    print(
        f"Tax preserved:                "
        f"{summary['required_tax_preserved_cases']}"
    )

    print(
        f"Unsupported claims:           "
        f"{summary['unsupported_claim_count']}"
    )

    print(
        f"Contradictions:               "
        f"{summary['contradiction_count']}"
    )

    print(
        f"Safety failure rate:          "
        f"{summary['safety_failure_rate']:.2%}"
    )

    print()
    print(
        f"Artifact: {SCORES_OUTPUT_PATH}"
    )

    print()
    print(
        "5C.4.4b offline semantic scoring: COMPLETE"
    )


if __name__ == "__main__":
    main()