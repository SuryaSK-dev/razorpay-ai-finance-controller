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
5C.5.2 does NOT independently reconstruct a second execution from
data/raw. The deterministic execution has already been completed by
scripts/run_e2e_deterministic.py and frozen as
data/eval/e2e_deterministic_results_5C5_3.json. This verifier consumes
that artifact as the actual-result source.

No Gemini calls. No AI-generated result participates in the
comparison. No ground_truth.json is read.

THREE OUTCOMES, NOT TWO
-----------------------
A case that does not match the frozen gold falls into one of three
buckets, and the distinction between them is the point:

    BASELINE_DIVERGENCE      an unexplained mismatch. Fails the gate.

    NOT_EVALUABLE_PER_CASE   the harness structurally CANNOT observe
                             this property. See
                             BATCH_RELATIONAL_CATEGORIES.

    KNOWN_POLICY_DIVERGENCE  the harness observes it correctly, and we
                             have decided the ENGINE is right and the
                             ground-truth label was optimistic. See
                             KNOWN_POLICY_DIVERGENCES.

Collapsing the last two would let an honesty mechanism hide a real
result. raw_divergent_cases is retained so a reviewer can always check
that raw == divergent + not_evaluable + known_policy, and a test
asserts that arithmetic.
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

EXPECTED_REPORT_VERSION = "5C.5.2-v2"

EXPECTED_CASE_COUNT = 63


# ======================================================================
# BATCH-RELATIONAL CATEGORIES
# ======================================================================
#
# scripts/run_e2e_deterministic.py executes each case in ISOLATION:
# materialize_case() writes one transaction's PG/bank/invoice rows into
# a private directory, and the real pipeline then runs over that
# directory alone.
#
# That isolation is correct for PER-RECORD properties -- tax
# arithmetic, amount agreement, referential integrity. Those depend
# only on the record itself, so a batch of one is a faithful test.
#
# It is structurally INCAPABLE of evaluating BATCH-RELATIONAL
# properties. Ambiguity means "another plausible record exists
# elsewhere in the batch". Duplicate detection means "this bank credit
# appears twice". In a batch of one, neither signal can exist by
# construction: find_bank_ambiguity_candidates() iterates a bank pool
# containing only the case's own row, correctly returns an empty list,
# and the decision engine correctly returns MATCHED.
#
# Verified directly via scripts/diagnose_ambiguity.py: run over the
# FULL batch, all six ambiguous-category records report
# is_ambiguous=True with 2-3 pieces of bank ambiguity evidence each.
# Same engine, same data, same code -- the only difference is the size
# of the candidate pool the harness hands it.
#
# These remain covered by the full-batch path:
#
#     tests/test_matching.py
#         test_ambiguous_candidates_resolved_deterministically_and_repeatably
#         test_ambiguous_result_never_auto_matchable
#     MatchSummary.ambiguous_flagged
#
# Removing this exclusion requires giving each case the rest of the
# batch as context. See FAILURE_LOG.md.

BATCH_RELATIONAL_CATEGORIES = frozenset({
    "ambiguous",
    "duplicate",
})


# ======================================================================
# KNOWN POLICY DIVERGENCES
# ======================================================================
#
# Cases where the harness sees the divergence clearly, we understand
# why it happens, and we have decided the ENGINE is right and the
# ground-truth label was optimistic.
#
# This is NOT the same as NOT_EVALUABLE_PER_CASE above. That bucket is
# for properties the per-case harness structurally cannot observe.
# This bucket is for properties it observes correctly, where the
# EXPECTATION rather than the observation needs revisiting.
#
# ---------------------------------------------------------------------
# reference_mismatch_fuzzy
# ---------------------------------------------------------------------
#
# UPGRADE B removed the BANKREF_<txn_id> convention from this
# category's bank rows so they would finally reach the fuzzy tier
# (FAILURE_LOG.md section 33: previously zero of 61 records did).
#
# They now do. The engine recovers all six by narration similarity
# with amount and date agreement enforced -- measured precision 1.00
# and recall 1.00 through threshold 90. It then declines to auto-match
# them, because scoring withholds SCORE_UTR_EXACT and
# SCORE_TXN_ID_BANK (neither is available), so confidence lands LOW
# and `low_confidence_requires_human_review` fires at priority 6.
#
#     expected  MATCHED / NONE
#     actual    HUMAN_REVIEW / REFERENCE_MISMATCH
#
# We are keeping the engine's behaviour. A settlement whose only link
# to a transaction is a fuzzy string match, with no structured
# identifier agreeing anywhere, is not something a finance system
# should auto-approve. Routing it to a human is the fail-closed
# outcome this architecture claims to produce, and here it produces it
# without being asked to.
#
# The ground-truth label was written while the fuzzy tier was
# unreachable, so it encoded an assumption that had never been tested.
# It is deliberately left as MATCHED: correcting it would be a third
# ground-truth edit, and a reviewer counting three starts asking
# whether the answer key follows the engine. Reporting 90.16% with six
# explained fail-closed divergences is a more honest artifact than
# 100% reached by moving the target.
#
# Also recorded in scripts/report_accuracy.py and README.md, so the
# number and its explanation never travel separately.

KNOWN_POLICY_DIVERGENCES: dict[str, str] = {
    "reference_mismatch_fuzzy": (
        "Recovered by the fuzzy tier, then routed to HUMAN_REVIEW "
        "because no structured identifier agrees -- only narration "
        "similarity. Fail-closed by design. Ground truth expected "
        "auto-match, an assumption made while the fuzzy tier was "
        "unreachable and therefore never tested."
    ),
}


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

    Prevents accidentally comparing the gold benchmark against an
    unrelated or stale JSON file.
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
    Compare the deterministic fields present in the frozen gold record.

    Do not compare fields the gold benchmark does not freeze.
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

        # Category lives at the top level of the frozen benchmark
        # case, not inside deterministic_expected_decision.
        category = frozen_case.get("category")

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
                    "not_evaluable": False,
                    "known_policy_divergence": False,
                    "policy_rationale": None,
                    "category": category,
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
        # The case executed but produced no deterministic decision.
        # Should never happen for a completed 5C.5.3 artifact.
        # ----------------------------------------------------------

        if actual is None:
            results.append(
                {
                    "case_id": case_id,
                    "txn_id": expected["txn_id"],
                    "status": "MISSING_ACTUAL",
                    "match": False,
                    "not_evaluable": False,
                    "known_policy_divergence": False,
                    "policy_rationale": None,
                    "category": category,
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

        # ----------------------------------------------------------
        # Classification.
        #
        # Order matters: the batch-relational check runs first because
        # "the harness cannot see this" is a stronger claim than "we
        # disagree with the label". A category should never be in both
        # sets, and a test asserts that.
        #
        # Both only apply when the case ACTUALLY diverges. A case in
        # either category that matches anyway is still a genuine
        # match -- neither bucket can absorb a passing case.
        # ----------------------------------------------------------

        diverged = not comparison["match"]

        not_evaluable = (
            diverged
            and category in BATCH_RELATIONAL_CATEGORIES
        )

        known_policy = (
            diverged
            and not not_evaluable
            and category in KNOWN_POLICY_DIVERGENCES
        )

        if not_evaluable:
            status = "NOT_EVALUABLE_PER_CASE"
        elif known_policy:
            status = "KNOWN_POLICY_DIVERGENCE"
        elif comparison["match"]:
            status = "MATCH"
        else:
            status = "BASELINE_DIVERGENCE"

        results.append(
            {
                "case_id": case_id,
                "txn_id": expected["txn_id"],
                "status": status,
                "match": comparison["match"],
                "not_evaluable": not_evaluable,
                "known_policy_divergence": known_policy,
                "policy_rationale": (
                    KNOWN_POLICY_DIVERGENCES[category]
                    if known_policy else None
                ),
                "category": category,
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

    not_evaluable_cases = sum(
        result.get("not_evaluable", False)
        for result in results
    )

    known_policy_cases = sum(
        result.get("known_policy_divergence", False)
        for result in results
    )

    # Divergences EXCLUDING both structurally unevaluable cases and
    # known, documented policy disagreements. This is the number the
    # stability gate keys on: it should contain only mismatches nobody
    # has yet explained.
    divergent_cases = sum(
        not result["match"]
        and not result.get("not_evaluable", False)
        and not result.get("known_policy_divergence", False)
        for result in results
    )

    # Retained for transparency: the raw mismatch count BEFORE any
    # exclusion. A reviewer can verify
    #
    #     raw == divergent + not_evaluable + known_policy
    #
    # so neither bucket can silently absorb a real regression. A test
    # asserts this arithmetic.
    raw_divergent_cases = sum(
        not result["match"]
        for result in results
    )

    not_evaluable_case_ids = sorted(
        result["case_id"]
        for result in results
        if result.get("not_evaluable", False)
    )

    known_policy_case_ids = sorted(
        result["case_id"]
        for result in results
        if result.get("known_policy_divergence", False)
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
    #
    # divergent_cases already excludes both documented buckets, so
    # this expression is unchanged.
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

        "scope": {
            "batch_relational_categories": sorted(
                BATCH_RELATIONAL_CATEGORIES
            ),
            "limitation": (
                "Per-case execution isolates each transaction, so "
                "properties that depend on other records existing in "
                "the batch (ambiguity, duplicate detection) cannot be "
                "evaluated here. These are covered by the full-batch "
                "path instead."
            ),
            "known_policy_divergences": KNOWN_POLICY_DIVERGENCES,
            "policy_note": (
                "A known policy divergence is NOT a blind spot. The "
                "harness observes it correctly; we have decided the "
                "engine is right and the ground-truth label was "
                "optimistic. Each such case carries its rationale."
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
            "not_evaluable_cases": not_evaluable_cases,
            "known_policy_divergence_cases": known_policy_cases,
            "raw_divergent_cases": raw_divergent_cases,
            "evaluable_cases": (
                len(frozen_cases) - not_evaluable_cases
            ),
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

        "not_evaluable_case_ids": not_evaluable_case_ids,

        "known_policy_divergence_case_ids": known_policy_case_ids,

        "case_results": results,
    }


# ======================================================================
# CLI
# ======================================================================

def write_report(report: dict[str, Any]) -> None:
    """
    Persist the verification report.

    A FAILED verification must still leave an auditable artifact
    explaining why it failed.
    """

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
        "Not evaluable (batch-relational): "
        f"{coverage['not_evaluable_cases']}"
    )

    print(
        "Known policy divergences: "
        f"{coverage['known_policy_divergence_cases']}"
    )

    print(
        "Raw mismatches (before exclusion): "
        f"{coverage['raw_divergent_cases']}"
    )

    print(
        "Evaluable cases: "
        f"{coverage['evaluable_cases']}"
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

    # ------------------------------------------------------------------
    # Always surface excluded cases, pass or fail. An exclusion that is
    # only visible on failure is an exclusion that can hide.
    # ------------------------------------------------------------------

    if coverage["not_evaluable_cases"]:
        print(
            "NOT EVALUABLE BY PER-CASE EXECUTION "
            "(covered by full-batch path):"
        )

        for result in report["case_results"]:
            if result.get("not_evaluable"):
                print(
                    f"  {result['case_id']} "
                    f"NOT_EVALUABLE_PER_CASE "
                    f"({result.get('category')})"
                )

        print()

    if coverage["known_policy_divergence_cases"]:
        print(
            "KNOWN POLICY DIVERGENCES "
            "(engine kept, label was optimistic):"
        )

        seen_rationales: set[str] = set()

        for result in report["case_results"]:
            if not result.get("known_policy_divergence"):
                continue

            print(
                f"  {result['case_id']} KNOWN_POLICY_DIVERGENCE "
                f"({result.get('category')})"
            )
            print(f"       {result['differences']}")

            rationale = result.get("policy_rationale")

            if rationale and rationale not in seen_rationales:
                seen_rationales.add(rationale)
                print()
                print(f"       {rationale}")
                print()

        print()

    if not report["baseline_stable"]:
        print(
            "5C.5.2 RESULT: BASELINE DRIFT DETECTED"
        )

        for result in report["case_results"]:
            if result["match"]:
                continue

            if result.get("not_evaluable"):
                continue

            if result.get("known_policy_divergence"):
                continue

            print(
                f"  {result['case_id']} "
                f"{result['status']}"
            )

            if result["differences"]:
                print(
                    "       differences="
                    f"{result['differences']}"
                )

        write_report(report)

        print()
        print(
            f"Artifact: {OUTPUT_PATH}"
        )

        raise SystemExit(1)

    print(
        "5C.5.2 RESULT: DETERMINISTIC GOLD BASELINE STABLE"
    )

    write_report(report)

    print()
    print(
        f"Artifact: {OUTPUT_PATH}"
    )

    print(
        "5C.5.2 deterministic gold baseline verification: COMPLETE"
    )


if __name__ == "__main__":
    main()