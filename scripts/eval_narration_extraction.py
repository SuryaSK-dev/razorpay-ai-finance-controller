"""
Phase 5C.3 — Real Gemini narration extraction evaluation.

Compares REAL Gemini narration extraction against the frozen
Phase 5C.1 held-out dataset.

This stage measures extraction quality only.

The LLM:
    - may propose a transaction ID
    - may return no transaction ID
    - cannot make a financial decision
    - cannot modify a MatchDecision
    - cannot bypass the existing Phase 5 guardrail

This is NOT the final baseline-vs-financial-decision evaluation.

Important evaluation rules:
    - Provider failures are NOT counted as model abstentions.
    - Timeouts are NOT counted as model abstentions.
    - Quota failures are NOT counted as model abstentions.
    - Parse/validation failures are NOT counted as model abstentions.
    - Extraction metrics are calculated only over cases for which
      the model produced a valid AgentCallResult.
    - Evaluation coverage is reported separately.
    - Requests are paced to respect the observed Gemini free-tier
      request-per-minute limit.
    - No automatic retry is performed after a 429.
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from src.agent.config import load_agent_config
from src.agent.narration_extractor import extract_txn_id_via_llm
from src.agent.providers.gemini_provider import GeminiProvider


DATASET_PATH = ROOT / "data" / "eval" / "held_out_narrations.json"

# Observed Gemini Free Tier limit from the previous evaluation:
# 15 generate_content requests per minute.
#
# Five seconds between requests keeps the 20-case benchmark outside
# a single 60-second burst while remaining reasonably efficient.
REQUEST_INTERVAL_SECONDS = 5.0


VALID_MODEL_OUTCOMES = {
    "success",
    "abstention",
}

FAILURE_OUTCOMES = {
    "timeout",
    "quota_failure",
    "parse_failure",
    "provider_failure",
}


@dataclass
class CaseResult:
    case_id: str
    category: str
    expected: str | None
    predicted: str | None
    succeeded: bool
    error: str | None
    latency_seconds: float | None
    outcome: str

    @property
    def evaluated(self) -> bool:
        """
        True only when Gemini returned a valid model-level outcome.

        A provider timeout, quota failure, or parsing failure is an
        operational failure, not a model abstention.
        """
        return self.outcome in VALID_MODEL_OUTCOMES

    @property
    def correct(self) -> bool:
        """
        Case correctness is meaningful only for evaluated cases.
        """
        return self.evaluated and self.predicted == self.expected

    @property
    def proposed(self) -> bool:
        """
        True only when the model successfully proposed a transaction ID.
        """
        return (
            self.outcome == "success"
            and self.predicted is not None
        )

    @property
    def false_proposal(self) -> bool:
        """
        A false proposal is a model-generated transaction ID that does
        not equal the expected transaction ID.

        For negative/ambiguous cases, expected=None, so any proposal is
        a false proposal.
        """
        return (
            self.proposed
            and self.predicted != self.expected
        )


def load_dataset() -> dict:
    with DATASET_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def classify_failure(result) -> str:
    """
    Classify an AgentCallResult into a controlled evaluation outcome.

    This deliberately distinguishes:

        successful extraction
        successful abstention
        timeout
        quota exhaustion
        parse/validation failure
        other provider failure

    These categories must never be collapsed into a single
    'predicted=None' bucket because doing so would contaminate
    extraction-quality metrics.
    """

    if result.succeeded:
        if result.value is None:
            return "abstention"

        if result.value.proposed_txn_id is None:
            return "abstention"

        return "success"

    error = (result.error or "").lower()

    if (
        "timeout" in error
        or "timed out" in error
        or "exceeded 10s" in error
    ):
        return "timeout"

    if (
        "429" in error
        or "resource_exhausted" in error
        or "quota" in error
    ):
        return "quota_failure"

    if (
        "parse" in error
        or "schema" in error
        or "validation" in error
        or "required schema" in error
    ):
        return "parse_failure"

    return "provider_failure"


def evaluate_case(
    case: dict,
    provider: GeminiProvider,
) -> CaseResult:
    narration = case["narration"]
    expected = case["expected_txn_id"]

    start = time.perf_counter()

    try:
        result = extract_txn_id_via_llm(
            narration,
            provider.as_callable(),
        )

        elapsed = time.perf_counter() - start

        outcome = classify_failure(result)

        predicted = (
            result.value.proposed_txn_id
            if (
                result.succeeded
                and result.value is not None
            )
            else None
        )

        return CaseResult(
            case_id=case["case_id"],
            category=case["category"],
            expected=expected,
            predicted=predicted,
            succeeded=result.succeeded,
            error=result.error,
            latency_seconds=elapsed,
            outcome=outcome,
        )

    except Exception as exc:
        elapsed = time.perf_counter() - start

        return CaseResult(
            case_id=case["case_id"],
            category=case["category"],
            expected=expected,
            predicted=None,
            succeeded=False,
            error=f"{type(exc).__name__}: {exc}",
            latency_seconds=elapsed,
            outcome="provider_failure",
        )


def calculate_metrics(
    results: list[CaseResult],
) -> dict:
    """
    Calculate extraction-quality and operational metrics separately.

    Extraction metrics use only VALID_MODEL_OUTCOMES.

    Operational failures are reported separately so a Gemini quota
    outage cannot artificially make the model appear more conservative.
    """

    total = len(results)

    evaluated_results = [
        result
        for result in results
        if result.evaluated
    ]

    evaluated_cases = len(evaluated_results)

    correct = sum(
        result.correct
        for result in evaluated_results
    )

    positive_cases = sum(
        result.expected is not None
        for result in evaluated_results
    )

    true_positive = sum(
        result.expected is not None
        and result.predicted == result.expected
        for result in evaluated_results
    )

    proposed = sum(
        result.proposed
        for result in evaluated_results
    )

    false_proposals = sum(
        result.false_proposal
        for result in evaluated_results
    )

    abstentions = sum(
        result.outcome == "abstention"
        for result in results
    )

    successful_calls = sum(
        result.outcome in VALID_MODEL_OUTCOMES
        for result in results
    )

    timeout_failures = sum(
        result.outcome == "timeout"
        for result in results
    )

    quota_failures = sum(
        result.outcome == "quota_failure"
        for result in results
    )

    parse_failures = sum(
        result.outcome == "parse_failure"
        for result in results
    )

    provider_failures = sum(
        result.outcome == "provider_failure"
        for result in results
    )

    failed_calls = (
        timeout_failures
        + quota_failures
        + parse_failures
        + provider_failures
    )

    coverage = (
        evaluated_cases / total
        if total
        else 0.0
    )

    accuracy = (
        correct / evaluated_cases
        if evaluated_cases
        else 0.0
    )

    precision = (
        true_positive / proposed
        if proposed
        else 0.0
    )

    recall = (
        true_positive / positive_cases
        if positive_cases
        else 0.0
    )

    false_proposal_rate = (
        false_proposals / evaluated_cases
        if evaluated_cases
        else 0.0
    )

    abstention_rate = (
        abstentions / evaluated_cases
        if evaluated_cases
        else 0.0
    )

    latencies = [
        result.latency_seconds
        for result in results
        if (
            result.evaluated
            and result.latency_seconds is not None
        )
    ]

    median_latency = (
        median(latencies)
        if latencies
        else None
    )

    p95_latency = (
        percentile(latencies, 95)
        if latencies
        else None
    )

    return {
        "total": total,
        "evaluated_cases": evaluated_cases,
        "coverage": coverage,

        "correct": correct,
        "true_positive": true_positive,
        "proposed": proposed,
        "false_proposals": false_proposals,
        "abstentions": abstentions,

        "successful_calls": successful_calls,
        "failed_calls": failed_calls,

        "timeout_failures": timeout_failures,
        "quota_failures": quota_failures,
        "parse_failures": parse_failures,
        "provider_failures": provider_failures,

        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "false_proposal_rate": false_proposal_rate,
        "abstention_rate": abstention_rate,

        "median_latency_seconds": median_latency,
        "p95_latency_seconds": p95_latency,
    }


def percentile(
    values: list[float],
    percentile_value: float,
) -> float | None:
    """
    Simple nearest-rank-style percentile calculation.

    Values are expected to be non-empty.
    """
    if not values:
        return None

    ordered = sorted(values)

    index = round(
        (percentile_value / 100.0)
        * (len(ordered) - 1)
    )

    return ordered[index]


def print_metrics(metrics: dict) -> None:
    print("=" * 72)
    print("5C.3 REAL GEMINI NARRATION EXTRACTION EVALUATION")
    print("=" * 72)

    print(f"Dataset: {DATASET_PATH}")
    print(f"Cases: {metrics['total']}")

    print("\nEvaluation coverage")
    print("-" * 72)
    print(
        f"Evaluated cases:      "
        f"{metrics['evaluated_cases']}"
    )
    print(
        f"Coverage:             "
        f"{metrics['coverage']:.2%}"
    )

    print("\nModel extraction metrics")
    print("-" * 72)
    print(
        f"Correct:              "
        f"{metrics['correct']}"
    )
    print(
        f"True positives:       "
        f"{metrics['true_positive']}"
    )
    print(
        f"Proposals:            "
        f"{metrics['proposed']}"
    )
    print(
        f"False proposals:      "
        f"{metrics['false_proposals']}"
    )
    print(
        f"Abstentions:          "
        f"{metrics['abstentions']}"
    )

    print("\nOperational outcomes")
    print("-" * 72)
    print(
        f"Successful calls:     "
        f"{metrics['successful_calls']}"
    )
    print(
        f"Timeout failures:     "
        f"{metrics['timeout_failures']}"
    )
    print(
        f"Quota failures:       "
        f"{metrics['quota_failures']}"
    )
    print(
        f"Parse failures:       "
        f"{metrics['parse_failures']}"
    )
    print(
        f"Provider failures:    "
        f"{metrics['provider_failures']}"
    )
    print(
        f"Total failed calls:   "
        f"{metrics['failed_calls']}"
    )

    print("\nRates")
    print("-" * 72)
    print(
        f"Accuracy:             "
        f"{metrics['accuracy']:.2%}"
    )
    print(
        f"Precision:            "
        f"{metrics['precision']:.2%}"
    )
    print(
        f"Recall:               "
        f"{metrics['recall']:.2%}"
    )
    print(
        f"False proposal rate:  "
        f"{metrics['false_proposal_rate']:.2%}"
    )
    print(
        f"Abstention rate:      "
        f"{metrics['abstention_rate']:.2%}"
    )

    if metrics["median_latency_seconds"] is not None:
        print(
            f"Median latency:       "
            f"{metrics['median_latency_seconds']:.3f}s"
        )

    if metrics["p95_latency_seconds"] is not None:
        print(
            f"P95 latency:          "
            f"{metrics['p95_latency_seconds']:.3f}s"
        )


def print_category_breakdown(
    results: list[CaseResult],
) -> None:
    grouped = defaultdict(list)

    for result in results:
        grouped[result.category].append(result)

    print("\nCategory breakdown")
    print("-" * 72)

    for category in sorted(grouped):
        subset = grouped[category]

        evaluated = [
            result
            for result in subset
            if result.evaluated
        ]

        correct = sum(
            result.correct
            for result in evaluated
        )

        proposed = sum(
            result.proposed
            for result in evaluated
        )

        false_proposals = sum(
            result.false_proposal
            for result in evaluated
        )

        failures = sum(
            not result.evaluated
            for result in subset
        )

        print(
            f"{category:<32} "
            f"cases={len(subset):2d} "
            f"evaluated={len(evaluated):2d} "
            f"correct={correct:2d} "
            f"proposed={proposed:2d} "
            f"false={false_proposals:2d} "
            f"failures={failures:2d}"
        )


def print_case_results(
    results: list[CaseResult],
) -> None:
    print("\nCase-level results")
    print("-" * 72)

    for result in results:
        if not result.evaluated:
            status = "ERROR"
        elif result.correct:
            status = "PASS"
        else:
            status = "FAIL"

        latency = (
            f"{result.latency_seconds:.3f}s"
            if result.latency_seconds is not None
            else "n/a"
        )

        print(
            f"{result.case_id:<6} "
            f"{status:<5} "
            f"{result.category:<30} "
            f"expected={result.expected!r:<20} "
            f"predicted={result.predicted!r:<20} "
            f"outcome={result.outcome:<16} "
            f"latency={latency}"
        )

        if result.error:
            print(
                f"       error={result.error}"
            )


def main() -> None:
    dataset = load_dataset()

    if dataset.get("dataset_version") != "5C.1-v1":
        raise ValueError(
            "5C.3 must run against frozen dataset "
            "'5C.1-v1'."
        )

    cases = dataset.get("cases")

    if not cases:
        raise ValueError(
            "Held-out dataset contains no evaluation cases."
        )

    config = load_agent_config()
    provider = GeminiProvider(config)

    results: list[CaseResult] = []

    for index, case in enumerate(cases, start=1):
        print(
            f"Evaluating {index}/{len(cases)}: "
            f"{case['case_id']}"
        )

        results.append(
            evaluate_case(case, provider)
        )

        # Respect the observed Gemini Free Tier request limit.
        #
        # Do not sleep after the final request.
        if index < len(cases):
            time.sleep(REQUEST_INTERVAL_SECONDS)

    metrics = calculate_metrics(results)

    print()
    print_metrics(metrics)

    print_category_breakdown(results)
    print_case_results(results)

    print()
    print(
        "5C.3 real Gemini narration extraction evaluation: "
        "RUN COMPLETE"
    )

    print(
        "Metrics above separate model quality from "
        "provider/operational failures."
    )


if __name__ == "__main__":
    main()