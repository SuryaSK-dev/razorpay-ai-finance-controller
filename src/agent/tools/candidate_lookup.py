# src/agent/tools/candidate_lookup.py
"""
Agent tool: looks up whether an LLM-proposed txn_id exists as a real
candidate in the EXISTING, UNMODIFIED Phase 3 index. This file calls
into candidates.py/matching engine as a read-only consumer -- it
never modifies matching logic, never adds a new tier inside
candidates.py itself. Phase 0-4 remains frozen at v0.8-phase4-final.
"""

from __future__ import annotations
from dataclasses import dataclass

from src.matching.candidates import CandidateIndex
from src.models import NormalizedRecord
from src.agent.contracts import NarrationExtraction


@dataclass
class ToolLookupResult:
    found: bool
    candidates: list[NormalizedRecord]
    source: str  # "llm_proposed_lookup" -- always labeled distinctly
                 # from the deterministic tiers' own match_type values


def lookup_proposed_txn_id(
    extraction: NarrationExtraction, index: CandidateIndex
) -> ToolLookupResult:
    """
    Accepts the FORMAL contract type, not a raw string -- this is
    the fix that closes the gap the review identified: contracts.py
    existed but candidate_lookup.py was still consuming a bare str,
    meaning the type boundary was tested in isolation but not
    actually enforced along the real execution path.
    """
    if extraction.proposed_txn_id is None:
        return ToolLookupResult(found=False, candidates=[], source="llm_proposed_lookup")

    candidates = index.bank_by_txn.get(extraction.proposed_txn_id, [])
    return ToolLookupResult(
        found=bool(candidates),
        candidates=candidates,
        source="llm_proposed_lookup",
    )