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
from pydantic import BaseModel, Field, BeforeValidator

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
    gross_amount: Money
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
    utr: Optional[str] = None          # intentionally optional — some
                                        # synthetic records omit it
    timestamp: datetime


class BankStatementRecord(BaseModel):
    bank_ref: str
    utr: Optional[str] = None
    credited_amount: Money
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
    invoice_amount: Money
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