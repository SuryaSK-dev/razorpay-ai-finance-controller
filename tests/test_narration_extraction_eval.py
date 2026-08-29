"""
Phase 5C.3 evaluation-harness tests.

These tests do not call Gemini.

They verify that the metric calculations correctly distinguish:
- correct proposals
- false proposals
- abstentions
- failed provider calls
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(
    str(Path(__file__).resolve().parent.parent)
)

from scripts.eval_narration_extraction import (
    CaseResult,
    calculate_metrics,
)


def test_perfect_extraction_metrics():
    results = [
        CaseResult(
            case_id="N001", category="exact",
            expected="TXN_00042", predicted="TXN_00042",
            succeeded=True, error=None, latency_seconds=1.0,
            outcome="success",
        ),
        CaseResult(
            case_id="N002", category="none",
            expected=None, predicted=None,
            succeeded=True, error=None, latency_seconds=1.0,
            outcome="abstention",
        ),
    ]

    metrics = calculate_metrics(results)

    assert metrics["accuracy"] == 1.0
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 1.0
    assert metrics["false_proposals"] == 0
    assert metrics["abstentions"] == 1


def test_wrong_transaction_proposal_counts_as_false_proposal():
    results = [
        CaseResult(
            case_id="N001", category="ambiguous",
            expected=None, predicted="TXN_99999",
            succeeded=True, error=None, latency_seconds=1.0,
            outcome="success",
        )
    ]

    metrics = calculate_metrics(results)

    assert metrics["proposed"] == 1
    assert metrics["false_proposals"] == 1
    assert metrics["precision"] == 0.0
    assert metrics["false_proposal_rate"] == 1.0


def test_abstention_is_not_a_false_proposal():
    results = [
        CaseResult(
            case_id="N001", category="ambiguous",
            expected=None, predicted=None,
            succeeded=True, error=None, latency_seconds=1.0,
            outcome="abstention",
        )
    ]

    metrics = calculate_metrics(results)

    assert metrics["abstentions"] == 1
    assert metrics["false_proposals"] == 0


def test_failed_provider_call_is_recorded():
    """
    A provider failure is an OPERATIONAL failure, not a model abstention.
    Counting it as an abstention would let a Gemini outage masquerade as
    the model being appropriately cautious -- which is exactly the metric
    contamination the `outcome` field was introduced to prevent.
    """
    results = [
        CaseResult(
            case_id="N001", category="provider_failure",
            expected="TXN_00042", predicted=None,
            succeeded=False, error="LLM call failed", latency_seconds=1.2,
            outcome="provider_failure",
        )
    ]

    metrics = calculate_metrics(results)

    assert metrics["failed_calls"] == 1
    assert metrics["successful_calls"] == 0
    assert metrics["provider_failures"] == 1
    assert metrics["abstentions"] == 0        # was: == 1 -- old, wrong semantics
    assert metrics["evaluated_cases"] == 0    # excluded from extraction metrics