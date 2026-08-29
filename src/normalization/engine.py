# src/normalization/engine.py
"""
Maps validated source-specific records (PGSettlementRecord,
BankStatementRecord, InvoiceRecord) into the single common shape,
NormalizedRecord, that every downstream module (matching, tax
validation, decisioning) works with exclusively.

Deliberately deterministic: no confidence scoring, no matching, no
tax verification happens here. Those belong to later phases.
"""

from __future__ import annotations
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional

from src.models import (
    PGSettlementRecord,
    BankStatementRecord,
    InvoiceRecord,
    NormalizedRecord,
)


def _to_utc(dt) -> datetime:
    """Every date in the system becomes UTC-aware. A bare `date`
    (from BankStatementRecord.value_date) is anchored at midnight UTC;
    a naive datetime is assumed UTC; an aware datetime with a
    different offset is converted."""
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)


# =======================================================================
# BANK NARRATION EXTRACTION
# Bank records have no txn_id field of their own. Our synthetic data
# encodes it in bank_ref (BANKREF_<txn_id>), but a real bank feed would
# only offer a free-text narration line. This fallback chain -- try
# bank_ref first, then attempt to parse the narration, then give up
# cleanly -- is what a production system would actually need, and
# demonstrates the same pattern that matters for real bank exports.
# =======================================================================

_NARRATION_PATTERNS = [
    r"(TXN_\d{5,8})",
    r"(TXN-\d{4}-\d{4,8})",
    r"(PYT_\d{7,8})",        # was: r"PYT_(\d{7,8})"
]


def _extract_txn_from_bank_ref(bank_ref: str) -> Optional[str]:
    """Our synthetic generator names bank refs as 'BANKREF_<txn_id>'
    (and 'BANKREF_<txn_id>_DUP' for the duplicate category)."""
    if not bank_ref or not bank_ref.startswith("BANKREF_"):
        return None
    remainder = bank_ref[len("BANKREF_"):]
    return remainder.split("_DUP")[0]


def _extract_txn_from_narration(narration: Optional[str]) -> Optional[str]:
    """Fallback: attempt to find a transaction-id-shaped token inside
    free-text bank narration. This is the realistic path for an actual
    bank statement, where no structured bank_ref linkage exists."""
    if not narration:
        return None
    for pattern in _NARRATION_PATTERNS:
        match = re.search(pattern, narration, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None


def resolve_bank_txn_id(bank_ref: str, narration: Optional[str]) -> Optional[str]:
    """
    Priority order, matching how a real ingestion pipeline would
    actually try to link a bank row to a transaction:
      1. Structured bank_ref (fast path, when available)
      2. Narration regex (realistic fallback for genuine bank exports)
      3. None (genuinely unresolved -- left for matching/candidates.py
         to attempt via UTR/amount/date signals instead)
    """
    return (_extract_txn_from_bank_ref(bank_ref)
            or _extract_txn_from_narration(narration))


# =======================================================================
# PER-SOURCE NORMALIZERS
# =======================================================================

def normalize_pg_record(record: PGSettlementRecord) -> NormalizedRecord:
    return NormalizedRecord(
        txn_id=record.txn_id, source="pg", utr=record.utr,
        amount=record.gross_amount, fee=record.pg_fee,   # NEW: fee
        date_utc=_to_utc(record.timestamp),
        gst=record.gst_on_fee, tds=record.tds_withheld,
        raw_ref=record.model_dump(mode="json"),
    )


def normalize_bank_record(record: BankStatementRecord) -> NormalizedRecord:
    """
    txn_id is Optional -- when unresolved, it is left as None, never
    a sentinel string. A string like "UNRESOLVED" is dangerous here:
    it looks like a real identifier and could silently pass an
    equality check downstream. None makes "not yet known" explicit
    and unambiguous to every consumer of this record.
    """
    resolved_txn_id = resolve_bank_txn_id(record.bank_ref, record.narration)
    return NormalizedRecord(
        txn_id=resolved_txn_id,
        source="bank",
        utr=record.utr,
        amount=record.credited_amount,
        date_utc=_to_utc(record.value_date),
        gst=None,
        tds=None,
        raw_ref=record.model_dump(mode="json"),
    )


def normalize_invoice_record(record: InvoiceRecord) -> NormalizedRecord:
    year, month = (int(part) for part in record.period.split("-"))
    invoice_fee = record.invoice_amount - record.claimed_gst  # derived --
                                        # our schema has no standalone
                                        # invoice fee field; invoice_amount
                                        # = fee + gst by construction in
                                        # generate_data.py
    return NormalizedRecord(
        txn_id=record.txn_id,
        source="invoice",
        utr=None,
        amount=record.invoice_amount,
        fee=invoice_fee,                # ADD THIS LINE
        date_utc=datetime(year, month, 1, tzinfo=timezone.utc),
        gst=record.claimed_gst,
        tds=record.claimed_tds,
        raw_ref=record.model_dump(mode="json"),
    )

# =======================================================================
# BATCH NORMALIZATION + REPORT
# =======================================================================

@dataclass
class NormalizationReport:
    """Summary of one normalization pass -- genuinely useful demo
    material, and a real signal of how much bank-side linkage
    succeeded via bank_ref vs. narration vs. neither."""
    pg_count: int
    bank_count: int
    invoice_count: int
    bank_resolved_via_ref: int
    bank_resolved_via_narration: int
    bank_unresolved: int

    def summary(self) -> str:
        lines = [
            "Normalization Summary",
            "-" * 40,
            f"PG records            : {self.pg_count}",
            f"Bank records          : {self.bank_count}",
            f"Invoice records       : {self.invoice_count}",
            "",
            f"Bank resolved (ref)   : {self.bank_resolved_via_ref}",
            f"Bank resolved (regex) : {self.bank_resolved_via_narration}",
            f"Bank unresolved       : {self.bank_unresolved}",
        ]
        return "\n".join(lines)


@dataclass
class NormalizedBatch:
    records: list[NormalizedRecord]
    report: NormalizationReport


def normalize_batch(loaded_batch) -> NormalizedBatch:
    """
    Normalizes every valid record from a LoadedBatch (Phase 2 loader
    output) into one flat list of NormalizedRecord, plus a report
    describing how bank-side linkage resolved. Ingestion errors are
    NOT passed through here -- they remain IngestionError objects for
    the exception manager to convert into CORRUPTED_RECORD exceptions
    later, not silently dropped.
    """
    records: list[NormalizedRecord] = []

    for pg_record in loaded_batch.pg.valid_records:
        records.append(normalize_pg_record(pg_record))

    resolved_via_ref = 0
    resolved_via_narration = 0
    unresolved = 0

    for bank_record in loaded_batch.bank.valid_records:
        via_ref = _extract_txn_from_bank_ref(bank_record.bank_ref)
        if via_ref:
            resolved_via_ref += 1
        elif _extract_txn_from_narration(bank_record.narration):
            resolved_via_narration += 1
        else:
            unresolved += 1
        records.append(normalize_bank_record(bank_record))

    for invoice_record in loaded_batch.invoice.valid_records:
        records.append(normalize_invoice_record(invoice_record))

    report = NormalizationReport(
        pg_count=len(loaded_batch.pg.valid_records),
        bank_count=len(loaded_batch.bank.valid_records),
        invoice_count=len(loaded_batch.invoice.valid_records),
        bank_resolved_via_ref=resolved_via_ref,
        bank_resolved_via_narration=resolved_via_narration,
        bank_unresolved=unresolved,
    )

    return NormalizedBatch(records=records, report=report)