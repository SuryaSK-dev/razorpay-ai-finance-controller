# src/exceptions/decision_table.py
"""
Explicit, formally-defined deterministic decision policy for the
reconciliation engine.

The policy is represented as a priority-ordered table rather than
implicit if/elif control flow. The first matching rule determines the
primary status and exception code.

Important policy principles:

1. No valid candidate pairing means the transaction is UNMATCHED.
2. Duplicate/ambiguous identity takes precedence over downstream
   financial or tax evaluation.
3. A confirmed monetary discrepancy takes precedence over generic
   low-confidence classification.
4. A missing authoritative source is an unresolved reconciliation
   state, not merely a low-confidence reference mismatch.
5. Tax mismatches are evaluated only after identity/reconciliation
   state is sufficiently established.
6. FULLY_CLEAN is the only rule that produces MATCHED.
7. The catch-all exists as a defensive safety boundary and must not
   become normal production behavior.

COVERAGE
--------
DecisionContext has eleven boolean dimensions, so the policy space is
2^11 = 2048 combinations. All 2048 are swept in
tests/test_decision_table.py and every one resolves to exactly one
rule.

An earlier documented figure of "512/512" described a 2^9 sweep that
held duplicate_detected and amount_mismatch at False throughout. Both
are real policy dimensions with their own rules, so that figure
understated the space rather than covering it. The 512 sweep is
retained because FAILURE_LOG.md section 9 refers to it, but 2048 is
the coverage claim.

Financial authority remains deterministic. No AI-generated output
participates in this policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from src.models import DecisionStatus, ExceptionCode


class MatchCondition(str, Enum):
    """Conditions that can influence the deterministic decision policy."""

    NO_CANDIDATES_FOUND = "no_candidates_found"
    IS_AMBIGUOUS = "is_ambiguous"
    LOW_CONFIDENCE = "low_confidence"
    MISSING_BANK = "missing_bank"
    MISSING_INVOICE = "missing_invoice"
    GST_MISMATCH = "gst_mismatch"
    TDS_MISMATCH = "tds_mismatch"
    TAX_UNVERIFIABLE = "tax_unverifiable"
    FULLY_CLEAN = "fully_clean"
    DUPLICATE_DETECTED = "duplicate_detected"
    AMOUNT_MISMATCH = "amount_mismatch"


@dataclass
class DecisionContext:
    """
    Complete deterministic fact set consumed by the decision policy.

    The context is built by manager.py from MatchResult and
    deterministic tax verification.

    `fully_clean` remains explicit so the policy can be exercised
    independently through exhaustive combinatorial tests.
    """

    no_candidates_found: bool
    is_ambiguous: bool
    duplicate_detected: bool
    low_confidence: bool
    missing_bank: bool
    missing_invoice: bool
    amount_mismatch: bool
    gst_mismatch: bool
    tds_mismatch: bool
    tax_unverifiable: bool
    fully_clean: bool = False


@dataclass
class DecisionRule:
    """
    One deterministic row of the policy table.

    Lower priority numbers are evaluated first.
    Priority is explicit policy, not an accidental consequence of
    Python if/elif ordering.
    """

    name: str
    condition: Callable[[DecisionContext], bool]
    status: DecisionStatus
    exception_code: ExceptionCode
    priority: int


# ======================================================================
# DETERMINISTIC POLICY TABLE
# ======================================================================
#
# Priority is explicit and dense:
#
#   0  no candidates
#   1  duplicate
#   2  ambiguous
#   3  amount mismatch
#   4  missing bank
#   5  missing invoice
#   6  low confidence
#   7  GST mismatch
#   8  TDS mismatch
#   9  tax unverifiable
#   10 fully clean
#   11 catch-all
#
# Rationale:
#
#   - No candidates means there is no valid reconciliation pairing.
#
#   - Duplicate/ambiguous identity must be resolved before trusting
#     any particular candidate.
#
#   - AMOUNT_MISMATCH represents a confirmed financial-control
#     discrepancy and therefore outranks generic LOW_CONFIDENCE.
#
#   - Missing authoritative sources are explicit reconciliation
#     failures. They must not be hidden behind LOW_CONFIDENCE.
#
#   - LOW_CONFIDENCE is a fallback classification when no stronger
#     deterministic exception has been established.
#
#   - GST/TDS mismatches are evaluated after identity and core
#     reconciliation state.
#
#   - Tax unverifiable means "cannot confirm", not "confirmed mismatch".
#
#   - FULLY_CLEAN is the only automatic MATCHED outcome.
#
# ======================================================================

DECISION_TABLE: list[DecisionRule] = [
    # ---------------------------------------------------------------
    # 0. No usable candidate pairing
    # ---------------------------------------------------------------
    DecisionRule(
        name="no_candidates_at_all",
        condition=lambda c: c.no_candidates_found,
        status=DecisionStatus.UNMATCHED,
        exception_code=ExceptionCode.HUMAN_REVIEW_REQUIRED,
        priority=0,
    ),

    # ---------------------------------------------------------------
    # 1. Duplicate evidence
    # ---------------------------------------------------------------
    DecisionRule(
        name="duplicate_detected",
        condition=lambda c: c.duplicate_detected,
        status=DecisionStatus.HUMAN_REVIEW,
        exception_code=ExceptionCode.DUPLICATE_DETECTED,
        priority=1,
    ),

    # ---------------------------------------------------------------
    # 2. Ambiguous candidate identity
    # ---------------------------------------------------------------
    DecisionRule(
        name="ambiguous_takes_priority_over_tax_and_confidence_state",
        condition=lambda c: c.is_ambiguous,
        status=DecisionStatus.AMBIGUOUS,
        exception_code=ExceptionCode.AMBIGUOUS_MATCH,
        priority=2,
    ),

    # ---------------------------------------------------------------
    # 3. Confirmed monetary discrepancy
    # ---------------------------------------------------------------
    DecisionRule(
        name="amount_mismatch",
        condition=lambda c: c.amount_mismatch,
        status=DecisionStatus.HUMAN_REVIEW,
        exception_code=ExceptionCode.AMOUNT_MISMATCH,
        priority=3,
    ),

    # ---------------------------------------------------------------
    # 4. Bank source is missing
    #
    # Missing bank is explicitly UNMATCHED according to the current
    # reconciliation contract. It is not reduced to generic
    # LOW_CONFIDENCE.
    # ---------------------------------------------------------------
    DecisionRule(
        name="missing_bank_unmatched",
        condition=lambda c: c.missing_bank,
        status=DecisionStatus.UNMATCHED,
        exception_code=ExceptionCode.MISSING_IN_BANK,
        priority=4,
    ),

    # ---------------------------------------------------------------
    # 5. Invoice source is missing
    # ---------------------------------------------------------------
    DecisionRule(
        name="missing_invoice_otherwise_clean",
        condition=lambda c: c.missing_invoice,
        status=DecisionStatus.PARTIAL_MATCH,
        exception_code=ExceptionCode.MISSING_IN_INVOICE,
        priority=5,
    ),

    # ---------------------------------------------------------------
    # 6. Generic low-confidence fallback
    # ---------------------------------------------------------------
    DecisionRule(
        name="low_confidence_requires_human_review",
        condition=lambda c: c.low_confidence,
        status=DecisionStatus.HUMAN_REVIEW,
        exception_code=ExceptionCode.REFERENCE_MISMATCH,
        priority=6,
    ),

    # ---------------------------------------------------------------
    # 7. GST mismatch
    # ---------------------------------------------------------------
    DecisionRule(
        name="gst_mismatch_takes_priority_over_tds",
        condition=lambda c: c.gst_mismatch,
        status=DecisionStatus.TAX_MISMATCH,
        exception_code=ExceptionCode.ERR_GST_MISMATCH,
        priority=7,
    ),

    # ---------------------------------------------------------------
    # 8. TDS mismatch
    # ---------------------------------------------------------------
    DecisionRule(
        name="tds_mismatch",
        condition=lambda c: c.tds_mismatch,
        status=DecisionStatus.TAX_MISMATCH,
        exception_code=ExceptionCode.ERR_TDS_VARIANCE,
        priority=8,
    ),

    # ---------------------------------------------------------------
    # 9. Tax could not be verified
    # ---------------------------------------------------------------
    DecisionRule(
        name="tax_unverifiable_but_matched",
        condition=lambda c: c.tax_unverifiable,
        status=DecisionStatus.HUMAN_REVIEW,
        exception_code=ExceptionCode.HUMAN_REVIEW_REQUIRED,
        priority=9,
    ),

    # ---------------------------------------------------------------
    # 10. Fully clean reconciliation
    # ---------------------------------------------------------------
    DecisionRule(
        name="fully_clean_match",
        condition=lambda c: c.fully_clean,
        status=DecisionStatus.MATCHED,
        exception_code=ExceptionCode.NONE,
        priority=10,
    ),

    # ---------------------------------------------------------------
    # 11. Defensive safety boundary
    # ---------------------------------------------------------------
    DecisionRule(
        name="catch_all_unresolved_state",
        condition=lambda c: True,
        status=DecisionStatus.HUMAN_REVIEW,
        exception_code=ExceptionCode.HUMAN_REVIEW_REQUIRED,
        priority=11,
    ),
]


def evaluate(context: DecisionContext) -> DecisionRule:
    """
    Evaluate the deterministic policy.

    The first matching rule in explicit priority order wins.

    The catch-all is retained as a defensive safety boundary. It should
    not occur for a valid production context produced by manager.py.

    If it does occur, a RuntimeWarning is emitted so that the policy
    or context-construction defect remains visible instead of being
    silently converted into an ordinary business decision.
    """

    for rule in sorted(DECISION_TABLE, key=lambda r: r.priority):
        if rule.condition(context):
            if rule.name == "catch_all_unresolved_state":
                import warnings

                warnings.warn(
                    "Decision policy fell through to catch-all for context "
                    f"{context!r}. This should be unreachable for a valid "
                    "production context; investigate context construction.",
                    RuntimeWarning,
                )

            return rule

    # Defensive guard. Because the catch-all rule exists, this should
    # be structurally unreachable.
    raise ValueError(
        f"No decision rule matched context {context!r}. "
        "Every DecisionContext combination must be covered by the "
        "explicit deterministic policy."
    )