# scripts/smoke_test_gemini.py
"""
5B.4 — Minimal real Gemini connectivity smoke test.

This test intentionally does NOT touch:
- transaction data
- matching
- candidate lookup
- tax logic
- MatchDecision
- agent controller

It proves only that:
    configuration -> GeminiProvider -> Gemini API -> ProviderResponse

works with the configured free-tier model.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.agent.config import load_agent_config
from src.agent.providers.gemini_provider import GeminiProvider


def main() -> None:
    config = load_agent_config()
    provider = GeminiProvider(config)

    response = provider.call(
        "Respond with exactly: OK"
    )

    print("Gemini smoke test: PASS")
    print(f"Model: {response.model}")
    print(f"Response: {response.text}")
    print(f"Latency: {response.latency_seconds:.3f}s")
    print(f"Input tokens: {response.input_tokens}")
    print(f"Output tokens: {response.output_tokens}")
    print(f"Estimated cost: ${response.estimated_cost_usd:.6f}")


if __name__ == "__main__":
    main()