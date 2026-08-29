"""
Phase 5C.4 — Deterministic explanation faithfulness validator.

The validator checks whether an LLM-generated explanation remains
consistent with facts already established by the deterministic
financial engine.

The LLM has no authority to alter those facts.

A validation failure means:

    reject the explanation

It does NOT mean:

    change the financial decision
"""

from __future__ import annotations

import re

from src.agent.explanation_contracts import (
    ExplanationFacts,
    ExplanationResponse,
)


def _normalize(text: str) -> str:
    """
    Normalize text for conservative textual consistency checks.
    """
    return " ".join(text.lower().split())


def _contains(text: str, value: str) -> bool:
    """
    Case-insensitive normalized containment check.
    """
    return _normalize(value) in _normalize(text)


def validate_explanation(
    facts: ExplanationFacts,
    response: ExplanationResponse,
) -> tuple[bool, list[str]]:
    """
    Validate an explanation against deterministic facts.

    Returns:

        (True, [])

    when the explanation passes all checks.

    Otherwise:

        (False, [violations])

    The validator is intentionally conservative.
    """

    violations: list[str] = []

    explanation = response.explanation.strip()

    if not explanation:
        violations.append("empty_explanation")
        return False, violations

    normalized = _normalize(explanation)

    # ------------------------------------------------------------
    # 1. Verified status must be represented
    # ------------------------------------------------------------

    if facts.status:
        if not _contains(normalized, facts.status):
            violations.append(
                "missing_verified_status"
            )

    # ------------------------------------------------------------
    # 2. Verified reason codes must be represented
    # ------------------------------------------------------------

    for reason_code in facts.reason_codes:
        if not _contains(
            normalized,
            reason_code,
        ):
            violations.append(
                f"missing_reason_code:{reason_code}"
            )

    # ------------------------------------------------------------
    # 3. Verified evidence must be represented
    # ------------------------------------------------------------

    for evidence_item in facts.evidence:
        if evidence_item and not _contains(
            normalized,
            evidence_item,
        ):
            violations.append(
                f"missing_evidence:{evidence_item}"
            )

    # ------------------------------------------------------------
    # 4. Verified financial amounts
    # ------------------------------------------------------------

    financial_values = (
        ("claimed_amount", facts.claimed_amount),
        ("expected_amount", facts.expected_amount),
        ("claimed_tax", facts.claimed_tax),
        ("expected_tax", facts.expected_tax),
    )

    for field_name, value in financial_values:
        if value is None:
            continue

        if not _contains(normalized, value):
            violations.append(
                f"missing_{field_name}:{value}"
            )

    # ------------------------------------------------------------
    # 5. Reject obvious contradictory decision language
    # ------------------------------------------------------------

    contradictory_statuses = {
        "MATCH": {
            "review",
            "rejected",
            "exception",
        },
        "REVIEW": {
            "matched",
            "approved",
            "settled",
        },
        "REJECT": {
            "matched",
            "approved",
        },
    }

    expected_status = facts.status.upper()

    for contradictory in contradictory_statuses.get(
        expected_status,
        set(),
    ):
        if re.search(
            rf"\b{re.escape(contradictory)}\b",
            normalized,
        ):
            violations.append(
                f"contradictory_status:{contradictory}"
            )

    return (
        len(violations) == 0,
        violations,
    )