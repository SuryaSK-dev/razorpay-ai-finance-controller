# scripts/verify_e2e_gold_baseline.py
"""
Phase 5C.5.2 — Deterministic E2E gold baseline verification.

Purpose
-------
Verify the completed 5C.5.3 deterministic E2E execution artifact
against the frozen 5C.5.1 benchmark.

Architecture
------------

    frozen E2E benchmark
             │
             │ expected deterministic result
             ▼
        ┌─────────────┐
        │ GOLD RECORD │
        └──────┬──────┘
               │
               │ exact comparison
               │
        ┌──────▼──────────────┐
        │ 5C.5.3 EXECUTION    │
        │ ARTIFACT             │
        └──────┬──────────────┘
               │
               ▼
        ┌─────────────────────┐
        │ CURRENT RESULT      │
        └─────────────────────┘

Important
---------
5C.5.2 does NOT independently reconstruct a second 61-case execution
from data/raw.

The deterministic execution has already been completed by:

    scripts/run_e2e_deterministic.py

and frozen as:

    data/eval/e2e_deterministic_results_5C5_3.json

This verifier consumes that artifact as the actual-result source.

The frozen 5C.5.1 benchmark remains the gold source.

No Gemini calls.
No AI-generated result participates in the comparison.
No ground_truth.json is read.
Corrupted records are expected to appear as deterministic terminal
outcomes from 5C.5.3, e.g.:

    UNMATCHED / CORRUPTED_RECORD
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


# ======================================================================
# PATHS / CONSTANTS
# ======================================================================

ROOT = Path(__file__).resolve().parent.parent

EVAL_DIR = ROOT / "data" / "eval"

BENCHMARK_PATH = (
    EVAL_DIR / "e2e_reconciliation_benchmark_5C5_1.json"
)

EXECUTION_ARTIFACT_PATH = (
    EVAL_DIR / "e2e_deterministic_results_5C5_3.json"
)

OUTPUT_PATH = (
    EVAL_DIR / "e2e_gold_baseline_verification_5C5_2.json"
)

EXPECTED_DATASET_VERSION = "5C.5-v1"
EXPECTED_BENCHMARK_STAGE = "5C.5.1"
EXPECTED_EXECUTION_STAGE = "5C.5.3"
EXPECTED_STAGE = "5C.5.2"

EXPECTED_REPORT_VERSION = "5C.5.2-v1"

EXPECTED_CASE_COUNT = 63


# ======================================================================
# JSON HELPERS
# ======================================================================

def serialize_value(value: Any) -> Any:
    """
    Convert deterministic model values into stable JSON-compatible
    values.
    """

    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if hasattr(value, "value"):
        return value.value

    if isinstance(value, dict):
        return {
            str(key): serialize_value(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [
            serialize_value(item)
            for item in value
        ]

    if isinstance(value, tuple):
        return [
            serialize_value(item)
            for item in value
        ]

    return value


def load_json(path: Path) -> dict[str, Any]:
    """
    Load a required JSON artifact.
    """

    if not path.exists():
        raise FileNotFoundError(
            f"Required artifact does not exist: {path}"
        )

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        raise ValueError(
            f"Expected JSON object in {path}, "
            f"got {type(data).__name__}"
        )

    return data


def canonical_json(value: Any) -> str:
    """
    Stable comparison representation.

    Dictionary ordering cannot create a false baseline mismatch.
    """

    return json.dumps(
        serialize_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


# ======================================================================
# FROZEN GOLD
# ======================================================================

def extract_frozen_gold(
    case: dict[str, Any],
) -> dict[str, Any]:
    """
    Extract the deterministic expected decision from the frozen
    5C.5.1 benchmark.

    Only fields explicitly frozen by the benchmark are compared.
    """

    gold = case.get("deterministic_expected_decision")

    if not isinstance(gold, dict):
        raise ValueError(
            f"{case.get('case_id')} must contain a valid "
            "'deterministic_expected_decision' object."
        )

    normalized: dict[str, Any] = {}

    field_aliases = {
        "status": (
            "status",
            "expected_status",
        ),
        "exception_code": (
            "exception_code",
            "expected_exception_code",
        ),
        "matched_sources": (
            "matched_sources",
            "expected_matched_sources",
        ),
        "reason_codes": (
            "reason_codes",
            "expected_reason_codes",
        ),
        "evidence": (
            "evidence",
            "expected_evidence",
        ),
    }

    for canonical_name, aliases in field_aliases.items():
        for alias in aliases:
            if alias in gold:
                normalized[canonical_name] = serialize_value(
                    gold[alias]
                )
                break

    txn_id = (
        gold.get("txn_id")
        or gold.get("transaction_id")
        or case.get("source_transaction_id")
    )

    if txn_id is None:
        raise ValueError(
            f"{case.get('case_id')} has no transaction identifier."
        )

    normalized["txn_id"] = serialize_value(txn_id)

    required = {
        "txn_id",
        "status",
        "exception_code",
    }

    missing = {
        field
        for field in required
        if field not in normalized
    }

    if missing:
        raise ValueError(
            f"{case.get('case_id')} is missing deterministic gold "
            f"fields {sorted(missing)}."
        )

    return normalized


def index_gold_cases(
    benchmark: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """
    Index frozen benchmark cases by case_id.
    """

    cases = benchmark.get("cases")

    if not isinstance(cases, list):
        raise ValueError(
            "Frozen benchmark cases must be a list."
        )

    indexed: dict[str, dict[str, Any]] = {}

    for case in cases:
        case_id = case.get("case_id")

        if not case_id:
            raise ValueError(
                "Benchmark contains a case without case_id."
            )

        if case_id in indexed:
            raise ValueError(
                f"Duplicate frozen case ID: {case_id}"
            )

        indexed[case_id] = case

    return indexed


# ======================================================================
# 5C.5.3 EXECUTION ARTIFACT
# ======================================================================

def validate_execution_artifact(
    artifact: dict[str, Any],
) -> None:
    """
    Validate that the supplied 5C.5.3 artifact is the artifact we
    expect to consume.

    This prevents accidentally comparing the gold benchmark against
    an unrelated or stale JSON file.
    """

    report_version = artifact.get("report_version")

    if report_version is not None:
        if report_version != "5C.5.3-v1":
            raise ValueError(
                "Unexpected 5C.5.3 execution report version: "
                f"{report_version!r}"
            )

    evaluation_stage = artifact.get("evaluation_stage")

    if evaluation_stage is not None:
        if evaluation_stage != EXPECTED_EXECUTION_STAGE:
            raise ValueError(
                "Execution artifact stage mismatch: "
                f"{evaluation_stage!r}; expected "
                f"{EXPECTED_EXECUTION_STAGE!r}."
            )

    dataset_version = artifact.get("dataset_version")

    if dataset_version is not None:
        if dataset_version != EXPECTED_DATASET_VERSION:
            raise ValueError(
                "Execution artifact dataset version mismatch: "
                f"{dataset_version!r}; expected "
                f"{EXPECTED_DATASET_VERSION!r}."
            )

    cases = artifact.get("cases")

    if not isinstance(cases, list):
        raise ValueError(
            "5C.5.3 execution artifact must contain a 'cases' list."
        )

    if len(cases) != EXPECTED_CASE_COUNT:
        raise ValueError(
            "5C.5.3 execution artifact must contain "
            f"{EXPECTED_CASE_COUNT} cases; found {len(cases)}."
        )


def extract_execution_decision(
    case: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Extract the deterministic decision from one 5C.5.3 case.

    A case may legitimately have no deterministic_decision only when
    the execution itself failed. Such a case is represented as a
    missing actual result.
    """

    decision = case.get("deterministic_decision")

    if decision is None:
        return None

    if not isinstance(decision, dict):
        raise ValueError(
            f"{case.get('case_id')} has a malformed "
            "'deterministic_decision'."
        )

    normalized: dict[str, Any] = {}

    for field in (
        "txn_id",
        "status",
        "exception_code",
        "matched_sources",
        "reason_codes",
        "evidence",
    ):
        if field in decision:
            normalized[field] = serialize_value(
                decision[field]
            )

    # The execution artifact normally carries txn_id in the decision,
    # but retain the case-level transaction ID as a safe fallback.
    if "txn_id" not in normalized:
        txn_id = case.get("source_transaction_id")

        if txn_id is not None:
            normalized["txn_id"] = serialize_value(
                txn_id
            )

    return normalized


def index_execution_cases(
    artifact: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """
    Index 5C.5.3 execution cases by case_id.
    """

    indexed: dict[str, dict[str, Any]] = {}

    for case in artifact["cases"]:
        case_id = case.get("case_id")

        if not case_id:
            raise ValueError(
                "5C.5.3 artifact contains a case without case_id."
            )

        if case_id in indexed:
            raise ValueError(
                f"Duplicate 5C.5.3 case ID: {case_id}"
            )

        indexed[case_id] = case

    return indexed


# ======================================================================
# COMPARISON
# ======================================================================

def compare_decisions(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> dict[str, Any]:
    """
    Compare the deterministic fields that are present in the frozen
    gold record.

    Do not compare fields that the gold benchmark does not freeze.
    """

    differences: dict[str, Any] = {}

    for field in (
        "txn_id",
        "status",
        "exception_code",
        "matched_sources",
        "reason_codes",
        "evidence",
    ):
        if field not in expected:
            continue

        expected_value = serialize_value(
            expected.get(field)
        )

        actual_value = serialize_value(
            actual.get(field)
        )

        if canonical_json(expected_value) != canonical_json(
            actual_value
        ):
            differences[field] = {
                "expected": expected_value,
                "actual": actual_value,
            }

    return {
        "match": not differences,
        "differences": differences,
    }


# ======================================================================
# VERIFICATION
# ======================================================================

def verify() -> dict[str, Any]:
    """
    Compare the frozen 5C.5.1 benchmark against the completed
    5C.5.3 deterministic execution artifact.
    """

    benchmark = load_json(BENCHMARK_PATH)
    execution_artifact = load_json(
        EXECUTION_ARTIFACT_PATH
    )

    # --------------------------------------------------------------
    # Validate frozen benchmark metadata
    # --------------------------------------------------------------

    if benchmark.get("dataset_version") != EXPECTED_DATASET_VERSION:
        raise ValueError(
            "Frozen benchmark version mismatch: "
            f"{benchmark.get('dataset_version')!r}; expected "
            f"{EXPECTED_DATASET_VERSION!r}."
        )

    if benchmark.get("authority") != "deterministic":
        raise ValueError(
            "Frozen benchmark must declare deterministic authority."
        )

    benchmark_stage = benchmark.get("evaluation_stage")

    if benchmark_stage != EXPECTED_BENCHMARK_STAGE:
        raise ValueError(
            "Frozen benchmark stage mismatch: "
            f"{benchmark_stage!r}; expected "
            f"{EXPECTED_BENCHMARK_STAGE!r}."
        )

    frozen_cases = index_gold_cases(
        benchmark
    )

    if len(frozen_cases) != EXPECTED_CASE_COUNT:
        raise ValueError(
            "5C.5.2 requires the locked 5C.5.1 benchmark with "
            f"{EXPECTED_CASE_COUNT} cases; found "
            f"{len(frozen_cases)}."
        )

    # --------------------------------------------------------------
    # Validate every gold record before comparison
    # --------------------------------------------------------------

    for case_id, case in frozen_cases.items():
        try:
            extract_frozen_gold(case)
        except ValueError as exc:
            raise ValueError(
                f"Invalid frozen gold for {case_id}: {exc}"
            ) from exc

    # --------------------------------------------------------------
    # Validate 5C.5.3 execution artifact
    # --------------------------------------------------------------

    validate_execution_artifact(
        execution_artifact
    )

    execution_cases = index_execution_cases(
        execution_artifact
    )

    # --------------------------------------------------------------
    # Compare benchmark against execution artifact
    # --------------------------------------------------------------

    results: list[dict[str, Any]] = []

    for case_id, frozen_case in frozen_cases.items():

        expected = extract_frozen_gold(
            frozen_case
        )

        execution_case = execution_cases.get(
            case_id
        )

        if execution_case is None:
            results.append(
                {
                    "case_id": case_id,
                    "txn_id": expected["txn_id"],
                    "status": "MISSING_EXECUTION_CASE",
                    "match": False,
                    "differences": {
                        "case_id": {
                            "expected": case_id,
                            "actual": None,
                        }
                    },
                }
            )
            continue

        actual = extract_execution_decision(
            execution_case
        )

        # ----------------------------------------------------------
        # The case executed, but produced no deterministic decision.
        #
        # This should never happen for the completed 5C.5.3 artifact.
        # ----------------------------------------------------------

        if actual is None:
            results.append(
                {
                    "case_id": case_id,
                    "txn_id": expected["txn_id"],
                    "status": "MISSING_ACTUAL",
                    "match": False,
                    "differences": {
                        "deterministic_decision": {
                            "expected": "decision object",
                            "actual": None,
                        }
                    },
                }
            )
            continue

        comparison = compare_decisions(
            expected,
            actual,
        )

        results.append(
            {
                "case_id": case_id,
                "txn_id": expected["txn_id"],
                "status": (
                    "MATCH"
                    if comparison["match"]
                    else "BASELINE_DIVERGENCE"
                ),
                "match": comparison["match"],
                "differences": comparison["differences"],
            }
        )

    # --------------------------------------------------------------
    # Coverage checks
    # --------------------------------------------------------------

    frozen_case_ids = set(
        frozen_cases.keys()
    )

    execution_case_ids = set(
        execution_cases.keys()
    )

    unexpected_execution_cases = sorted(
        execution_case_ids - frozen_case_ids
    )

    missing_execution_cases = sorted(
        frozen_case_ids - execution_case_ids
    )

    matched_cases = sum(
        result["match"]
        for result in results
    )

    divergent_cases = sum(
        not result["match"]
        for result in results
    )

    execution_errors = sum(
        case.get("status") == "EXECUTION_ERROR"
        for case in execution_cases.values()
    )

    successful_executions = sum(
        case.get("status") != "EXECUTION_ERROR"
        and case.get("deterministic_decision") is not None
        for case in execution_cases.values()
    )

    # --------------------------------------------------------------
    # Baseline stability
    # --------------------------------------------------------------

    baseline_stable = (
        len(frozen_cases) == EXPECTED_CASE_COUNT
        and len(execution_cases) == EXPECTED_CASE_COUNT
        and not missing_execution_cases
        and not unexpected_execution_cases
        and execution_errors == 0
        and successful_executions == EXPECTED_CASE_COUNT
        and divergent_cases == 0
    )

    return {
        "report_version": EXPECTED_REPORT_VERSION,
        "evaluation_stage": EXPECTED_STAGE,
        "dataset_version": EXPECTED_DATASET_VERSION,
        "authority": "deterministic",
        "model_role": "read_only_assistance",

        "inputs": {
            "frozen_benchmark": str(
                BENCHMARK_PATH.relative_to(ROOT)
            ),
            "deterministic_execution_artifact": str(
                EXECUTION_ARTIFACT_PATH.relative_to(ROOT)
            ),
        },

        "execution_integrity": {
            "expected_cases": EXPECTED_CASE_COUNT,
            "execution_cases": len(execution_cases),
            "successful_executions": successful_executions,
            "execution_errors": execution_errors,
        },

        "coverage": {
            "frozen_cases": len(frozen_cases),
            "execution_cases": len(execution_cases),
            "actual_decisions": successful_executions,
            "matched_cases": matched_cases,
            "divergent_cases": divergent_cases,
            "missing_execution_cases": len(
                missing_execution_cases
            ),
            "unexpected_execution_cases": len(
                unexpected_execution_cases
            ),
        },

        "baseline_stable": baseline_stable,

        "unexpected_execution_case_ids": (
            unexpected_execution_cases
        ),

        "missing_execution_case_ids": (
            missing_execution_cases
        ),

        "case_results": results,
    }


# ======================================================================
# CLI
# ======================================================================

def main() -> None:
    print("=" * 72)
    print(
        "5C.5.2 DETERMINISTIC E2E GOLD BASELINE VERIFICATION"
    )
    print("=" * 72)

    report = verify()

    coverage = report["coverage"]
    integrity = report["execution_integrity"]

    print()
    print(
        "Frozen benchmark cases: "
        f"{coverage['frozen_cases']}"
    )

    print(
        "5C.5.3 execution cases: "
        f"{coverage['execution_cases']}"
    )

    print(
        "Successful executions: "
        f"{integrity['successful_executions']}"
    )

    print(
        "Execution errors: "
        f"{integrity['execution_errors']}"
    )

    print(
        "Exact matches: "
        f"{coverage['matched_cases']}"
    )

    print(
        "Baseline divergences: "
        f"{coverage['divergent_cases']}"
    )

    print(
        "Missing execution cases: "
        f"{coverage['missing_execution_cases']}"
    )

    print(
        "Unexpected execution cases: "
        f"{coverage['unexpected_execution_cases']}"
    )

    print()

    if not report["baseline_stable"]:
        print(
            "5C.5.2 RESULT: BASELINE DRIFT DETECTED"
        )

        for result in report["case_results"]:
            if not result["match"]:
                print(
                    f"  {result['case_id']} "
                    f"{result['status']}"
                )

                if result["differences"]:
                    print(
                        "       differences="
                        f"{result['differences']}"
                    )

        # Persist the failure report as well. This is important:
        # a failed verification should still leave an auditable
        # artifact explaining why it failed.
        OUTPUT_PATH.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with OUTPUT_PATH.open(
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump(
                report,
                handle,
                indent=2,
                ensure_ascii=False,
            )

        print()
        print(
            f"Artifact: {OUTPUT_PATH}"
        )

        raise SystemExit(1)

    print(
        "5C.5.2 RESULT: DETERMINISTIC GOLD BASELINE STABLE"
    )

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with OUTPUT_PATH.open(
        "w",
        encoding="utf-8",
    ) as handle:
        json.dump(
            report,
            handle,
            indent=2,
            ensure_ascii=False,
        )

    print()
    print(
        f"Artifact: {OUTPUT_PATH}"
    )

    print(
        "5C.5.2 deterministic gold baseline verification: COMPLETE"
    )


if __name__ == "__main__":
    main()