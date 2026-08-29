"""
Phase 5C.2 — Deterministic narration extraction baseline.

Measures the EXISTING Phase-2 deterministic narration extractor
against the frozen Phase-5C.1 held-out dataset.

This script does NOT:
- introduce new extraction logic
- call an LLM
- perform candidate matching
- alter financial decisions
- modify Phase-2 behavior

It is measurement only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from dataclasses import dataclass

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from src.normalization.engine import _extract_txn_from_narration


DATASET_PATH = ROOT / "data" / "eval" / "held_out_narrations.json"


@dataclass
class CaseResult:
    case_id: str
    category: str
    expected: str | None
    predicted: str | None

    @property
    def correct(self) -> bool:
        return self.predicted == self.expected

    @property
    def proposed(self) -> bool:
        return self.predicted is not None

    @property
    def false_proposal(self) -> bool:
        return self.predicted is not None and self.predicted != self.expected


def load_dataset() -> dict:
    with DATASET_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def evaluate_case(case: dict) -> CaseResult:
    narration = case["narration"]
    expected = case["expected_txn_id"]

    predicted = _extract_txn_from_narration(narration)

    return CaseResult(
        case_id=case["case_id"],
        category=case["category"],
        expected=expected,
        predicted=predicted,
    )


def calculate_metrics(results: list[CaseResult]) -> dict:
    total = len(results)

    correct = sum(r.correct for r in results)

    # A "positive" ground-truth case is one where an expected ID
    # actually exists in the narration benchmark.
    positive_cases = sum(
        r.expected is not None
        for r in results
    )

    true_positive = sum(
        r.expected is not None
        and r.predicted == r.expected
        for r in results
    )

    proposed = sum(r.proposed for r in results)

    false_proposals = sum(
        r.false_proposal
        for r in results
    )

    abstentions = sum(
        r.predicted is None
        for r in results
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

    accuracy = (
        correct / total
        if total
        else 0.0
    )

    false_proposal_rate = (
        false_proposals / total
        if total
        else 0.0
    )

    abstention_rate = (
        abstentions / total
        if total
        else 0.0
    )

    return {
        "total": total,
        "correct": correct,
        "positive_cases": positive_cases,
        "true_positive": true_positive,
        "proposed": proposed,
        "false_proposals": false_proposals,
        "abstentions": abstentions,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "false_proposal_rate": false_proposal_rate,
        "abstention_rate": abstention_rate,
    }


def print_metrics(metrics: dict) -> None:
    print("=" * 72)
    print("5C.2 DETERMINISTIC NARRATION EXTRACTION BASELINE")
    print("=" * 72)

    print(f"Dataset: {DATASET_PATH}")
    print(f"Cases: {metrics['total']}")

    print("\nCore metrics")
    print("-" * 72)
    print(f"Correct:              {metrics['correct']}")
    print(f"True positives:       {metrics['true_positive']}")
    print(f"Proposals:            {metrics['proposed']}")
    print(f"False proposals:      {metrics['false_proposals']}")
    print(f"Abstentions:          {metrics['abstentions']}")

    print("\nRates")
    print("-" * 72)
    print(f"Accuracy:             {metrics['accuracy']:.2%}")
    print(f"Precision:            {metrics['precision']:.2%}")
    print(f"Recall:               {metrics['recall']:.2%}")
    print(
        f"False proposal rate:  "
        f"{metrics['false_proposal_rate']:.2%}"
    )
    print(
        f"Abstention rate:      "
        f"{metrics['abstention_rate']:.2%}"
    )


def print_category_breakdown(
    results: list[CaseResult],
) -> None:
    categories = sorted(
        {result.category for result in results}
    )

    print("\nCategory breakdown")
    print("-" * 72)

    for category in categories:
        subset = [
            result
            for result in results
            if result.category == category
        ]

        correct = sum(r.correct for r in subset)
        proposed = sum(r.proposed for r in subset)
        false_proposals = sum(
            r.false_proposal
            for r in subset
        )

        print(
            f"{category:<32} "
            f"cases={len(subset):2d} "
            f"correct={correct:2d} "
            f"proposed={proposed:2d} "
            f"false={false_proposals:2d}"
        )


def print_case_results(
    results: list[CaseResult],
) -> None:
    print("\nCase-level results")
    print("-" * 72)

    for result in results:
        status = "PASS" if result.correct else "FAIL"

        print(
            f"{result.case_id:<6} "
            f"{status:<5} "
            f"{result.category:<30} "
            f"expected={result.expected!r:<20} "
            f"predicted={result.predicted!r}"
        )


def main() -> None:
    dataset = load_dataset()

    if "cases" not in dataset:
        raise ValueError(
            "Held-out dataset must contain a 'cases' field."
        )

    cases = dataset["cases"]

    if not cases:
        raise ValueError(
            "Held-out dataset contains no evaluation cases."
        )

    results = [
        evaluate_case(case)
        for case in cases
    ]

    metrics = calculate_metrics(results)

    print_metrics(metrics)
    print_category_breakdown(results)
    print_case_results(results)

    print("\n5C.2 deterministic baseline: COMPLETE")


if __name__ == "__main__":
    main()