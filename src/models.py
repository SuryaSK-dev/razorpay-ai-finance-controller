# src/models.py
"""
Canonical data contracts for the reconciliation engine.

This file defines WHAT data exists and WHAT VALID DATA LOOKS LIKE.
It does not contain matching logic, tax logic, exception logic, or
audit logic — those live in their own modules and import from here.

Every downstream module (ingestion, normalization, matching, tax,
exceptions, audit, evaluation) depends on this file. This file
depends on nothing except config.py and Pydantic itself.
"""

from __future__ import annotations
from decimal import Decimal, InvalidOperation
from datetime import datetime, date
from enum import Enum
from typing import Optional, Annotated, Any, Literal
from pydantic import BaseModel, Field, BeforeValidator, AfterValidator

# =======================================================================
# SHARED MONEY VALIDATOR
# One rule, reused everywhere via Annotated — no duplicated validator
# code across every model. A float or bool reaching this point is
# rejected immediately; by the time a bad value reaches business
# logic, the precision damage is already unrecoverable, so we stop
# it here, at the boundary.
# =======================================================================

def to_decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        # bool is a subclass of int in Python -- without this explicit
        # check, a stray True/False could slip past the float check
        # and produce a confusing Decimal("True") error deep in the
        # pipeline instead of a clear one right here.
        raise ValueError(
            f"Boolean value {value!r} rejected for a monetary field."
        )
    if isinstance(value, float):
        raise ValueError(
            f"Float value {value!r} rejected. Monetary fields must be "
            f"passed as str or Decimal — floats introduce binary "
            f"floating-point error (e.g. 0.1 + 0.2 != 0.3) that is "
            f"unacceptable in financial computation."
        )
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"Cannot convert {value!r} to Decimal: {e}")

Money = Annotated[Decimal, BeforeValidator(to_decimal)]


# =======================================================================
# UNSUPPORTED TRANSACTION TYPES
# A negative transaction value means a refund, a chargeback, a reversal
# or an adjustment. This system does not model any of them, and the
# honest response to data it cannot model is to refuse it -- not to
# process it as though it were a forward settlement.
#
# WHY THIS GUARD EXISTS (FAILURE_LOG.md section 65)
# -------------------------------------------------
# Before it, a refund row in the bank feed was absorbed in total
# silence. It ingested cleanly, was discarded by the tier-3 amount gate
# as a non-candidate, and then appeared in NO decision, NO exception and
# NO error count -- an injected refund left all of 61 decisions, 37
# exceptions and 2 ingestion errors completely unchanged.
#
# The mechanism is worth understanding, because the guard is not a bug
# fix. The amount gate that makes fuzzy matching safe is EXACTLY what
# made the refund invisible: a -1204.78 credit cannot match a +1204.78
# expected net, so it was correctly rejected as a candidate and then
# silently forgotten. The system was fail-closed against a wrong MATCH
# and silent about an unmodelled TRANSACTION TYPE. Those are different
# properties and only one of them was guarded.
#
# A rejected record is reported, counted in total_errors, and printed by
# run_pipeline.py -- the same treatment the two corrupted records get.
# Silence is the thing being removed here, not the refund.
UNSUPPORTED_TRANSACTION_TYPE = "UNSUPPORTED_TRANSACTION_TYPE"


def reject_negative(value: Decimal) -> Decimal:
    """
    Refuse a negative transaction value.

    Applied to the three fields that carry a TRANSACTION VALUE, never to
    a component or a balance:

        PGSettlementRecord.gross_amount
        BankStatementRecord.credited_amount
        InvoiceRecord.invoice_amount

    Deliberately NOT applied to `pg_fee`, `gst_on_fee`, `tds_withheld`,
    `net_payout`, `bank_charges`, `claimed_gst` or `claimed_tds`. A
    negative fee component or a credit-note tax line is a different
    question with a different answer, and guarding them here would
    conflate "we do not model refunds" with "this number looks odd".
    `net_payout` in particular can legitimately go negative when fees
    exceed a small gross, which is an anomaly rather than an
    unsupported type.
    """
    if value < 0:
        raise ValueError(
            f"{UNSUPPORTED_TRANSACTION_TYPE}: negative transaction value "
            f"{value}. Refunds, chargebacks, reversals and adjustments "
            f"are not modelled by this system. The record is rejected "
            f"and reported rather than processed as a forward "
            f"settlement -- see FAILURE_LOG.md section 65."
        )
    return value


# A transaction value: the amount that moved. Never negative here,
# because a negative one means a transaction type this system has
# decided not to model rather than to model badly.
SettlementValue = Annotated[
    Decimal, BeforeValidator(to_decimal), AfterValidator(reject_negative)
]

# =======================================================================
# ENUMS
# Typed over raw strings: autocomplete-friendly, typo-proof, and a
# reviewer can see the entire universe of valid states at a glance
# instead of hunting through business logic for string literals.
# =======================================================================

class DecisionStatus(str, Enum):
    MATCHED = "MATCHED"
    PARTIAL_MATCH = "PARTIAL_MATCH"
    TAX_MISMATCH = "TAX_MISMATCH"
    AMBIGUOUS = "AMBIGUOUS"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    UNMATCHED = "UNMATCHED"

class ExceptionCode(str, Enum):
    NONE = "NONE"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    MISSING_IN_BANK = "MISSING_IN_BANK"
    MISSING_IN_INVOICE = "MISSING_IN_INVOICE"
    TIMING_GAP = "TIMING_GAP"
    DUPLICATE_DETECTED = "DUPLICATE_DETECTED"
    REFERENCE_MISMATCH = "REFERENCE_MISMATCH"
    ERR_GST_MISMATCH = "ERR_GST_MISMATCH"
    ERR_TDS_VARIANCE = "ERR_TDS_VARIANCE"
    ERR_TDS_BELOW_THRESHOLD = "ERR_TDS_BELOW_THRESHOLD"  # TDS wrongly
                                                            # applied when
                                                            # seller is
                                                            # under the
                                                            # ₹5L annual
                                                            # threshold
    ERR_MISSING_IRN = "ERR_MISSING_IRN"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    CORRUPTED_RECORD = "CORRUPTED_RECORD"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


# =======================================================================
# SOURCE MODELS
# Each source keeps its own native vocabulary (settlement_id vs
# bank_ref vs invoice_id) rather than being prematurely normalized —
# that's what normalization/engine.py exists for. Preserving source
# fidelity here means nothing is lost before it reaches the audit
# trail's raw_ref pointer.
# =======================================================================

class PGSettlementRecord(BaseModel):
    settlement_id: str
    txn_id: str
    merchant_id: str
    gross_amount: SettlementValue
    pg_fee: Money
    gst_on_fee: Money
    tds_withheld: Money = Field(default=Decimal("0"))
    net_payout: Money
    merchant_gstin: Optional[str] = None
    merchant_ytd_gross_opening: Money = Field(default=Decimal("0"))
    # Each merchant's cumulative gross transaction value PRIOR to this
    # batch -- i.e. their running total from real prior-period
    # settlement history. This is what makes the TDS Section 393
    # threshold (INR 5,00,000 annual) evaluable at all: the threshold
    # is a property of a seller's YEAR, not any single transaction or
    # any single batch, and no downstream consumer can correctly
    # reconstruct "has this merchant crossed the threshold" without
    # knowing their starting point. A real production system would
    # source this from an actual merchant ledger; here it is generated
    # explicitly so the synthetic data is self-consistent and every
    # TDS decision is independently verifiable, not just internally
    # consistent with the generator's own private state.
    payment_method: Optional[str] = None
    # How the customer paid: UPI / CARD / NETBANKING. This is the reason
    # pg_fee is what it is -- MDR is method-dependent (config.MDR_BY_METHOD),
    # not a flat percentage.
    #
    # It was previously written into the raw JSON and then SILENTLY
    # DROPPED here, because a field absent from this model never reaches
    # NormalizedRecord.raw_ref. The audit trail therefore recorded a fee
    # with no way to explain it. Optional so a feed without the field
    # still ingests rather than failing validation.
    utr: Optional[str] = None          # intentionally optional — some
                                        # synthetic records omit it
    timestamp: datetime


class BankStatementRecord(BaseModel):
    bank_ref: str
    utr: Optional[str] = None
    credited_amount: SettlementValue
    value_date: date
    narration: Optional[str] = None    # messy free text — candidate for
                                        # LLM extraction in the AI sidecar
    bank_charges: Money = Field(default=Decimal("0"))


class InvoiceRecord(BaseModel):
    invoice_id: str
    txn_id: str
    irn: Optional[str] = None          # e-invoice reference — can be
                                        # missing, tested explicitly
    gstin: Optional[str] = None
    invoice_amount: SettlementValue
    claimed_gst: Money
    claimed_tds: Money
    period: str                        # e.g. "2026-08", for GSTR-2B
                                        # period-matching


# =======================================================================
# INTERNAL: NORMALIZED RECORD
# The universal internal language. Every module downstream of
# normalization/engine.py works ONLY with this shape — matching,
# tax validation, and decisioning never see a source-specific field
# name again after this point.
# =======================================================================

class NormalizedRecord(BaseModel):
    txn_id: Optional[str] = None  # None means "not yet resolved" --
                                   # bank records may reach this stage
                                   # without a linked txn_id; matching
                                   # resolves it downstream via
                                   # UTR/amount/date signals. Never use
                                   # a sentinel string here -- it would
                                   # look like a real identifier and
                                   # could silently pass an equality
                                   # check against it.
    source: Literal["pg", "bank", "invoice"]
    utr: Optional[str] = None
    amount: Money
    fee: Optional[Money] = None
    date_utc: datetime
    gst: Optional[Money] = None
    tds: Optional[Money] = None
    raw_ref: dict[str, Any] = Field(default_factory=dict)  # audit
                                                              # back-pointer
                                                              # to the
                                                              # original
                                                              # source record.
                                                              # This is also
                                                              # how
                                                              # seller_ledger.py
                                                              # reads
                                                              # merchant_ytd_gross_opening
                                                              # -- via
                                                              # raw_ref, since
                                                              # that field
                                                              # is PG-source
                                                              # -specific and
                                                              # not part of the
                                                              # universal
                                                              # canonical schema.

# =======================================================================
# DECISION CONTRACT
# The single point-of-record for a transaction's final outcome.
# MatchScore, TaxVerification, ExceptionRecord, and AuditEntry are
# deliberately NOT defined here — they belong to matching/, tax/,
# exceptions/, and audit/ respectively. Keeping them out of models.py
# preserves a clean boundary: this file is the data contract, not the
# business logic.
# =======================================================================

class MatchDecision(BaseModel):
    txn_id: str
    status: DecisionStatus
    confidence_score: int  # 0-100
    matched_sources: list[Literal["pg", "bank", "invoice"]] = Field(default_factory=list)
    tax_verified: Optional[bool] = None
    exception_code: ExceptionCode = ExceptionCode.NONE
    reason_codes: list[ExceptionCode] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


# =======================================================================
# GROUND TRUTH (evaluation-only — never read by the pipeline itself)
# =======================================================================

class GroundTruthRecord(BaseModel):
    txn_id: str
    expected_status: DecisionStatus
    expected_exception_code: ExceptionCode = ExceptionCode.NONE
    category: str  # generation category, e.g. "exact_match",
                    # "tax_mismatch" — used for per-category eval breakdown