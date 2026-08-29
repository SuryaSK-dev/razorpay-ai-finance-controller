"""
Phase 5C.4 — Explanation validator tests.

These tests verify that the explanation layer cannot silently
contradict deterministic financial facts.

No API call is made.
"""

from __future__ import annotations

import sys
from pathlib import Path


# Make the repository root importable when this file is executed as:
#
#     python tests/test_explanation_validator.py
#
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


from src.agent.explanation_contracts import (
    ExplanationFacts,
    ExplanationResponse,
)
from src.agent.explanation_validator import (
    validate_explanation,
)


def make_facts() -> ExplanationFacts:
    return ExplanationFacts(
        status="REVIEW",
        reason_codes=(
            "ERR_GST_MISMATCH",
        ),
        confidence_score=94.0,
        evidence=(
            "GST mismatch",
        ),
        claimed_amount="6.00",
        expected_amount="9.00",
        claimed_tax="6.00",
        expected_tax="9.00",
    )


def test_faithful_explanation_passes() -> None:
    facts = make_facts()

    response = ExplanationResponse(
        explanation=(
            "The transaction is REVIEW because "
            "ERR_GST_MISMATCH was identified. "
            "The evidence shows GST mismatch: "
            "the claimed amount is 6.00 while the "
            "expected amount is 9.00. "
            "The claimed tax is 6.00 and the expected "
            "tax is 9.00."
        )
    )

    valid, violations = validate_explanation(
        facts,
        response,
    )

    assert valid is True
    assert violations == []


def test_empty_explanation_fails() -> None:
    facts = make_facts()

    response = ExplanationResponse(
        explanation=""
    )

    valid, violations = validate_explanation(
        facts,
        response,
    )

    assert valid is False
    assert "empty_explanation" in violations


def test_missing_status_fails() -> None:
    facts = make_facts()

    response = ExplanationResponse(
        explanation=(
            "ERR_GST_MISMATCH was identified. "
            "GST mismatch shows 6.00 versus 9.00."
        )
    )

    valid, violations = validate_explanation(
        facts,
        response,
    )

    assert valid is False
    assert "missing_verified_status" in violations


def test_missing_reason_code_fails() -> None:
    facts = make_facts()

    response = ExplanationResponse(
        explanation=(
            "The transaction is REVIEW because "
            "a GST mismatch was identified. "
            "The claimed amount is 6.00 and the "
            "expected amount is 9.00."
        )
    )

    valid, violations = validate_explanation(
        facts,
        response,
    )

    assert valid is False
    assert (
        "missing_reason_code:ERR_GST_MISMATCH"
        in violations
    )


def test_missing_financial_fact_fails() -> None:
    facts = make_facts()

    response = ExplanationResponse(
        explanation=(
            "The transaction is REVIEW because "
            "ERR_GST_MISMATCH was identified. "
            "The claimed amount is 6.00."
        )
    )

    valid, violations = validate_explanation(
        facts,
        response,
    )

    assert valid is False
    assert (
        "missing_expected_amount:9.00"
        in violations
    )


def test_contradictory_status_fails() -> None:
    facts = make_facts()

    response = ExplanationResponse(
        explanation=(
            "The transaction is REVIEW because "
            "ERR_GST_MISMATCH was identified. "
            "The transaction was also matched successfully. "
            "The claimed amount is 6.00 and expected amount "
            "is 9.00. GST mismatch is present."
        )
    )

    valid, violations = validate_explanation(
        facts,
        response,
    )

    assert valid is False
    assert (
        "contradictory_status:matched"
        in violations
    )


def test_facts_are_immutable() -> None:
    facts = make_facts()

    try:
        facts.status = "MATCH"
        raise AssertionError(
            "ExplanationFacts must be immutable"
        )
    except AttributeError:
        pass


def test_explanation_is_immutable() -> None:
    response = ExplanationResponse(
        explanation="REVIEW because ERR_GST_MISMATCH"
    )

    try:
        response.explanation = "MATCH"
        raise AssertionError(
            "ExplanationResponse must be immutable"
        )
    except AttributeError:
        pass


def main() -> None:
    test_faithful_explanation_passes()
    test_empty_explanation_fails()
    test_missing_status_fails()
    test_missing_reason_code_fails()
    test_missing_financial_fact_fails()
    test_contradictory_status_fails()
    test_facts_are_immutable()
    test_explanation_is_immutable()

    print(
        "5C.4.1 explanation contract and validator tests passed "
        "-- deterministic financial facts remain authoritative."
    )


if __name__ == "__main__":
    main()