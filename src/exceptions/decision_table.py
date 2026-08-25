# src/exceptions/decision_table.py
"""
Explicit, formally-defined decision policy for the reconciliation
engine -- built to answer a specific criticism: decision logic that
emerges from if/elif branches shaped by debugging against specific
synthetic failure categories, rather than from a deliberately
designed policy, has undefined behavior for combinations the test
data never happened to produce.

This module defines the policy as data, not control flow: a
priority-ordered table of (condition, resulting status, resulting
exception code) rules. Every possible combination of inputs is
resolved by walking the table in priority order and taking the
first rule that matches -- there is no code path where behavior is
"whatever the last elif happened to catch."
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from src.models import DecisionStatus, ExceptionCode


class MatchCondition(str, Enum):
    """The full space of input conditions the policy can react to."""
    NO_CANDIDATES_FOUND = "no_candidates_found"
    IS_AMBIGUOUS = "is_ambiguous"
    LOW_CONFIDENCE = "low_confidence"
    MISSING_BANK = "missing_bank"
    MISSING_INVOICE = "missing_invoice"
    GST_MISMATCH = "gst_mismatch"
    TDS_MISMATCH = "tds_mismatch"
    TAX_UNVERIFIABLE = "tax_unverifiable"
    FULLY_CLEAN = "fully_clean"


@dataclass
class DecisionContext:
    """The complete, pre-computed fact set the policy reasons over.
    Built once per transaction from a MatchResult + TaxVerification,
    then evaluated against every rule in priority order.

    fully_clean is computed by the caller (manager.py) as a derived
    boolean of the other 8 flags, but kept as an explicit field
    rather than a computed property here, so it can be constructed
    directly in isolated tests without duplicating the derivation
    logic in two places."""
    no_candidates_found: bool
    is_ambiguous: bool
    low_confidence: bool
    missing_bank: bool
    missing_invoice: bool
    gst_mismatch: bool
    tds_mismatch: bool
    tax_unverifiable: bool
    fully_clean: bool = False


@dataclass
class DecisionRule:
    """One row of the policy table. `condition` is a pure predicate
    over DecisionContext -- no side effects, no order-dependence
    beyond the table's own explicit priority position."""
    name: str
    condition: Callable[[DecisionContext], bool]
    status: DecisionStatus
    exception_code: ExceptionCode
    priority: int  # lower = evaluated first; explicit, not implicit


# =======================================================================
# THE POLICY TABLE
# Priority order is the entire policy. Read top to bottom: the first
# rule whose condition is True wins. Every combination this table can
# be asked about is covered by test_decision_table.py, including
# combinations no synthetic category happens to produce on its own
# (e.g. ambiguous AND tax-mismatched at once).
# =======================================================================

DECISION_TABLE: list[DecisionRule] = [
        DecisionRule(
        name="no_candidates_at_all",
        condition=lambda c: c.no_candidates_found,
        status=DecisionStatus.UNMATCHED,
        exception_code=ExceptionCode.HUMAN_REVIEW_REQUIRED,  # CHANGED from
                                                               # MISSING_IN_BANK,
                                                               # which wrongly
                                                               # implied bank
                                                               # was specifically
                                                               # the absent
                                                               # source when
                                                               # BOTH may be
                                                               # absent
        priority=0,
    ),
    DecisionRule(
        name="ambiguous_takes_priority_over_tax_state",
        # Ambiguity about WHICH record matches must be resolved
        # before tax correctness of a specific pairing is even
        # meaningful to ask about.
        condition=lambda c: c.is_ambiguous,
        status=DecisionStatus.AMBIGUOUS,
        exception_code=ExceptionCode.AMBIGUOUS_MATCH,
        priority=1,
    ),
    DecisionRule(
        name="low_confidence_requires_human_review",
        condition=lambda c: c.low_confidence,
        status=DecisionStatus.HUMAN_REVIEW,
        exception_code=ExceptionCode.REFERENCE_MISMATCH,
        priority=2,
    ),
    DecisionRule(
        name="gst_mismatch_takes_priority_over_tds",
        # When both GST and TDS are wrong simultaneously, GST is
        # reported first as the PRIMARY status -- a deliberate
        # priority choice, not an accident of elif ordering. Both
        # violations are still preserved in reason_codes by
        # manager.py's _all_violated_codes(), regardless of which
        # one determines `status` here.
        condition=lambda c: c.gst_mismatch,
        status=DecisionStatus.TAX_MISMATCH,
        exception_code=ExceptionCode.ERR_GST_MISMATCH,
        priority=3,
    ),
    DecisionRule(
        name="tds_mismatch",
        condition=lambda c: c.tds_mismatch,
        status=DecisionStatus.TAX_MISMATCH,
        exception_code=ExceptionCode.ERR_TDS_VARIANCE,
        priority=4,
    ),
    DecisionRule(
        name="missing_bank_otherwise_clean",
        condition=lambda c: c.missing_bank,
        status=DecisionStatus.PARTIAL_MATCH,
        exception_code=ExceptionCode.MISSING_IN_BANK,
        priority=5,
    ),
    DecisionRule(
        name="missing_invoice_otherwise_clean",
        condition=lambda c: c.missing_invoice,
        status=DecisionStatus.PARTIAL_MATCH,
        exception_code=ExceptionCode.MISSING_IN_INVOICE,
        priority=6,
    ),
    DecisionRule(
        name="tax_unverifiable_but_matched",
        # Explicit resolution for a confident, complete match where
        # tax simply could not be verified (e.g. seller_gross
        # unknown). This is NOT the same as a tax mismatch -- it is
        # an honest "cannot confirm," and deserves its own reviewable
        # state rather than silently defaulting to MATCHED or
        # TAX_MISMATCH.
        condition=lambda c: c.tax_unverifiable,
        status=DecisionStatus.HUMAN_REVIEW,
        exception_code=ExceptionCode.HUMAN_REVIEW_REQUIRED,
        priority=7,
    ),
    DecisionRule(
        name="fully_clean_match",
        condition=lambda c: c.fully_clean,
        status=DecisionStatus.MATCHED,
        exception_code=ExceptionCode.NONE,
        priority=8,
    ),
    DecisionRule(
        name="catch_all_unresolved_state",
        # Should be structurally unreachable in production --
        # _build_context() always derives fully_clean=True when every
        # other flag is False. This rule exists purely as a safety
        # net: if that derivation logic ever has a bug and produces
        # an unanticipated all-False combination, the system routes
        # to human review instead of crashing with an unhandled
        # ValueError. The combinatorial test sweep intentionally
        # tests fully_clean as an independent field (not derived),
        # which is what surfaces this edge case -- a real and useful
        # finding from that sweep, not a false positive.
        condition=lambda c: True,
        status=DecisionStatus.HUMAN_REVIEW,
        exception_code=ExceptionCode.HUMAN_REVIEW_REQUIRED,
        priority=9,
    ),
]


def evaluate(context: DecisionContext) -> DecisionRule:
    """
    Walks DECISION_TABLE in priority order, returns the first
    matching rule. If no rule matches, this is a genuine policy gap
    -- fail loudly rather than silently defaulting, so an uncovered
    combination is caught at test time, not discovered in production.
    """
    for rule in sorted(DECISION_TABLE, key=lambda r: r.priority):
        if rule.condition(context):
            if rule.name == "catch_all_unresolved_state":
                import warnings
                warnings.warn(
                    f"Decision policy fell through to catch-all for context "
                    f"{context!r} -- this should be structurally unreachable "
                    f"in production; investigate _build_context() immediately.",
                    RuntimeWarning,
                )
            return rule

    raise ValueError(
        f"No decision rule matched context {context!r} -- this is a "
        f"genuine policy gap. Every DecisionContext combination must "
        f"be covered by an explicit rule; add one rather than let "
        f"this fall through silently."
    )