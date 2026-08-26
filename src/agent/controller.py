# src/agent/controller.py
"""
Finance Controller Agent: the orchestration layer that makes Phase 5
an actual bounded agent rather than a bag of AI utility functions.

Responsibility, precisely: given an ALREADY-COMPUTED MatchDecision,
retrieve its evidence, optionally use the narration-extraction tool
for context, and produce a human-facing explanation. This layer
NEVER computes financial truth -- it only reasons about and narrates
truth that Phase 0-4 already established.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from src.models import MatchDecision
from src.agent.explainer import explain_decision_via_llm, fallback_template_explanation
from src.agent.guardrails import AgentCallResult
from src.agent.contracts import Explanation


@dataclass
class ControllerResponse:
    """What the agent returns for one decision -- explicitly
    separates financial facts (untouched, copied straight from the
    input MatchDecision) from agent-generated narration (the only
    thing this layer is allowed to produce)."""
    txn_id: str
    status: str                 # copied verbatim from MatchDecision
    exception_code: str         # copied verbatim from MatchDecision
    reason_codes: list[str]     # copied verbatim from MatchDecision
    confidence_score: int       # copied verbatim from MatchDecision
    explanation: str            # AGENT-GENERATED, the only new content
    explanation_source: str     # "llm" | "deterministic_fallback"
    agent_metadata: dict = field(default_factory=dict)


class FinanceControllerAgent:
    """
    The bounded agent. Every method takes an already-finished
    MatchDecision as input and never mutates it -- Python dataclasses
    are mutable by default, so this is enforced by DISCIPLINE plus
    the invariant test (test_agent_never_mutates_decision), not by
    the type system alone. Worth stating explicitly rather than
    overclaiming an enforcement mechanism that doesn't exist.
    """

    def __init__(self, llm_call_fn):
        self.llm_call_fn = llm_call_fn

    def explain(self, decision: MatchDecision) -> ControllerResponse:
        result: AgentCallResult[Explanation] = explain_decision_via_llm(decision, self.llm_call_fn)

        if result.succeeded:
            explanation_obj = result.value
            source = "llm"
        else:
            explanation_obj = fallback_template_explanation(decision)
            source = "deterministic_fallback"

        return ControllerResponse(
            txn_id=decision.txn_id,
            status=decision.status.value,
            exception_code=decision.exception_code.value,
            reason_codes=[c.value for c in decision.reason_codes],
            confidence_score=decision.confidence_score,
            explanation=explanation_obj.text,   # extract .text from the contract
            explanation_source=source,
            agent_metadata={
                "llm_latency_seconds": result.latency_seconds,
                "llm_error": result.error,
            },
        )

    def explain_batch(self, decisions: list[MatchDecision]) -> list[ControllerResponse]:
        return [self.explain(d) for d in decisions]