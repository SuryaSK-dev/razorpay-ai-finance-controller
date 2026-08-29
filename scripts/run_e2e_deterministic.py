# scripts/run_e2e_deterministic.py
"""
Phase 5C.5.3 — Deterministic E2E execution.

Purpose
-------
Execute every frozen 5C.5.1 benchmark case through the REAL production
deterministic pipeline:

    benchmark case
        -> raw source JSON
        -> ingestion
        -> normalization
        -> matching
        -> deterministic decision

This stage records ACTUAL deterministic outputs only.

Important boundary
------------------
This module does NOT:

    - modify the frozen benchmark
    - compare actual results against gold
    - repair expected decisions
    - invoke an LLM
    - infer financial truth
    - alter Phase 3 or Phase 4 behavior

Expected-vs-actual analysis belongs to the following checkpoint.

Evaluation materialization principle
------------------------------------
The benchmark is the source of truth for the records that belong to a
case.

A case may contain:

    - the primary PG transaction
    - the corresponding bank record
    - one or more duplicate/sibling bank records
    - the corresponding invoice record
    - one or more sibling records explicitly attached to the case

All such records must be materialized into the same batch.

This is critical for duplicate/ambiguity evaluation.

The E2E adapter MUST NOT:
    - synthesize records
    - modify transaction IDs
    - alter amounts
    - recalculate fees
    - recalculate GST/TDS
    - change dates
    - rename source records
    - collapse duplicate rows

The adapter only selects and writes the records already declared by
the frozen benchmark.

Corrupted-record boundary
-------------------------
An ingestion rejection is NOT a pipeline programming failure when the
benchmark case is intentionally testing malformed source data.

The ingestion layer remains the validation firewall.

For an expected corrupted case:

    raw source
        -> ingestion rejection
        -> deterministic CORRUPTED_RECORD outcome
        -> STOP

The rejected record is NEVER normalized, matched, or passed into
decisioning.

This preserves the architectural boundary:

    malformed input != matching failure != AI failure
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from src.ingestion.loader import load_batch
from src.normalization.engine import normalize_batch
from src.matching.engine import run_matching
from src.exceptions.manager import decide_batch
from src.models import DecisionStatus, ExceptionCode


BENCHMARK_PATH = (
    ROOT
    / "data"
    / "eval"
    / "e2e_reconciliation_benchmark_5C5_1.json"
)


OUTPUT_PATH = (
    ROOT
    / "data"
    / "eval"
    / "e2e_deterministic_results_5C5_3.json"
)


EXPECTED_BENCHMARK_VERSION = "5C.5-v1"
EXPECTED_STAGE = "5C.5.3"


# Categories whose defining behavior is source-record corruption.
CORRUPTED_CATEGORIES = {
    "corrupted",
}


# ---------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------

def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Required artifact not found: {path}"
        )

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        raise ValueError(
            f"Expected JSON object in {path}, "
            f"got {type(data).__name__}"
        )

    return data


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as handle:
        json.dump(
            payload,
            handle,
            indent=2,
            ensure_ascii=False,
        )
        handle.write("\n")


# ---------------------------------------------------------------------
# Benchmark validation
# ---------------------------------------------------------------------

def validate_benchmark(
    benchmark: dict[str, Any],
) -> list[dict[str, Any]]:
    version = benchmark.get("dataset_version")

    if version != EXPECTED_BENCHMARK_VERSION:
        raise ValueError(
            "Unexpected benchmark version: "
            f"{version!r}; expected "
            f"{EXPECTED_BENCHMARK_VERSION!r}"
        )

    cases = benchmark.get("cases")

    if not isinstance(cases, list):
        raise ValueError(
            "Benchmark 'cases' must be a list."
        )

    if not cases:
        raise ValueError(
            "Frozen benchmark contains zero cases."
        )

    required_case_fields = {
        "case_id",
        "category",
        "input_transaction",
        "narration",
        "source_transaction_id",
        "deterministic_expected_decision",
    }

    seen_ids: set[str] = set()

    for case in cases:
        if not isinstance(case, dict):
            raise ValueError(
                "Every benchmark case must be a JSON object."
            )

        missing = required_case_fields - case.keys()

        if missing:
            raise ValueError(
                f"Case {case.get('case_id')!r} is missing "
                f"required fields: {sorted(missing)}"
            )

        case_id = case["case_id"]

        if not isinstance(case_id, str) or not case_id:
            raise ValueError(
                "Every benchmark case must have a "
                "non-empty string case_id."
            )

        if case_id in seen_ids:
            raise ValueError(
                f"Duplicate benchmark case_id: {case_id}"
            )

        seen_ids.add(case_id)

        transaction = case["input_transaction"]

        if not isinstance(transaction, dict):
            raise ValueError(
                f"{case_id}.input_transaction must be an object."
            )

        for source in ("pg", "bank", "invoice"):
            records = transaction.get(source)

            if not isinstance(records, list):
                raise ValueError(
                    f"{case_id}.input_transaction.{source} "
                    "must be a list."
                )

    return cases


# ---------------------------------------------------------------------
# Evaluation-record extraction
# ---------------------------------------------------------------------

def _validate_record_list(
    case_id: str,
    source: str,
    records: Any,
    location: str,
) -> list[dict[str, Any]]:
    """
    Validate a benchmark-provided source record collection.

    We intentionally do not transform records here.
    """

    if records is None:
        return []

    if not isinstance(records, list):
        raise ValueError(
            f"{case_id}.{location} must be a list."
        )

    validated: list[dict[str, Any]] = []

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(
                f"{case_id}.{location}[{index}] must be an object."
            )

        validated.append(record)

    return validated


def _extract_case_source_records(
    case: dict[str, Any],
    source: str,
) -> list[dict[str, Any]]:
    """
    Return every benchmark-declared record for one source.

    Primary records come from:

        case["input_transaction"][source]

    The benchmark may additionally expose sibling/duplicate records
    through explicit case-level collections.

    Supported optional layouts:

        case["sibling_records"][source]
        case["duplicate_records"][source]

    and:

        case["related_records"][source]

    These are additive only.

    No record is synthesized or modified.

    If the benchmark does not contain these optional fields, behavior
    is identical to the original implementation.
    """

    case_id = case["case_id"]
    transaction = case["input_transaction"]

    records: list[dict[str, Any]] = []

    # ---------------------------------------------------------------
    # Primary case records
    # ---------------------------------------------------------------

    records.extend(
        _validate_record_list(
            case_id=case_id,
            source=source,
            records=transaction.get(source),
            location=f"input_transaction.{source}",
        )
    )

    # ---------------------------------------------------------------
    # Optional explicit sibling records
    # ---------------------------------------------------------------

    sibling_records = case.get("sibling_records")

    if sibling_records is not None:
        if not isinstance(sibling_records, dict):
            raise ValueError(
                f"{case_id}.sibling_records must be an object."
            )

        records.extend(
            _validate_record_list(
                case_id=case_id,
                source=source,
                records=sibling_records.get(source),
                location=f"sibling_records.{source}",
            )
        )

    # ---------------------------------------------------------------
    # Optional explicit duplicate records
    # ---------------------------------------------------------------

    duplicate_records = case.get("duplicate_records")

    if duplicate_records is not None:
        if not isinstance(duplicate_records, dict):
            raise ValueError(
                f"{case_id}.duplicate_records must be an object."
            )

        records.extend(
            _validate_record_list(
                case_id=case_id,
                source=source,
                records=duplicate_records.get(source),
                location=f"duplicate_records.{source}",
            )
        )

    # ---------------------------------------------------------------
    # Optional generic related records
    # ---------------------------------------------------------------

    related_records = case.get("related_records")

    if related_records is not None:
        if not isinstance(related_records, dict):
            raise ValueError(
                f"{case_id}.related_records must be an object."
            )

        records.extend(
            _validate_record_list(
                case_id=case_id,
                source=source,
                records=related_records.get(source),
                location=f"related_records.{source}",
            )
        )

    return records


def _deduplicate_record_objects(
    records: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Preserve record multiplicity while removing only exact duplicate
    JSON objects.

    Important:
        Two records with different fields are NEVER collapsed merely
        because they share a transaction ID.

    That distinction is essential for duplicate/ambiguity testing.

    Exact byte-equivalent logical records, if repeated accidentally in
    multiple benchmark sections, are written only once.
    """

    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    for record in records:
        fingerprint = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

        if fingerprint in seen:
            continue

        seen.add(fingerprint)
        result.append(record)

    return result


# ---------------------------------------------------------------------
# Temporary raw-source materialization
# ---------------------------------------------------------------------

def materialize_case(
    case: dict[str, Any],
    case_dir: Path,
) -> None:
    """
    Convert one benchmark case into the exact three JSON files expected
    by src.ingestion.loader.load_batch().

    This is ONLY an evaluation adapter.

    It does not:
        - transform fields
        - calculate financial values
        - alter transaction IDs
        - merge records by transaction ID
        - remove duplicate candidates
        - choose a winning candidate

    Every benchmark-declared primary/sibling/duplicate record is placed
    into the same source batch so that the REAL production ingestion,
    normalization, candidate generation and matching layers can observe
    the complete evidence set.
    """

    case_id = case["case_id"]

    filenames = {
        "pg": "pg_settlement.json",
        "bank": "bank_statement.json",
        "invoice": "merchant_invoice.json",
    }

    for source, filename in filenames.items():
        records = _extract_case_source_records(
            case,
            source,
        )

        records = _deduplicate_record_objects(records)

        path = case_dir / filename

        with path.open("w", encoding="utf-8") as handle:
            json.dump(
                records,
                handle,
                indent=2,
                ensure_ascii=False,
            )
            handle.write("\n")


# ---------------------------------------------------------------------
# Decision serialization
# ---------------------------------------------------------------------

def serialize_decision(decision) -> dict[str, Any]:
    """
    Convert the project's Pydantic MatchDecision into a stable,
    JSON-safe evaluation representation.
    """

    if hasattr(decision, "model_dump"):
        return decision.model_dump(mode="json")

    if hasattr(decision, "dict"):
        return decision.dict()

    raise TypeError(
        "MatchDecision does not expose model_dump() or dict()."
    )


# ---------------------------------------------------------------------
# Corrupted-record terminal decision
# ---------------------------------------------------------------------

def build_corrupted_record_decision(
    case: dict[str, Any],
    ingestion_errors: int,
) -> dict[str, Any]:
    """
    Build the deterministic terminal outcome for an ingestion-rejected
    corrupted benchmark case.

    This does NOT repair or reinterpret the malformed source record.

    The validation failure itself remains the evidence that the source
    could not safely enter the financial pipeline.
    """

    case_id = case["case_id"]
    source_transaction_id = case["source_transaction_id"]

    if ingestion_errors <= 0:
        raise ValueError(
            f"{case_id}: corrupted-record decision requested without "
            "an observed ingestion rejection."
        )

    return {
        "txn_id": source_transaction_id,
        "status": DecisionStatus.UNMATCHED.value,
        "confidence_score": 0,
        "matched_sources": [],
        "tax_verified": None,
        "exception_code": ExceptionCode.CORRUPTED_RECORD.value,
        "reason_codes": [
            ExceptionCode.CORRUPTED_RECORD.value
        ],
        "evidence": {
            "terminal_stage": "ingestion",
            "ingestion_rejected": True,
            "ingestion_error_count": ingestion_errors,
            "matching_executed": False,
            "decisioning_from_match_result": False,
            "reason": (
                "Source record failed schema validation and was "
                "blocked at the ingestion firewall."
            ),
        },
    }


# ---------------------------------------------------------------------
# One-case execution
# ---------------------------------------------------------------------

def execute_case(
    case: dict[str, Any],
    workspace: Path,
) -> dict[str, Any]:

    case_id = case["case_id"]
    source_transaction_id = case["source_transaction_id"]
    category = case["category"]

    case_dir = workspace / case_id
    case_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------
    # Evaluation-only raw materialization
    # ---------------------------------------------------------------

    materialize_case(
        case,
        case_dir,
    )

    # ---------------------------------------------------------------
    # REAL Phase 2 ingestion
    # ---------------------------------------------------------------

    loaded = load_batch(case_dir)

    # ---------------------------------------------------------------
    # Expected corrupted-record terminal path
    # ---------------------------------------------------------------

    if loaded.total_errors:
        if category not in CORRUPTED_CATEGORIES:
            raise ValueError(
                f"{case_id}: ingestion rejected "
                f"{loaded.total_errors} source records for a "
                f"non-corrupted benchmark category {category!r}."
            )

        corrupted_decision = build_corrupted_record_decision(
            case,
            loaded.total_errors,
        )

        return {
            "case_id": case_id,
            "category": category,
            "source_transaction_id": source_transaction_id,
            "execution": {
                "ingestion_errors": loaded.total_errors,
                "normalized_record_count": 0,
                "matching_result_count": 0,
                "decision_count": 1,
            },
            "deterministic_decision": corrupted_decision,
            "matching": None,
            "status": "EXECUTED",
            "error": None,
        }

    # ---------------------------------------------------------------
    # REAL Phase 2 normalization
    # ---------------------------------------------------------------

    normalized = normalize_batch(loaded)

    # ---------------------------------------------------------------
    # REAL Phase 3 matching
    # ---------------------------------------------------------------

    match_results = run_matching(
        normalized.records
    )

    # ---------------------------------------------------------------
    # REAL Phase 4 deterministic decisioning
    # ---------------------------------------------------------------

    decisions = decide_batch(
        match_results
    )

    matching_results_by_txn = {
        result.txn_id: result
        for result in match_results
    }

    decisions_by_txn = {
        decision.txn_id: decision
        for decision in decisions
    }

    decision = decisions_by_txn.get(
        source_transaction_id
    )

    if decision is None:
        raise ValueError(
            f"{case_id}: deterministic pipeline produced "
            f"no decision for source transaction "
            f"{source_transaction_id!r}."
        )

    matching_result = matching_results_by_txn.get(
        source_transaction_id
    )

    if matching_result is None:
        raise ValueError(
            f"{case_id}: deterministic pipeline produced "
            f"a decision but no corresponding matching result "
            f"for source transaction "
            f"{source_transaction_id!r}."
        )

    serialized_decision = serialize_decision(
        decision
    )

    return {
        "case_id": case_id,
        "category": category,
        "source_transaction_id": source_transaction_id,
        "execution": {
            "ingestion_errors": loaded.total_errors,
            "normalized_record_count": len(
                normalized.records
            ),
            "matching_result_count": len(
                match_results
            ),
            "decision_count": len(
                decisions
            ),
        },
        "deterministic_decision": serialized_decision,
        "matching": {
            "txn_id": matching_result.txn_id,
            "bank_candidate_count": (
                matching_result.bank_candidate_count
            ),
            "invoice_candidate_count": (
                matching_result.invoice_candidate_count
            ),
            "bank_match_type": (
                matching_result.bank_match_type
            ),
            "invoice_match_type": (
                matching_result.invoice_match_type
            ),
            "confidence": (
                matching_result.confidence.value
            ),
            "is_ambiguous": (
                matching_result.is_ambiguous
            ),
            "sources_present": (
                matching_result.sources_present
            ),
            "matcher_version": (
                matching_result.matcher_version
            ),
        },
        "status": "EXECUTED",
        "error": None,
    }


# ---------------------------------------------------------------------
# Full benchmark execution
# ---------------------------------------------------------------------

def build_results(
    cases: list[dict[str, Any]],
) -> dict[str, Any]:

    results: list[dict[str, Any]] = []

    execution_errors = 0

    with tempfile.TemporaryDirectory(
        prefix="razorpay_e2e_5C5_3_"
    ) as temporary_root:

        workspace = Path(temporary_root)

        for case in cases:
            try:
                result = execute_case(
                    case,
                    workspace,
                )

            except Exception as exc:
                execution_errors += 1

                result = {
                    "case_id": case["case_id"],
                    "category": case["category"],
                    "source_transaction_id": (
                        case["source_transaction_id"]
                    ),
                    "execution": {
                        "ingestion_errors": None,
                        "normalized_record_count": None,
                        "matching_result_count": None,
                        "decision_count": None,
                    },
                    "deterministic_decision": None,
                    "matching": None,
                    "status": "EXECUTION_ERROR",
                    "error": (
                        f"{type(exc).__name__}: {exc}"
                    ),
                }

            results.append(result)

    successful = sum(
        result["status"] == "EXECUTED"
        for result in results
    )

    return {
        "report_version": "5C.5.3-v1",
        "evaluation_stage": EXPECTED_STAGE,
        "generated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),

        "authority": "deterministic",
        "model_role": "read_only_candidate_and_explanation",

        "benchmark": {
            "path": str(
                BENCHMARK_PATH.relative_to(ROOT)
            ),
            "dataset_version": EXPECTED_BENCHMARK_VERSION,
            "cases": len(cases),
        },

        "execution": {
            "total_cases": len(cases),
            "successful_cases": successful,
            "execution_errors": execution_errors,
            "success_rate_percent": (
                round(
                    successful * 100.0 / len(cases),
                    2,
                )
                if cases
                else 0.0
            ),
        },

        "comparison": {
            "performed": False,
            "reason": (
                "5C.5.3 records actual deterministic "
                "pipeline output only. Expected-vs-actual "
                "gold comparison belongs to the next "
                "evaluation checkpoint."
            ),
        },

        "ai_boundary": {
            "llm_invoked": False,
            "ai_authority": "none",
            "financial_decision_authority": "deterministic",
        },

        "corrupted_record_policy": {
            "ingestion_validation_unchanged": True,
            "rejected_records_sent_to_matching": False,
            "terminal_exception_code": (
                ExceptionCode.CORRUPTED_RECORD.value
            ),
        },

        "case_materialization_policy": {
            "primary_records_preserved": True,
            "sibling_records_preserved": True,
            "duplicate_records_preserved": True,
            "records_synthesized": False,
            "financial_fields_recomputed": False,
            "transaction_ids_rewritten": False,
            "exact_duplicate_json_objects_collapsed": True,
            "candidate_selection_performed": False,
        },

        "cases": results,
    }


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def print_report(
    report: dict[str, Any],
) -> None:

    execution = report["execution"]

    print("=" * 72)
    print(
        "5C.5.3 DETERMINISTIC E2E PIPELINE EXECUTION"
    )
    print("=" * 72)
    print()

    print(
        f"Benchmark cases:       "
        f"{execution['total_cases']}"
    )

    print(
        f"Successful executions: "
        f"{execution['successful_cases']}"
    )

    print(
        f"Execution errors:      "
        f"{execution['execution_errors']}"
    )

    print(
        f"Success rate:          "
        f"{execution['success_rate_percent']:.2f}%"
    )

    print()

    if execution["execution_errors"] == 0:
        print(
            "5C.5.3 RESULT: "
            "DETERMINISTIC E2E EXECUTION COMPLETE"
        )
    else:
        print(
            "5C.5.3 RESULT: "
            "EXECUTION ERRORS DETECTED"
        )

        print()

        for case in report["cases"]:
            if case["status"] == "EXECUTION_ERROR":
                print(
                    f"  {case['case_id']} "
                    f"{case['error']}"
                )

    # Explicit visibility for the corrupted-record boundary.
    corrupted_cases = [
        case
        for case in report["cases"]
        if case["category"] in CORRUPTED_CATEGORIES
    ]

    if corrupted_cases:
        print()
        print("Corrupted-record terminal outcomes:")

        for case in corrupted_cases:
            decision = case.get(
                "deterministic_decision"
            )

            if decision is None:
                print(
                    f"  {case['case_id']} "
                    f"NO_DETERMINISTIC_DECISION"
                )
                continue

            print(
                f"  {case['case_id']} "
                f"{decision['status']} / "
                f"{decision['exception_code']}"
            )

    print()
    print(
        f"Artifact: "
        f"{OUTPUT_PATH}"
    )


def main() -> None:
    print("=" * 72)
    print(
        "5C.5.3 DETERMINISTIC E2E PIPELINE EXECUTION"
    )
    print("=" * 72)

    benchmark = load_json(
        BENCHMARK_PATH
    )

    cases = validate_benchmark(
        benchmark
    )

    report = build_results(
        cases
    )

    write_json(
        OUTPUT_PATH,
        report,
    )

    print_report(
        report
    )

    if report["execution"]["execution_errors"]:
        raise SystemExit(1)

    print(
        "5C.5.3 deterministic E2E execution: COMPLETE"
    )


if __name__ == "__main__":
    main()