# scripts/verify_real_model_integration.py
"""
5B.6 — Real-model integration verification.

Proves that a REAL Gemini response can pass through the existing
Phase 5 agent boundary without acquiring financial authority.

This is an integration verification, not a quality evaluation.

It intentionally operates on an already-created MatchDecision.
The model is therefore downstream of deterministic financial truth.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.agent.config import load_agent_config
from src.agent.controller import FinanceControllerAgent
from src.agent.providers.gemini_provider import GeminiProvider
from src.models import MatchDecision, DecisionStatus, ExceptionCode


def _sample_decision() -> MatchDecision:
    return MatchDecision(
        txn_id="TXN_REAL_MODEL_TEST",
        status=DecisionStatus.TAX_MISMATCH,
        confidence_score=94,
        exception_code=ExceptionCode.ERR_GST_MISMATCH,
        reason_codes=[
            ExceptionCode.ERR_GST_MISMATCH,
            ExceptionCode.ERR_TDS_VARIANCE,
        ],
        evidence={
            "gst": {
                "expected": "9.00",
                "claimed": "6.00",
            }
        },
    )


def main() -> None:
    config = load_agent_config()
    provider = GeminiProvider(config)

    # The real provider is adapted to the existing Phase 5 callable
    # contract. The controller remains provider-agnostic.
    agent = FinanceControllerAgent(provider.as_callable())

    decision = _sample_decision()
    before = copy.deepcopy(decision)

    response = agent.explain(decision)

    # ---------------------------------------------------------------
    # Financial authority invariants
    # ---------------------------------------------------------------

    assert decision.status == before.status, (
        "REAL MODEL CHANGED decision.status"
    )

    assert decision.exception_code == before.exception_code, (
        "REAL MODEL CHANGED decision.exception_code"
    )

    assert decision.reason_codes == before.reason_codes, (
        "REAL MODEL CHANGED decision.reason_codes"
    )

    assert decision.confidence_score == before.confidence_score, (
        "REAL MODEL CHANGED decision.confidence_score"
    )

    assert decision.evidence == before.evidence, (
        "REAL MODEL CHANGED decision.evidence"
    )

    # ---------------------------------------------------------------
    # Agent output checks
    # ---------------------------------------------------------------

    assert response.explanation_source == "llm", (
        f"Expected real LLM explanation, got "
        f"{response.explanation_source!r}"
    )

    assert response.explanation, (
        "Real Gemini returned no usable explanation"
    )

    provider_response = provider.last_response

    assert provider_response is not None, (
        "Provider did not retain ProviderResponse metadata"
    )

    print("=" * 72)
    print("5B.6 REAL-MODEL INTEGRATION VERIFICATION")
    print("=" * 72)

    print(f"Provider: Gemini")
    print(f"Model: {provider_response.model}")
    print(f"LLM succeeded: {response.explanation_source == 'llm'}")
    print(f"Latency: {provider_response.latency_seconds:.3f}s")
    print(f"Input tokens: {provider_response.input_tokens}")
    print(f"Output tokens: {provider_response.output_tokens}")
    print(
        f"Intended cost: "
        f"${provider_response.estimated_cost_usd:.6f}"
    )

    print("\nFinancial authority checks:")
    print("status:           UNCHANGED")
    print("exception_code:   UNCHANGED")
    print("reason_codes:     UNCHANGED")
    print("confidence_score: UNCHANGED")
    print("evidence:         UNCHANGED")

    print("\nReal-model explanation:")
    print(response.explanation)

    print("\n5B.6 integration verification: PASS")


if __name__ == "__main__":
    main()