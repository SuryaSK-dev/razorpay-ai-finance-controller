# src/agent/providers/base.py
"""
Provider-neutral interface for Phase 5 real-model integration.

Every LLM provider must implement exactly this interface. Provider-
specific SDKs, authentication, model configuration, and transport
details must remain inside the concrete provider implementation.

This layer has NO financial authority:
- no matching
- no tax calculation
- no decisioning
- no candidate validation
- no financial mutation

The existing call_llm_bounded() guardrail remains the enforcement
boundary for every actual model call.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderResponse:
    """
    Provider-level observability metadata.

    This describes what the provider returned and how the API call
    behaved. It is NOT a financial decision and must never contain
    financial authority fields such as status, amount, tax, or
    exception codes.
    """

    text: str
    model: str
    latency_seconds: float
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


class LLMProvider(ABC):
    """
    Provider-neutral interface.

    Concrete providers implement call() and raise exceptions on
    provider/API failures. The existing call_llm_bounded() function
    owns timeout and failure handling.
    """

    def __init__(self) -> None:
        self.last_response: ProviderResponse | None = None

    @abstractmethod
    def call(self, prompt: str) -> ProviderResponse:
        """
        Execute one LLM request.

        Implementations must:
        - return ProviderResponse on success
        - raise on API/provider failure
        - perform no financial decisioning
        - never bypass call_llm_bounded()
        """
        raise NotImplementedError

    def as_callable(self):
        """
        Adapt the provider to the existing Phase 5 callable contract:

            (prompt: str) -> str

        call_llm_bounded() remains unaware of the concrete provider.

        The complete ProviderResponse is retained on last_response for
        observability, while only response.text crosses into the
        existing parsing/validation pipeline.
        """

        def _fn(prompt: str) -> str:
            response = self.call(prompt)
            self.last_response = response
            return response.text

        return _fn