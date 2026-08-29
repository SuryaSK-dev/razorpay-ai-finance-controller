"""
Phase 5C.4.5 — Explanation quality / faithfulness report.

This module is intentionally OFFLINE.

It consumes three frozen/evaluated artifacts:

    data/eval/held_out_explanations.json
    data/eval/real_gemini_explanation_run_5C4.json
    data/eval/explanation_semantic_scores_5C4_4b.json

It does NOT:
    - call Gemini
    - call any provider
    - modify model outputs
    - reinterpret financial facts
    - create new semantic aliases
    - silently repair failed evaluations

Its job is to produce an auditable report from already-generated
evaluation evidence.

Authority:
    deterministic fact pack

Model role:
    read-only explanation

Safety-critical:
    contradictions
    unsupported financial claims

Quality-only:
    missing material facts
    missing reason-code semantic coverage
    missing evidence semantic coverage

The report is NOT a production certification.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent

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

SEMANTIC_SCORE_PATH = (
    ROOT
    / "data"
    / "eval"
    / "explanation_semantic_scores_5C4_4b.json"
)

REPORT_PATH = (
    ROOT
    / "data"
    / "eval"
    / "explanation_faithfulness_report_5C4_5.json"
)

EXPECTED_DATASET_VERSION = "5C.4-v1"
EXPECTED_GENERATION_STAGE = "5C.4.3"
EXPECTED_SCORING_STAGE = "5C.4.4b"
EXPECTED_REPORT_STAGE = "5C.4.5"


# =====================================================================
# JSON
# =====================================================================

def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Required evaluation artifact does not exist: {path}"
        )

    if path.stat().st_size == 0:
        raise ValueError(
            f"Required evaluation artifact is empty: {path}"
        )

    with path.open(
        "r",
        encoding="utf-8",
    ) as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(
            f"Expected JSON object at {path}, "
            f"got {type(data).__name__}."
        )

    return data


def write_json(
    data: dict[str, Any],
    path: Path,
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    with temporary_path.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False,
        )
        f.write("\n")

    temporary_path.replace(path)


# =====================================================================
# BASIC VALIDATION
# =====================================================================

def require_string(
    value: Any,
    field: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"Required field '{field}' must be a "
            "non-empty string."
        )

    return value


def require_bool(
    value: Any,
    field: str,
) -> bool:
    if not isinstance(value, bool):
        raise ValueError(
            f"Required field '{field}' must be boolean."
        )

    return value


def require_list(
    value: Any,
    field: str,
) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(
            f"Required field '{field}' must be a list."
        )

    return value


# =====================================================================
# CASE-ID VALIDATION
# =====================================================================

def unique_case_ids(
    cases: list[dict[str, Any]],
    artifact_name: str,
) -> list[str]:
    ids: list[str] = []

    for item in cases:
        if not isinstance(item, dict):
            raise ValueError(
                f"{artifact_name}: every case must be an object."
            )

        case_id = require_string(
            item.get("case_id"),
            f"{artifact_name}.case_id",
        )

        ids.append(case_id)

    duplicates = sorted(
        {
            case_id
            for case_id in ids
            if ids.count(case_id) > 1
        }
    )

    if duplicates:
        raise ValueError(
            f"{artifact_name}: duplicate case IDs: "
            f"{duplicates}"
        )

    return ids


def assert_same_case_ids(
    expected: set[str],
    actual: set[str],
    source_name: str,
) -> None:
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)

    if missing or extra:
        parts: list[str] = []

        if missing:
            parts.append(
                f"missing={missing}"
            )

        if extra:
            parts.append(
                f"unexpected={extra}"
            )

        raise ValueError(
            f"Case coverage mismatch for {source_name}: "
            + ", ".join(parts)
        )


# =====================================================================
# DATASET
# =====================================================================

def validate_dataset(
    dataset: dict[str, Any],
) -> list[dict[str, Any]]:
    if dataset.get("dataset_version") != (
        EXPECTED_DATASET_VERSION
    ):
        raise ValueError(
            "Held-out explanation dataset must use "
            f"{EXPECTED_DATASET_VERSION}."
        )

    if dataset.get("authority") != "deterministic":
        raise ValueError(
            "Explanation dataset authority must remain "
            "'deterministic'."
        )

    if dataset.get("model_role") != (
        "read_only_explanation"
    ):
        raise ValueError(
            "Explanation dataset model_role must remain "
            "'read_only_explanation'."
        )

    cases = dataset.get("cases")

    if not isinstance(cases, list) or not cases:
        raise ValueError(
            "Explanation dataset contains no cases."
        )

    unique_case_ids(
        cases,
        "held_out_explanations",
    )

    return cases


# =====================================================================
# REAL GEMINI ARTIFACT
# =====================================================================

def validate_generation_artifact(
    artifact: dict[str, Any],
) -> list[dict[str, Any]]:
    if artifact.get("dataset_version") != (
        EXPECTED_DATASET_VERSION
    ):
        raise ValueError(
            "Gemini generation artifact has the wrong "
            "dataset_version."
        )

    stage = artifact.get(
        "evaluation_stage"
    )

    if stage is not None and stage != (
        EXPECTED_GENERATION_STAGE
    ):
        raise ValueError(
            "Gemini generation artifact has unexpected "
            f"evaluation_stage: {stage!r}."
        )

    cases = artifact.get("cases")

    if not isinstance(cases, list):
        raise ValueError(
            "Gemini generation artifact must contain "
            "a 'cases' list."
        )

    unique_case_ids(
        cases,
        "real_gemini_explanation_run_5C4",
    )

    for item in cases:
        case_id = require_string(
            item.get("case_id"),
            "generation.case_id",
        )

        succeeded = item.get("succeeded")

        if succeeded is not None:
            require_bool(
                succeeded,
                f"{case_id}.succeeded",
            )

        explanation = item.get(
            "explanation"
        )

        # A failed provider call may legitimately have no
        # explanation. A successful call must have text.
        if succeeded is True:
            if not isinstance(
                explanation,
                str,
            ):
                raise ValueError(
                    f"{case_id}: successful Gemini "
                    "case must contain textual explanation."
                )

    return cases


# =====================================================================
# SEMANTIC SCORE ARTIFACT
# =====================================================================

def validate_semantic_artifact(
    artifact: dict[str, Any],
) -> list[dict[str, Any]]:
    if artifact.get("dataset_version") != (
        EXPECTED_DATASET_VERSION
    ):
        raise ValueError(
            "Semantic score artifact has the wrong "
            "dataset_version."
        )

    stage = artifact.get(
        "evaluation_stage"
    )

    if stage is not None and stage != (
        EXPECTED_SCORING_STAGE
    ):
        raise ValueError(
            "Semantic score artifact has unexpected "
            f"evaluation_stage: {stage!r}."
        )

    cases = artifact.get("cases")

    if not isinstance(cases, list):
        raise ValueError(
            "Semantic score artifact must contain "
            "a 'cases' list."
        )

    unique_case_ids(
        cases,
        "explanation_semantic_scores_5C4_4b",
    )

    return cases


# =====================================================================
# SEMANTIC CASE NORMALIZATION
# =====================================================================

def normalize_semantic_case(
    item: dict[str, Any],
) -> dict[str, Any]:
    """
    Normalize the persisted 5C.4.4b semantic scorer result.

    IMPORTANT:
        This function follows the ACTUAL persisted scorer schema.

    Persisted semantic dimensions:
        status_preserved
        required_amounts_preserved
        required_tax_preserved
        reason_codes_preserved
        evidence_preserved
        unsupported_claims
        contradictions
        scoring_error

    confidence_preserved is intentionally NOT required here because
    the current 5C.4.4b scorer does not persist that field.

    The report must never manufacture evaluation evidence that was
    not actually produced by the scorer.
    """

    case_id = require_string(
        item.get("case_id"),
        "semantic.case_id",
    )

    category = item.get(
        "category",
        "unknown",
    )

    if not isinstance(category, str):
        category = "unknown"

    # These are the actual boolean dimensions persisted by 5C.4.4b.
    bool_fields = (
        "status_preserved",
        "required_amounts_preserved",
        "required_tax_preserved",
        "reason_codes_preserved",
        "evidence_preserved",
    )

    normalized: dict[str, Any] = {
        "case_id": case_id,
        "category": category,
    }

    for field in bool_fields:
        normalized[field] = require_bool(
            item.get(field),
            f"{case_id}.{field}",
        )

    # These are actual persisted list fields.
    list_fields = (
        "unsupported_claims",
        "contradictions",
    )

    for field in list_fields:
        value = item.get(
            field,
            [],
        )

        if not isinstance(value, list):
            raise ValueError(
                f"{case_id}.{field} must be a list."
            )

        normalized[field] = value

    # The scorer may explicitly persist a scoring error.
    scoring_error = item.get(
        "scoring_error"
    )

    if scoring_error is not None:
        if not isinstance(
            scoring_error,
            str,
        ):
            raise ValueError(
                f"{case_id}.scoring_error must be "
                "a string or null."
            )

    normalized["scoring_error"] = scoring_error

    # Material-fact completeness is derived from the actual
    # preservation dimensions available in this artifact.
    #
    # We intentionally do NOT invent confidence preservation.
    missing_material_facts: list[str] = []

    if not normalized["status_preserved"]:
        missing_material_facts.append(
            "status"
        )

    if not normalized["required_amounts_preserved"]:
        missing_material_facts.append(
            "required amounts"
        )

    if not normalized["required_tax_preserved"]:
        missing_material_facts.append(
            "required tax"
        )

    if not normalized["reason_codes_preserved"]:
        missing_material_facts.append(
            "reason-code semantics"
        )

    if not normalized["evidence_preserved"]:
        missing_material_facts.append(
            "evidence semantics"
        )

    normalized["missing_material_facts"] = (
        missing_material_facts
    )

    # The current 5C.4.4b scorer does not persist a numeric score
    # in the artifact. Therefore this is deliberately None.
    #
    # Do not fabricate a score from the individual dimensions.
    normalized["score"] = None

    return normalized


# =====================================================================
# DERIVED SAFETY / QUALITY
# =====================================================================

def is_safety_critical(
    case: dict[str, Any],
) -> bool:
    return bool(
        case["contradictions"]
        or case["unsupported_claims"]
    )


def is_semantically_faithful(
    case: dict[str, Any],
) -> bool:
    return not (
        is_safety_critical(case)
        or case["missing_material_facts"]
    )


def count_true(
    cases: list[dict[str, Any]],
    field: str,
) -> int:
    return sum(
        1
        for case in cases
        if case[field] is True
    )


def percentage(
    numerator: int,
    denominator: int,
) -> float:
    if denominator == 0:
        return 0.0

    return round(
        numerator / denominator * 100.0,
        4,
    )


# =====================================================================
# QUALITY GAP REPORTING
# =====================================================================

def quality_gap_case_ids(
    cases: list[dict[str, Any]],
    field: str,
) -> list[str]:
    return [
        case["case_id"]
        for case in cases
        if not case[field]
    ]


def build_quality_findings(
    cases: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "reason_code_semantic_gap_cases": (
            quality_gap_case_ids(
                cases,
                "reason_codes_preserved",
            )
        ),
        "evidence_semantic_gap_cases": (
            quality_gap_case_ids(
                cases,
                "evidence_preserved",
            )
        ),
        "material_fact_gap_cases": [
            case["case_id"]
            for case in cases
            if case["missing_material_facts"]
        ],
        "safety_critical_cases": [
            case["case_id"]
            for case in cases
            if is_safety_critical(case)
        ],
    }


# =====================================================================
# REPORT BUILDING
# =====================================================================

def build_report(
    dataset: dict[str, Any],
    generation: dict[str, Any],
    semantic: dict[str, Any],
) -> dict[str, Any]:
    """
    Build the deterministic Phase 5C.4.5 explanation
    faithfulness report.

    Important:
        This function reports only dimensions that are actually
        persisted by the 5C.4.4b semantic scorer.

    Actual 5C.4.4b persisted dimensions:
        - status_preserved
        - required_amounts_preserved
        - required_tax_preserved
        - reason_codes_preserved
        - evidence_preserved
        - unsupported_claims
        - contradictions
        - scoring_error

    Confidence preservation is intentionally NOT reported because
    the current 5C.4.4b artifact does not persist that field.

    No Gemini/API calls occur here.
    """

    # ---------------------------------------------------------------
    # 1. Validate all three source artifacts
    # ---------------------------------------------------------------

    dataset_cases = validate_dataset(
        dataset
    )

    generation_cases = validate_generation_artifact(
        generation
    )

    semantic_cases_raw = validate_semantic_artifact(
        semantic
    )

    # ---------------------------------------------------------------
    # 2. Establish independent case-ID sets
    # ---------------------------------------------------------------

    dataset_ids = set(
        unique_case_ids(
            dataset_cases,
            "held_out_explanations",
        )
    )

    generation_ids = set(
        unique_case_ids(
            generation_cases,
            "real_gemini_explanation_run_5C4",
        )
    )

    semantic_ids = set(
        unique_case_ids(
            semantic_cases_raw,
            "explanation_semantic_scores_5C4_4b",
        )
    )

    # ---------------------------------------------------------------
    # 3. Require exact case coverage
    # ---------------------------------------------------------------

    assert_same_case_ids(
        dataset_ids,
        generation_ids,
        "Gemini generation artifact",
    )

    assert_same_case_ids(
        dataset_ids,
        semantic_ids,
        "semantic score artifact",
    )

    # ---------------------------------------------------------------
    # 4. Normalize actual 5C.4.4b semantic records
    # ---------------------------------------------------------------

    semantic_cases = [
        normalize_semantic_case(item)
        for item in semantic_cases_raw
    ]

    semantic_cases.sort(
        key=lambda item: item["case_id"]
    )

    total = len(semantic_cases)

    if total == 0:
        raise ValueError(
            "5C.4.5 cannot produce a report with zero "
            "semantic evaluation cases."
        )

    # ---------------------------------------------------------------
    # 5. Generation coverage
    # ---------------------------------------------------------------

    successful_generation_cases = sum(
        1
        for item in generation_cases
        if item.get("succeeded") is True
    )

    failed_generation_cases = (
        total - successful_generation_cases
    )

    # ---------------------------------------------------------------
    # 6. Safety metrics
    #
    # Safety-critical boundary:
    #
    #     contradictions
    #     OR
    #     unsupported financial claims
    #
    # Missing explanation detail is NOT automatically a
    # financial safety failure.
    # ---------------------------------------------------------------

    safety_failures = sum(
        is_safety_critical(item)
        for item in semantic_cases
    )

    contradictions = sum(
        bool(item["contradictions"])
        for item in semantic_cases
    )

    unsupported_claim_cases = sum(
        bool(item["unsupported_claims"])
        for item in semantic_cases
    )

    # ---------------------------------------------------------------
    # 7. Semantic faithfulness
    # ---------------------------------------------------------------

    semantically_faithful = sum(
        is_semantically_faithful(item)
        for item in semantic_cases
    )

    # ---------------------------------------------------------------
    # 8. Actual persisted semantic dimensions
    # ---------------------------------------------------------------

    status_preserved = count_true(
        semantic_cases,
        "status_preserved",
    )

    amounts_preserved = count_true(
        semantic_cases,
        "required_amounts_preserved",
    )

    tax_preserved = count_true(
        semantic_cases,
        "required_tax_preserved",
    )

    reason_codes_preserved = count_true(
        semantic_cases,
        "reason_codes_preserved",
    )

    evidence_preserved = count_true(
        semantic_cases,
        "evidence_preserved",
    )

    # ---------------------------------------------------------------
    # 9. Quality-gap analysis
    # ---------------------------------------------------------------

    quality_findings = build_quality_findings(
        semantic_cases
    )

    # ---------------------------------------------------------------
    # 10. Safety assessment
    # ---------------------------------------------------------------

    if safety_failures == 0:
        safety_assessment = (
            "NO_SAFETY_CRITICAL_FAILURES_DETECTED"
        )
    else:
        safety_assessment = (
            "SAFETY_CRITICAL_FAILURES_DETECTED"
        )

    # ---------------------------------------------------------------
    # 11. Final immutable report structure
    # ---------------------------------------------------------------

    return {
        "report_version": "5C.4.5-v1",

        "evaluation_stage": EXPECTED_REPORT_STAGE,

        "generated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),

        "authority": "deterministic",

        "model_role": "read_only_explanation",

        # -----------------------------------------------------------
        # Source artifacts
        # -----------------------------------------------------------

        "inputs": {
            "dataset": str(
                DATASET_PATH.relative_to(ROOT)
            ),

            "generation_artifact": str(
                RUN_OUTPUT_PATH.relative_to(ROOT)
            ),

            "semantic_score_artifact": str(
                SEMANTIC_SCORE_PATH.relative_to(ROOT)
            ),
        },

        # -----------------------------------------------------------
        # Dataset
        # -----------------------------------------------------------

        "dataset": {
            "dataset_version": (
                EXPECTED_DATASET_VERSION
            ),

            "cases": total,
        },

        # -----------------------------------------------------------
        # Coverage
        # -----------------------------------------------------------

        "coverage": {
            "dataset_cases": total,

            "generation_cases": len(
                generation_cases
            ),

            "semantic_cases": len(
                semantic_cases
            ),

            "case_alignment_verified": True,

            "successful_generation_cases": (
                successful_generation_cases
            ),

            "failed_generation_cases": (
                failed_generation_cases
            ),

            "generation_success_rate_percent": (
                percentage(
                    successful_generation_cases,
                    total,
                )
            ),
        },

        # -----------------------------------------------------------
        # Safety
        # -----------------------------------------------------------

        "safety": {
            "safety_critical_failures": (
                safety_failures
            ),

            "safety_failure_rate_percent": (
                percentage(
                    safety_failures,
                    total,
                )
            ),

            "contradictory_cases": (
                contradictions
            ),

            "unsupported_claim_cases": (
                unsupported_claim_cases
            ),

            "unsupported_claim_rate_percent": (
                percentage(
                    unsupported_claim_cases,
                    total,
                )
            ),

            "assessment": safety_assessment,
        },

        # -----------------------------------------------------------
        # Faithfulness
        #
        # NOTE:
        # confidence_preserved is deliberately absent.
        # The scorer artifact does not provide it.
        # -----------------------------------------------------------

        "faithfulness": {
            "semantically_faithful_cases": (
                semantically_faithful
            ),

            "semantic_faithfulness_rate_percent": (
                percentage(
                    semantically_faithful,
                    total,
                )
            ),

            "status_preserved": {
                "cases": status_preserved,
                "total": total,
                "rate_percent": percentage(
                    status_preserved,
                    total,
                ),
            },

            "amounts_preserved": {
                "cases": amounts_preserved,
                "total": total,
                "rate_percent": percentage(
                    amounts_preserved,
                    total,
                ),
            },

            "tax_preserved": {
                "cases": tax_preserved,
                "total": total,
                "rate_percent": percentage(
                    tax_preserved,
                    total,
                ),
            },

            "reason_codes_preserved": {
                "cases": reason_codes_preserved,
                "total": total,
                "rate_percent": percentage(
                    reason_codes_preserved,
                    total,
                ),
            },

            "evidence_preserved": {
                "cases": evidence_preserved,
                "total": total,
                "rate_percent": percentage(
                    evidence_preserved,
                    total,
                ),
            },

            "confidence_preserved": {
                "available": False,
                "reason": (
                    "The 5C.4.4b semantic scorer artifact "
                    "does not persist confidence_preserved."
                ),
            },
        },

        # -----------------------------------------------------------
        # Quality gaps
        # -----------------------------------------------------------

        "quality_gaps": quality_findings,

        # -----------------------------------------------------------
        # Interpretation
        # -----------------------------------------------------------

        "interpretation": {
            "financial_authority": (
                "The deterministic reconciliation facts "
                "remain authoritative. The LLM is evaluated "
                "only as a read-only explanation layer."
            ),

            "safety": (
                "Safety-critical failure is defined by the "
                "presence of a contradiction or unsupported "
                "financial claim. Zero failures in this "
                "captured evaluation set is evidence for "
                "this evaluation only and is not a production "
                "safety guarantee."
            ),

            "quality": (
                "Reason-code and evidence gaps are reported "
                "as explanation-quality limitations. They "
                "are not reclassified as financial safety "
                "failures unless they also produce a "
                "contradiction or unsupported financial claim."
            ),

            "confidence": (
                "Confidence preservation was not persisted "
                "by the 5C.4.4b scorer and therefore is not "
                "claimed or reconstructed by this report."
            ),

            "scope": (
                "This report covers only the frozen 5C.4 "
                "evaluation dataset and the captured Gemini "
                "outputs represented by the supplied artifacts."
            ),
        },

        # -----------------------------------------------------------
        # Per-case evidence
        # -----------------------------------------------------------

        "case_results": [
            {
                "case_id": item["case_id"],

                "category": item["category"],

                "status_preserved": item[
                    "status_preserved"
                ],

                "amounts_preserved": item[
                    "required_amounts_preserved"
                ],

                "tax_preserved": item[
                    "required_tax_preserved"
                ],

                "reason_codes_preserved": item[
                    "reason_codes_preserved"
                ],

                "evidence_preserved": item[
                    "evidence_preserved"
                ],

                "unsupported_claims": item[
                    "unsupported_claims"
                ],

                "contradictions": item[
                    "contradictions"
                ],

                "missing_material_facts": item[
                    "missing_material_facts"
                ],

                "scoring_error": item.get(
                    "scoring_error"
                ),

                "safety_critical_failure": (
                    is_safety_critical(item)
                ),

                "semantically_faithful": (
                    is_semantically_faithful(item)
                ),

                "score": item.get(
                    "score"
                ),
            }

            for item in semantic_cases
        ],
    }


# =====================================================================
# TERMINAL OUTPUT
# =====================================================================

def print_report(
    report: dict[str, Any],
) -> None:
    """
    Print the 5C.4.5 report.

    This printer understands both:
        - measured metrics
        - explicitly unavailable metrics

    It never assumes that an unavailable metric contains
    cases/total/rate_percent.
    """

    coverage = report["coverage"]
    safety = report["safety"]
    faithfulness = report["faithfulness"]

    print("=" * 72)
    print(
        "5C.4.5 EXPLANATION QUALITY / FAITHFULNESS REPORT"
    )
    print("=" * 72)

    # ---------------------------------------------------------------
    # Coverage
    # ---------------------------------------------------------------

    print()
    print("Evaluation coverage")
    print("-" * 72)

    print(
        f"Dataset cases:             "
        f"{coverage['dataset_cases']}"
    )

    print(
        f"Generation cases:         "
        f"{coverage['generation_cases']}"
    )

    print(
        f"Semantic score cases:     "
        f"{coverage['semantic_cases']}"
    )

    print(
        f"Case alignment:            "
        f"{coverage['case_alignment_verified']}"
    )

    print(
        f"Successful Gemini cases:  "
        f"{coverage['successful_generation_cases']}"
    )

    print(
        f"Generation success rate:  "
        f"{coverage['generation_success_rate_percent']:.2f}%"
    )

    # ---------------------------------------------------------------
    # Safety
    # ---------------------------------------------------------------

    print()
    print("Safety")
    print("-" * 72)

    print(
        f"Safety-critical failures: "
        f"{safety['safety_critical_failures']}"
    )

    print(
        f"Contradictory cases:       "
        f"{safety['contradictory_cases']}"
    )

    print(
        f"Unsupported-claim cases:  "
        f"{safety['unsupported_claim_cases']}"
    )

    print(
        f"Safety failure rate:      "
        f"{safety['safety_failure_rate_percent']:.2f}%"
    )

    # ---------------------------------------------------------------
    # Faithfulness
    # ---------------------------------------------------------------

    print()
    print("Faithfulness")
    print("-" * 72)

    measured_metrics = (
        ("status_preserved", "Status"),
        ("amounts_preserved", "Amounts"),
        ("tax_preserved", "Tax"),
        ("reason_codes_preserved", "Reason codes"),
        ("evidence_preserved", "Evidence"),
    )

    for key, label in measured_metrics:
        metric = faithfulness[key]

        print(
            f"{label:<25} "
            f"{metric['cases']}/"
            f"{metric['total']} "
            f"({metric['rate_percent']:.2f}%)"
        )

    # ---------------------------------------------------------------
    # Confidence
    #
    # This metric is deliberately unavailable because the
    # 5C.4.4b persisted artifact does not contain it.
    # ---------------------------------------------------------------

    confidence_metric = faithfulness.get(
        "confidence_preserved"
    )

    if (
        isinstance(
            confidence_metric,
            dict,
        )
        and confidence_metric.get("available") is False
    ):
        print(
            "Confidence preservation:  "
            "NOT AVAILABLE"
        )

        reason = confidence_metric.get(
            "reason"
        )

        if reason:
            print(
                f"                           "
                f"reason={reason}"
            )

    elif isinstance(
        confidence_metric,
        dict,
    ):
        # Defensive support if a future scorer actually
        # persists this metric.
        cases = confidence_metric.get(
            "cases"
        )
        total = confidence_metric.get(
            "total"
        )
        rate = confidence_metric.get(
            "rate_percent"
        )

        if (
            cases is not None
            and total is not None
            and rate is not None
        ):
            print(
                f"Confidence preservation:  "
                f"{cases}/{total} "
                f"({rate:.2f}%)"
            )
        else:
            print(
                "Confidence preservation:  "
                "NOT AVAILABLE"
            )

    # ---------------------------------------------------------------
    # Overall semantic faithfulness
    # ---------------------------------------------------------------

    print(
        f"Semantic faithfulness:    "
        f"{faithfulness['semantically_faithful_cases']}/"
        f"{coverage['semantic_cases']} "
        f"("
        f"{faithfulness['semantic_faithfulness_rate_percent']:.2f}%"
        f")"
    )

    # ---------------------------------------------------------------
    # Quality gaps
    # ---------------------------------------------------------------

    print()
    print("Quality gaps")
    print("-" * 72)

    gaps = report["quality_gaps"]

    print(
        "Reason-code gap cases:    "
        f"{gaps['reason_code_semantic_gap_cases']}"
    )

    print(
        "Evidence gap cases:       "
        f"{gaps['evidence_semantic_gap_cases']}"
    )

    print(
        "Material-fact gaps:       "
        f"{gaps['material_fact_gap_cases']}"
    )

    print(
        "Safety-critical cases:    "
        f"{gaps['safety_critical_cases']}"
    )

    # ---------------------------------------------------------------
    # Assessment
    # ---------------------------------------------------------------

    print()
    print("Assessment")
    print("-" * 72)

    print(
        safety["assessment"]
    )

    print()

    print(
        "5C.4.5 report generated successfully."
    )

    print(
        f"Artifact: {REPORT_PATH}"
    )


# =====================================================================
# MAIN
# =====================================================================

def main() -> None:
    dataset = load_json(
        DATASET_PATH
    )

    generation = load_json(
        RUN_OUTPUT_PATH
    )

    semantic = load_json(
        SEMANTIC_SCORE_PATH
    )

    report = build_report(
        dataset,
        generation,
        semantic,
    )

    write_json(
        report,
        REPORT_PATH,
    )

    print_report(
        report
    )


if __name__ == "__main__":
    main()