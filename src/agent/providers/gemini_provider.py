# src/agent/providers/gemini_provider.py
"""
Gemini Free-Tier provider for Phase 5B.

This module owns ONLY provider-specific concerns:
- Gemini SDK initialization
- API authentication
- model selection
- request configuration
- provider response metadata

It has NO financial authority.

Every actual model invocation is still made through the existing
call_llm_bounded() boundary by the caller.

Strict project constraint:
    AGENT_FREE_ONLY=True

No paid fallback, automatic model escalation, or billing activation
is implemented here.
"""

from __future__ import annotations

import time

from google import genai
from google.genai import types

from src.agent.config import AgentConfig
from src.agent.providers.base import LLMProvider, ProviderResponse


class GeminiProvider(LLMProvider):
    """
    Concrete Gemini implementation of the provider-neutral interface.

    The provider returns ProviderResponse only. It does not interpret
    the response as a financial decision.
    """

    def __init__(self, config: AgentConfig) -> None:
        super().__init__()

        if not config.free_only:
            raise RuntimeError(
                "GeminiProvider refuses to start because "
                "AGENT_FREE_ONLY is disabled. This project requires "
                "strict $0 API usage."
            )

        self.config = config

        self.client = genai.Client(
            api_key=config.gemini_api_key,
        )

    def call(self, prompt: str) -> ProviderResponse:
        """
        Execute one Gemini generation request.

        Raises:
            Exception: provider/API failures are intentionally allowed
            to propagate to call_llm_bounded(), which owns the failure
            and timeout policy.
        """

        if not prompt or not prompt.strip():
            raise ValueError("Gemini prompt cannot be empty.")

        start = time.perf_counter()

        response = self.client.models.generate_content(
            model=self.config.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=self.config.max_output_tokens,
            ),
        )

        latency = time.perf_counter() - start

        text = getattr(response, "text", None)

        if not text or not text.strip():
            raise RuntimeError(
                "Gemini returned an empty response."
            )

        usage = getattr(response, "usage_metadata", None)

        input_tokens = int(
            getattr(usage, "prompt_token_count", 0) or 0
        )

        output_tokens = int(
            getattr(usage, "candidates_token_count", 0) or 0
        )

        return ProviderResponse(
            text=text.strip(),
            model=self.config.gemini_model,
            latency_seconds=latency,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            # We intentionally report zero here because this project
            # is operating under a strict Free-Tier-only constraint.
            # This is NOT a claim about hypothetical paid pricing.
            estimated_cost_usd=0.0,
        )