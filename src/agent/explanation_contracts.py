"""
Phase 5C.4 — Read-only explanation contracts.

The explanation layer has NO financial authority.

The deterministic engine owns all financial facts.
The LLM may only produce human-readable explanatory text
based on those already-verified facts.

The model must never be allowed to:
- change status
- change reason codes
- change confidence
- change evidence
- change amounts
- create a financial decision
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExplanationFacts:
    """
    Immutable facts established by the deterministic financial engine.

    These values are authoritative.

    The LLM receives these facts as input but does not have the
    authority to modify them.
    """

    status: str
    reason_codes: tuple[str, ...]
    confidence_score: float | None
    evidence: tuple[str, ...]

    claimed_amount: str | None = None
    expected_amount: str | None = None

    claimed_tax: str | None = None
    expected_tax: str | None = None


@dataclass(frozen=True)
class ExplanationResponse:
    """
    Model-generated explanation.

    Only the explanation text is model-authored.

    Financial facts are deliberately NOT represented here.

    This prevents the model response from becoming a second
    source of financial authority.
    """

    explanation: str