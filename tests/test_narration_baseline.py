"""
Regression tests for the Phase 5C.2 deterministic narration baseline.

These tests ensure the evaluation harness uses the existing Phase-2
extractor and that the metric calculations behave correctly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(
    str(Path(__file__).resolve().parent.parent)
)


from scripts.eval_narration_baseline import (
    CaseResult,
    calculate_metrics,
)
from src.normalization.engine import (
    _extract_txn_from_narration,
)


def test_existing_txn_underscore_pattern():
    result = _extract_txn_from_narration(
        "NEFT CR TXN_00042 MERCH_001"
    )

    assert result == "TXN_00042"


def test_existing_txn_hyphen_pattern():
    result = _extract_txn_from_narration(
        "PAYMENT TXN-2026-0042 RECEIVED"
    )

    assert result == "TXN-2026-0042"


def test_existing_pyt_pattern():
    result = _extract_txn_from_narration(
        "SETTLEMENT PYT_1234567 COMPLETE"
    )

    assert result == "PYT_1234567"


def test_unrecognized_narration_returns_none():
    result = _extract_txn_from_narration(
        "PAYMENT RECEIVED WITHOUT TRANSACTION REFERENCE"
    )

    assert result is None


def test_case_insensitive_extraction_preserves_uppercase_output():
    result = _extract_txn_from_narration(
        "neft cr txn_00042 merchant payment"
    )

    assert result == "TXN_00042"


def test_metrics_perfect_case():
    results = [
        CaseResult(
            case_id="A",
            category="exact",
            expected="TXN_00042",
            predicted="TXN_00042",
        ),
        CaseResult(
            case_id="B",
            category="none",
            expected=None,
            predicted=None,
        ),
    ]

    metrics = calculate_metrics(results)

    assert metrics["total"] == 2
    assert metrics["correct"] == 2
    assert metrics["true_positive"] == 1
    assert metrics["proposed"] == 1
    assert metrics["false_proposals"] == 0
    assert metrics["accuracy"] == 1.0
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 1.0


def test_false_proposal_is_counted():
    results = [
        CaseResult(
            case_id="A",
            category="ambiguous",
            expected=None,
            predicted="TXN_00042",
        )
    ]

    metrics = calculate_metrics(results)

    assert metrics["false_proposals"] == 1
    assert metrics["proposed"] == 1
    assert metrics["precision"] == 0.0
    assert metrics["false_proposal_rate"] == 1.0


def test_abstention_is_not_a_false_proposal():
    results = [
        CaseResult(
            case_id="A",
            category="none",
            expected=None,
            predicted=None,
        )
    ]

    metrics = calculate_metrics(results)

    assert metrics["abstentions"] == 1
    assert metrics["false_proposals"] == 0