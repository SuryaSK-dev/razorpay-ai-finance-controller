# src/agent/contracts.py
"""
The formal boundary contract for everything an AI call is permitted
to return in this system. This is the structural answer to a
stronger claim than "LLM failure doesn't corrupt output" -- it's
"even a successful, confident, wrong LLM response cannot acquire
financial authority," because the TYPE of what crosses the AI
boundary makes that authority impossible to express.

Notice what is absent from every contract below: there is no field
anywhere in this file for amount, tax value, match status, or
decision outcome. The AI literally has no vocabulary to express a
financial fact -- only a proposal (subject to deterministic
verification) or a narration (read-only over facts already decided).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Literal


@dataclass(frozen=True)
class NarrationExtraction:
    """The ONLY shape a narration-extraction response can take.
    frozen=True means this object cannot be mutated after creation --
    a downstream bug attempting to alter it would raise, not silently
    corrupt state."""
    proposed_txn_id: Optional[str]
    confidence_hint: Literal["unspecified"] = "unspecified"  # CHANGED:
    # was Literal["low","medium","high"] with a hardcoded "medium" --
    # that implied model-derived confidence scoring that doesn't
    # exist. "unspecified" is the honest value until the prompt
    # actually asks the model to self-report and we validate that
    # signal means something.
    source: Literal["llm"] = "llm"

    def __post_init__(self):
        if self.proposed_txn_id is not None:
            import re
            if not re.match(r"^TXN_\d{5,8}$", self.proposed_txn_id):
                raise ValueError(
                    f"NarrationExtraction.proposed_txn_id={self.proposed_txn_id!r} "
                    f"does not match the required schema."
                )

@dataclass(frozen=True)
class Explanation:
    """The ONLY shape an explanation response can take. Notice:
    no status field, no exception_code field, no numeric fields at
    all. grounded_evidence_keys names WHICH evidence fields the
    explanation drew from, as a lightweight traceability check --
    not a guarantee of accuracy, but a structural nudge against
    fabrication."""
    text: str
    grounded_evidence_keys: tuple[str, ...] = field(default_factory=tuple)
    source: Literal["llm", "deterministic_fallback"] = "llm"

    def __post_init__(self):
        if not (20 <= len(self.text) <= 2000):
            raise ValueError(
                f"Explanation.text length {len(self.text)} outside "
                f"allowed bounds [20, 2000]"
            )


# =======================================================================
# EXPLICITLY, STRUCTURALLY FORBIDDEN
# There is no AgentAction, AgentDecision, or AgentStatus type in this
# file, and there never will be. Any future contribution attempting
# to add a class here that carries a DecisionStatus, a monetary
# Decimal, or an ExceptionCode is a direct violation of the Phase 5
# boundary and should be rejected in review on sight -- this comment
# exists specifically so that violation is easy to recognize.
# =======================================================================