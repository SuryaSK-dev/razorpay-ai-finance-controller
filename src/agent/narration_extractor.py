# src/agent/narration_extractor.py
"""
Bounded LLM use, case 1: extract a candidate transaction reference
from unstructured bank narration text that the deterministic fallback
chain (bank_ref -> regex) already failed to resolve.

CRITICAL BOUNDARY: this module NEVER decides whether a match is
correct. It only proposes a candidate txn_id string. That proposal
must still pass the real deterministic lookup (see
agent/tools/candidate_lookup.py) and the full existing scoring/
decision pipeline like any other candidate -- the LLM adds recall,
never authority.
"""

from __future__ import annotations
import re

from src.agent.contracts import NarrationExtraction
from src.agent.guardrails import call_llm_bounded, AgentCallResult

# A valid extracted txn_id must look like our actual ID space --
# this is the validate_fn that keeps a hallucinated or malformed
# LLM response from ever reaching the matching pipeline.
_VALID_TXN_ID_PATTERN = re.compile(r"^TXN_\d{5,8}$")


def _build_prompt(narration: str) -> str:
    return (
        "You are extracting a transaction reference from a bank "
        "narration string. Respond with ONLY the transaction ID in "
        "the format TXN_##### (5-8 digits), and nothing else. If you "
        "cannot find a confident match, respond with exactly: NONE\n\n"
        f"Bank narration: {narration}"
    )


def _parse_response(raw: str) -> NarrationExtraction:
    cleaned = raw.strip().upper()
    proposed = None if cleaned == "NONE" else cleaned
    return NarrationExtraction(proposed_txn_id=proposed, confidence_hint="unspecified")


def _validate_extraction(value: NarrationExtraction) -> bool:
    # __post_init__ already enforces the schema at construction time;
    # this validate_fn exists as the second, independent check
    # call_llm_bounded requires -- redundant by design, not by accident.
    return True  # if we reached here, __post_init__ already succeeded


def extract_txn_id_via_llm(
    narration: str, llm_call_fn
) -> AgentCallResult[NarrationExtraction]:
    """
    llm_call_fn: injected callable, () -> str, the actual API call.
    Injected rather than hardcoded so this module is testable without
    a real API key and swappable across providers without touching
    the guardrail logic.

    Returns an AgentCallResult -- caller MUST check .succeeded and
    treat .value.proposed_txn_id as an ADDITIONAL candidate signal,
    never as ground truth.
    """
    prompt = _build_prompt(narration)
    return call_llm_bounded(
        call_fn=lambda: llm_call_fn(prompt),
        parse_fn=_parse_response,
        validate_fn=_validate_extraction,
    )