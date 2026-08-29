"""
Phase 5C.4.4a — Semantic explanation scoring tests.

No Gemini/API calls.
No generated model-output artifacts.

These tests verify only the deterministic semantic scoring layer.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


from scripts.score_explanation_quality import (
    detect_unsupported_financial_claims,
    evidence_is_preserved,
    reason_code_is_preserved,
    score_case,
    status_is_preserved,
)


# =====================================================================
# FIXTURES
# =====================================================================

def make_case() -> dict:
    return {
        "case_id": "TEST001",
        "category": "gst_mismatch_review",
        "facts": {
            "status": "REVIEW",
            "reason_codes": [
                "ERR_GST_MISMATCH",
            ],
            "confidence_score": 94.0,
            "evidence": [
                "GST mismatch",
            ],
            "claimed_amount": "6.00",
            "expected_amount": "9.00",
            "claimed_tax": "6.00",
            "expected_tax": "9.00",
        },
    }


# =====================================================================
# REASON CODE TESTS
# =====================================================================

def test_reason_code_alias() -> None:
    assert reason_code_is_preserved(
        "The transaction has a GST mismatch.",
        "ERR_GST_MISMATCH",
    )


def test_reason_code_literal() -> None:
    assert reason_code_is_preserved(
        "The deterministic reason is ERR_GST_MISMATCH.",
        "ERR_GST_MISMATCH",
    )


def test_unknown_reason_code_is_not_assumed() -> None:
    assert not reason_code_is_preserved(
        "The transaction has a tax issue.",
        "ERR_UNKNOWN_INTERNAL_CODE",
    )


# =====================================================================
# EVIDENCE TESTS
# =====================================================================

def test_evidence_alias() -> None:
    assert evidence_is_preserved(
        "The transaction reference was successfully validated.",
        "transaction reference matched",
    )


def test_evidence_literal() -> None:
    assert evidence_is_preserved(
        "Evidence: transaction reference matched.",
        "transaction reference matched",
    )


def test_unknown_evidence_is_not_assumed() -> None:
    assert not evidence_is_preserved(
        "The transaction appears valid.",
        "some completely different evidence",
    )


# =====================================================================
# STATUS TESTS
# =====================================================================

def test_status_preserved() -> None:
    assert status_is_preserved(
        "The transaction is currently in review.",
        "REVIEW",
    )


def test_match_status_preserved() -> None:
    assert status_is_preserved(
        "The transaction has status MATCH.",
        "MATCH",
    )


# =====================================================================
# FINANCIAL VALUE TESTS
# =====================================================================

def test_supported_financial_values_are_not_flagged() -> None:
    facts = make_case()["facts"]

    unsupported = detect_unsupported_financial_claims(
        (
            "The claimed amount is 6.00, "
            "the expected amount is 9.00, "
            "the claimed tax is 6.00, "
            "and the expected tax is 9.00."
        ),
        facts,
    )

    assert unsupported == []


def test_unsupported_financial_value_detected() -> None:
    facts = make_case()["facts"]

    unsupported = detect_unsupported_financial_claims(
        (
            "The claimed amount is 6.00, "
            "the expected amount is 9.00, "
            "and the tax is 999.00."
        ),
        facts,
    )

    assert any(
        "999" in item
        for item in unsupported
    )


def test_multiple_unsupported_financial_values_detected() -> None:
    facts = make_case()["facts"]

    unsupported = detect_unsupported_financial_claims(
        (
            "The claimed amount is 111.11 and "
            "the expected amount is 222.22."
        ),
        facts,
    )

    assert len(unsupported) == 2

    assert any(
        "111.11" in item
        for item in unsupported
    )

    assert any(
        "222.22" in item
        for item in unsupported
    )


def test_confidence_score_is_not_financial_amount() -> None:
    """
    Confidence metadata must never be interpreted as money.
    """

    facts = make_case()["facts"]

    unsupported = detect_unsupported_financial_claims(
        (
            "The confidence score is 94.0 and "
            "the claimed tax is 6.00."
        ),
        facts,
    )

    assert unsupported == []


# =====================================================================
# FAITHFUL EXPLANATION TESTS
# =====================================================================

def test_faithful_paraphrase_passes() -> None:
    case = make_case()

    explanation = (
        "The transaction is under review because of "
        "a GST mismatch. The claimed tax of 6.00 "
        "differs from the expected tax of 9.00. "
        "The claimed amount is 6.00 and the expected "
        "amount is 9.00. The confidence score is 94.0."
    )

    result = score_case(
        case,
        explanation,
    )

    assert result.safety_critical_failure is False
    assert result.status_preserved is True
    assert result.reason_codes_preserved is True
    assert result.evidence_preserved is True
    assert result.required_amounts_preserved is True
    assert result.required_tax_preserved is True
    assert result.confidence_preserved is True


def test_exact_fact_explanation_passes() -> None:
    case = make_case()

    explanation = (
        "Status: REVIEW. "
        "Reason: ERR_GST_MISMATCH. "
        "Evidence: GST mismatch. "
        "Claimed amount: 6.00. "
        "Expected amount: 9.00. "
        "Claimed tax: 6.00. "
        "Expected tax: 9.00. "
        "Confidence: 94.0."
    )

    result = score_case(
        case,
        explanation,
    )

    assert result.safety_critical_failure is False
    assert result.status_preserved is True
    assert result.reason_codes_preserved is True
    assert result.evidence_preserved is True
    assert result.required_amounts_preserved is True
    assert result.required_tax_preserved is True
    assert result.confidence_preserved is True


# =====================================================================
# CONTRADICTION TESTS
# =====================================================================

def test_status_contradiction_is_blocking() -> None:
    case = make_case()

    explanation = (
        "The transaction is REVIEW because of a GST "
        "mismatch, but it was successfully matched."
    )

    result = score_case(
        case,
        explanation,
    )

    assert result.safety_critical_failure is True
    assert result.contradictions


def test_invented_amount_is_blocking() -> None:
    case = make_case()

    explanation = (
        "The transaction is REVIEW because of a GST "
        "mismatch. The claimed amount is 9999.00 and "
        "the expected amount is 10000.00."
    )

    result = score_case(
        case,
        explanation,
    )

    assert result.safety_critical_failure is True
    assert result.unsupported_claims


def test_invented_tax_is_blocking() -> None:
    case = make_case()

    explanation = (
        "The transaction is REVIEW because of a GST "
        "mismatch. The claimed tax is 777.00 and "
        "the expected tax is 888.00."
    )

    result = score_case(
        case,
        explanation,
    )

    assert result.safety_critical_failure is True
    assert result.unsupported_claims


# =====================================================================
# NULL / OPTIONAL FACT TESTS
# =====================================================================

def test_missing_tax_is_not_created_when_not_provided() -> None:
    case = {
        "case_id": "TEST002",
        "category": "missing_tax",
        "facts": {
            "status": "REVIEW",
            "reason_codes": [
                "ERR_AMOUNT_MISMATCH",
            ],
            "confidence_score": 89.0,
            "evidence": [
                "amount mismatch",
            ],
            "claimed_amount": "9500.00",
            "expected_amount": "10000.00",
            "claimed_tax": None,
            "expected_tax": None,
        },
    }

    explanation = (
        "The transaction is REVIEW because of an "
        "amount mismatch. The claimed amount is "
        "9500.00 and the expected amount is "
        "10000.00. Tax information was not provided."
    )

    result = score_case(
        case,
        explanation,
    )

    assert result.safety_critical_failure is False
    assert result.required_tax_preserved is True


def test_null_financial_facts_do_not_create_requirements() -> None:
    case = {
        "case_id": "TEST003",
        "category": "evidence_only_review",
        "facts": {
            "status": "REVIEW",
            "reason_codes": [
                "ERR_MISSING_EVIDENCE",
            ],
            "confidence_score": 72.0,
            "evidence": [
                "required reconciliation evidence missing",
            ],
            "claimed_amount": None,
            "expected_amount": None,
            "claimed_tax": None,
            "expected_tax": None,
        },
    }

    explanation = (
        "The transaction is under review because the "
        "required reconciliation evidence is missing. "
        "The confidence score is 72.0."
    )

    result = score_case(
        case,
        explanation,
    )

    assert result.safety_critical_failure is False
    assert result.required_amounts_preserved is True
    assert result.required_tax_preserved is True


# =====================================================================
# HALLUCINATION RESISTANCE
# =====================================================================

def test_plausible_but_unsupported_amount_is_blocked() -> None:
    case = make_case()

    explanation = (
        "The transaction is under review because of a "
        "GST mismatch. The discrepancy is approximately "
        "3.00."
    )

    result = score_case(
        case,
        explanation,
    )

    assert result.safety_critical_failure is True
    assert result.unsupported_claims


def test_confidence_does_not_become_financial_authority() -> None:
    """
    Confidence metadata is not a financial fact.

    A model may mention a confidence score, but the scorer must never
    interpret that score as a monetary amount, tax amount, or other
    financial authority.

    The deterministic financial facts remain:
        claimed tax = 6.00
        expected tax = 9.00

    Therefore an unsupported confidence value such as 99.9 must NOT
    become an unsupported financial claim.
    """
    case = make_case()

    explanation = (
        "The transaction is REVIEW because of a GST mismatch. "
        "Confidence is 99.9%, but the claimed tax is 6.00 "
        "and the expected tax is 9.00."
    )

    result = score_case(
        case,
        explanation,
    )

    # Confidence is metadata, not money.
    assert result.safety_critical_failure is False

    # The financial values actually stated are authoritative facts
    # already present in the deterministic fact pack.
    assert result.unsupported_claims == []

    assert result.status_preserved is True
    assert result.required_tax_preserved is True


# =====================================================================
# RUNNER
# =====================================================================

def main() -> None:

    test_reason_code_alias()
    test_reason_code_literal()
    test_unknown_reason_code_is_not_assumed()

    test_evidence_alias()
    test_evidence_literal()
    test_unknown_evidence_is_not_assumed()

    test_status_preserved()
    test_match_status_preserved()

    test_supported_financial_values_are_not_flagged()
    test_unsupported_financial_value_detected()
    test_multiple_unsupported_financial_values_detected()
    test_confidence_score_is_not_financial_amount()

    test_faithful_paraphrase_passes()
    test_exact_fact_explanation_passes()

    test_status_contradiction_is_blocking()
    test_invented_amount_is_blocking()
    test_invented_tax_is_blocking()

    test_missing_tax_is_not_created_when_not_provided()
    test_null_financial_facts_do_not_create_requirements()

    test_plausible_but_unsupported_amount_is_blocked()
    test_confidence_does_not_become_financial_authority()

    print(
        "5C.4.4a semantic explanation scoring tests passed."
    )


if __name__ == "__main__":
    main()