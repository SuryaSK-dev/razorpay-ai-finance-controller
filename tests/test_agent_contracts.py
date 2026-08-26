# tests/test_agent_contracts.py
"""
Proves the contract boundary itself: malformed proposals cannot be
constructed at all, and the contract types have no vocabulary to
express a financial decision.
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

import pytest
from src.agent.contracts import NarrationExtraction, Explanation


def test_valid_txn_id_constructs_successfully():
    obj = NarrationExtraction(proposed_txn_id="TXN_00042")
    assert obj.proposed_txn_id == "TXN_00042"


def test_none_proposal_is_valid():
    obj = NarrationExtraction(proposed_txn_id=None)
    assert obj.proposed_txn_id is None


def test_malformed_txn_id_cannot_be_constructed():
    with pytest.raises(ValueError):
        NarrationExtraction(proposed_txn_id="not-a-real-id")


def test_nonexistent_looking_but_schema_valid_id_still_constructs():
    """A schema-VALID but non-existent txn_id (e.g. TXN_99999999) is
    allowed to construct -- schema validity is not the same as
    existing in real data. Existence is checked separately by
    candidate_lookup.py against the real index, not by the contract."""
    obj = NarrationExtraction(proposed_txn_id="TXN_99999999")
    assert obj.proposed_txn_id == "TXN_99999999"


def test_explanation_too_short_rejected():
    with pytest.raises(ValueError):
        Explanation(text="too short")


def test_explanation_reasonable_length_accepted():
    obj = Explanation(text="This is a reasonable length explanation of a decision.")
    assert len(obj.text) > 20


def test_contracts_have_no_financial_authority_fields():
    """Structural proof: neither contract type has ANY field name
    resembling a financial decision, status, or monetary value.
    This test will fail loudly if anyone ever adds such a field --
    which is exactly the point."""
    forbidden_substrings = ["status", "decision", "amount", "gst", "tds", "matched", "exception_code"]

    narration_fields = NarrationExtraction.__dataclass_fields__.keys()
    explanation_fields = Explanation.__dataclass_fields__.keys()

    for field_name in list(narration_fields) + list(explanation_fields):
        for forbidden in forbidden_substrings:
            assert forbidden not in field_name.lower(), (
                f"Field '{field_name}' contains forbidden substring "
                f"'{forbidden}' -- agent contracts must never carry "
                f"financial authority fields"
            )


def test_frozen_contracts_cannot_be_mutated():
    obj = NarrationExtraction(proposed_txn_id="TXN_00042")
    with pytest.raises(Exception):  # FrozenInstanceError
        obj.proposed_txn_id = "TXN_00099"

def test_confidence_hint_is_honestly_unspecified():
    """Regression guard against re-introducing fabricated confidence
    scoring -- the field must stay 'unspecified' until real
    model-derived confidence is actually implemented and validated."""
    obj = NarrationExtraction(proposed_txn_id="TXN_00042")
    assert obj.confidence_hint == "unspecified"


if __name__ == "__main__":
    test_valid_txn_id_constructs_successfully()
    test_none_proposal_is_valid()
    test_malformed_txn_id_cannot_be_constructed()
    test_nonexistent_looking_but_schema_valid_id_still_constructs()
    test_explanation_too_short_rejected()
    test_explanation_reasonable_length_accepted()
    test_contracts_have_no_financial_authority_fields()
    test_frozen_contracts_cannot_be_mutated()
    test_confidence_hint_is_honestly_unspecified()
    print("All Phase 5 contract tests passed -- AI outputs structurally cannot carry financial authority.")