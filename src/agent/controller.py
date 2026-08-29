# src/agent/controller.py
"""
Finance Controller Agent: the orchestration layer that makes this an
actual bounded agent rather than a bag of AI utility functions.

TWO CAPABILITIES
----------------
    explain(decision)   -- Phase 5. Narrate one already-computed
                           MatchDecision. Unchanged.

    ask(question)       -- Phase 6. Answer a natural-language question
                           about the batch by selecting a read-only
                           tool, running it deterministically, and
                           phrasing the real result.

Both obey the same rule: this layer NEVER computes financial truth. It
reasons about and narrates truth that Phase 0-4 already established.

HOW ask() IS BOUNDED
--------------------
Two model calls, neither of which can produce a number:

    1. SELECTION -- the model reads the question and the tool
       catalogue, and returns a tool name plus arguments. It has no
       access to the data at this point. It cannot answer; it can only
       choose which question to ask the deterministic layer.

    2. PHRASING -- the model receives the REAL tool output and writes
       it in English. It is instructed to use only the numbers given.

Between them sits `dispatch()`, which runs the actual tool. The numbers
in the final answer come from `decide_batch()` output via
`BatchQueryContext`, not from the model.

The raw tool result is returned alongside the phrased answer in
`AgentAnswer.data`, so an operator -- or a reviewer -- can always check
the prose against the numbers it claims to describe. If the model
misstates something, the evidence to catch it is in the same object.

FAILURE BEHAVIOUR
-----------------
Every model call goes through `call_llm_bounded()`, so the Phase 5
timeout, parse validation, and failure isolation apply unchanged.

    Selection fails  -> honest "could not determine which tool"
    Tool fails       -> the tool's own error, unembellished
    Phrasing fails   -> deterministic template over the real result

The last one matters most: a phrasing failure must not lose the answer.
The numbers are already computed at that point, so the fallback renders
them directly rather than returning nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from src.models import MatchDecision
from src.agent.explainer import (
    explain_decision_via_llm,
    fallback_template_explanation,
)
from src.agent.guardrails import AgentCallResult, call_llm_bounded
from src.agent.contracts import Explanation
from src.agent.tools.query_tools import BatchQueryContext
from src.agent.tools.registry import dispatch
from src.agent.tool_selection import (
    NO_TOOL,
    ToolSelection,
    build_selection_prompt,
    parse_selection,
    selection_rejection_reason,
    validate_selection,
)


# ======================================================================
# RESPONSE TYPES
# ======================================================================

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


@dataclass
class AgentAnswer:
    """
    What the agent returns for one natural-language question.

    `data` is the raw tool output and `answer` is the model's prose
    about it. Keeping both means the prose is always checkable against
    the numbers it describes -- an operator does not have to trust the
    phrasing step, because the source is attached.

    `answer_source` distinguishes:
        "llm"                     -- model phrased the real result
        "deterministic_fallback"  -- model failed; result rendered
                                     directly from the data
        "no_tool"                 -- no tool fits the question
        "error"                   -- selection or tool failed
    """
    question: str
    answer: str
    answer_source: str
    tool_used: Optional[str] = None
    tool_arguments: dict = field(default_factory=dict)
    data: Optional[dict] = None
    agent_metadata: dict = field(default_factory=dict)


# ======================================================================
# PHRASING PROMPT
# ======================================================================

_PHRASING_INSTRUCTIONS = """\
You are answering a finance operator's question about a batch of
reconciled transactions.

The data below was produced by a deterministic reconciliation engine.
It is the authoritative result.

RULES
-----
- Use ONLY numbers, IDs, statuses and codes that appear in the data.
- Do NOT calculate anything. Do not derive new figures, percentages or
  totals that are not already present.
- Do NOT add caveats, interpretations, or recommendations that the
  data does not support.
- If the data does not answer the question, say so plainly.
- Be direct and brief. Two to four sentences.

OPERATOR QUESTION
-----------------
{question}

DATA (authoritative -- from the deterministic engine)
-----------------------------------------------------
{data}

Write the answer now. Plain prose, no markdown, no preamble.
"""


def build_phrasing_prompt(question: str, data: dict[str, Any]) -> str:
    return _PHRASING_INSTRUCTIONS.format(
        question=question.strip(),
        data=json.dumps(data, indent=2, sort_keys=True),
    )


# ======================================================================
# AGENT
# ======================================================================

class FinanceControllerAgent:
    """
    The bounded agent.

    `explain()` takes an already-finished MatchDecision and never
    mutates it. MatchDecision is a Pydantic model rather than a plain
    dataclass, but it is not frozen, so non-mutation is enforced by
    DISCIPLINE plus the invariant tests -- not by the type system.
    Worth stating explicitly rather than overclaiming an enforcement
    mechanism that does not exist.

    `ask()` additionally requires a BatchQueryContext. Without one the
    agent can still explain a decision it is handed, but cannot answer
    questions about a batch, and says so rather than improvising.
    """

    def __init__(
        self,
        llm_call_fn: Callable[[str], str],
        query_context: Optional[BatchQueryContext] = None,
    ):
        self.llm_call_fn = llm_call_fn
        self.query_context = query_context

    # ------------------------------------------------------------------
    # PHASE 5 -- EXPLAIN ONE DECISION (unchanged)
    # ------------------------------------------------------------------

    def explain(self, decision: MatchDecision) -> ControllerResponse:
        result: AgentCallResult[Explanation] = explain_decision_via_llm(
            decision, self.llm_call_fn
        )

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
            explanation=explanation_obj.text,
            explanation_source=source,
            agent_metadata={
                "llm_latency_seconds": result.latency_seconds,
                "llm_error": result.error,
            },
        )

    def explain_batch(
        self, decisions: list[MatchDecision]
    ) -> list[ControllerResponse]:
        return [self.explain(d) for d in decisions]

    # ------------------------------------------------------------------
    # PHASE 6 -- ANSWER A QUESTION ABOUT THE BATCH
    # ------------------------------------------------------------------

    def ask(self, question: str) -> AgentAnswer:
        """
        Answer one natural-language question about the batch.

        select tool -> run tool deterministically -> phrase the result
        """
        if self.query_context is None:
            return AgentAnswer(
                question=question,
                answer=(
                    "I have no batch loaded, so I cannot answer "
                    "questions about reconciliation results."
                ),
                answer_source="error",
                agent_metadata={"error": "no query_context supplied"},
            )

        if not question or not question.strip():
            return AgentAnswer(
                question=question,
                answer="I did not receive a question.",
                answer_source="error",
                agent_metadata={"error": "empty question"},
            )

        # --- 1. SELECTION ------------------------------------------------
        selection_result = self._select_tool(question)

        if not selection_result.succeeded:
            return AgentAnswer(
                question=question,
                answer=(
                    "I could not determine which tool answers that "
                    "question. Try asking about the match rate, the "
                    "exceptions, a specific transaction ID, or "
                    "throughput."
                ),
                answer_source="error",
                agent_metadata={
                    "stage": "selection",
                    "error": selection_result.error,
                    "llm_latency_seconds": selection_result.latency_seconds,
                    "raw_response": selection_result.raw_response,
                },
            )

        selection: ToolSelection = selection_result.value

        if selection.tool_name == NO_TOOL:
            return AgentAnswer(
                question=question,
                answer=(
                    "That question is outside what I can answer from "
                    "this batch. I can report the match rate, list "
                    "unresolved exceptions, show the evidence behind a "
                    "specific transaction, or report throughput."
                ),
                answer_source="no_tool",
                agent_metadata={
                    "stage": "selection",
                    "llm_latency_seconds": selection_result.latency_seconds,
                },
            )

        # --- 2. DETERMINISTIC EXECUTION ----------------------------------
        # Everything from here is real. The model has had its say about
        # WHICH question to ask; it has no influence on the answer.

        envelope = dispatch(
            self.query_context,
            selection.tool_name,
            selection.arguments,
        )

        if not envelope["ok"]:
            return AgentAnswer(
                question=question,
                answer=envelope["error"],
                answer_source="error",
                tool_used=selection.tool_name,
                tool_arguments=selection.arguments,
                agent_metadata={
                    "stage": "dispatch",
                    "error_type": envelope["error_type"],
                    "llm_latency_seconds": selection_result.latency_seconds,
                },
            )

        data = envelope["result"]

        # --- 3. PHRASING -------------------------------------------------
        phrasing_result = self._phrase_answer(question, data)

        if phrasing_result.succeeded:
            answer_text = phrasing_result.value
            source = "llm"
        else:
            # The numbers already exist. A phrasing failure must not
            # lose them.
            answer_text = _render_fallback(selection.tool_name, data)
            source = "deterministic_fallback"

        return AgentAnswer(
            question=question,
            answer=answer_text,
            answer_source=source,
            tool_used=selection.tool_name,
            tool_arguments=selection.arguments,
            data=data,
            agent_metadata={
                "selection_latency_seconds": selection_result.latency_seconds,
                "phrasing_latency_seconds": phrasing_result.latency_seconds,
                "phrasing_error": phrasing_result.error,
            },
        )

    # ------------------------------------------------------------------
    # INTERNALS
    # ------------------------------------------------------------------

    def _select_tool(self, question: str) -> AgentCallResult[ToolSelection]:
        """
        Ask the model which tool answers this question.

        Goes through call_llm_bounded, so `validate_selection` runs as
        the guardrail's validate_fn -- an invalid selection is rejected
        there and never reaches dispatch.
        """
        prompt = build_selection_prompt(question)

        return call_llm_bounded(
            call_fn=lambda: self.llm_call_fn(prompt),
            parse_fn=parse_selection,
            validate_fn=validate_selection,
        )

    def _phrase_answer(
        self, question: str, data: dict[str, Any]
    ) -> AgentCallResult[str]:
        """
        Ask the model to phrase the real result.

        Validation here is deliberately thin -- non-empty, not absurdly
        long. Verifying that the prose faithfully reflects the data is
        the job of the explanation validator and the faithfulness
        evaluation, not of a boolean in the call path. Attempting a
        substring check here would reject correct paraphrases and admit
        wrong ones, which is worse than not checking.
        """
        prompt = build_phrasing_prompt(question, data)

        return call_llm_bounded(
            call_fn=lambda: self.llm_call_fn(prompt),
            parse_fn=lambda raw: raw.strip(),
            validate_fn=lambda text: bool(text) and len(text) < 4000,
        )


# ======================================================================
# DETERMINISTIC FALLBACK RENDERING
# ======================================================================

def _render_fallback(tool_name: str, data: dict[str, Any]) -> str:
    """
    Render a tool result without the model.

    Used when phrasing fails. The result is already computed at that
    point, so returning nothing would discard a correct answer because
    a cosmetic step failed.

    Deliberately plain. This is a fallback, not a second attempt at
    good prose.
    """
    if tool_name == "get_match_rate":
        return (
            f"{data['matched']} of {data['total_records']} records "
            f"matched ({data['match_rate_pct']}%). "
            f"{data['unresolved']} unresolved. "
            f"By status: {data['by_status']}."
        )

    if tool_name == "get_exceptions":
        scope = (
            f" with status {data['filter_status']}"
            if data.get("filter_status") else ""
        )
        ids = ", ".join(
            item["txn_id"] for item in data["exceptions"][:10]
        )
        more = (
            f" (and {data['count'] - 10} more)"
            if data["count"] > 10 else ""
        )
        return (
            f"{data['count']} unresolved record(s){scope} out of "
            f"{data['total_records']}. {ids}{more}."
        )

    if tool_name == "get_evidence":
        return (
            f"{data['txn_id']}: status {data['status']}, "
            f"exception {data['exception_code']}, "
            f"reason codes {data['reason_codes']}, "
            f"confidence {data['confidence_score']} "
            f"({data['confidence_tier']}). "
            f"Rule fired: {data['matched_rule']}."
        )

    if tool_name == "get_throughput_report":
        if not data.get("available"):
            return f"No throughput benchmark available. {data.get('reason', '')}"
        return (
            f"Peak throughput {data['peak_records_per_second']} "
            f"records/second at batch size "
            f"{data['peak_at_batch_size']}. "
            f"Sizes tested: {data['batch_sizes']}. {data['caveat']}"
        )

    return json.dumps(data, indent=2, sort_keys=True)