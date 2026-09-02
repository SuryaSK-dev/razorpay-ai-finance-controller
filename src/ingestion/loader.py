# src/ingestion/loader.py
"""
Loads raw JSON source files and validates each record against its
Pydantic schema. This is the firewall between messy input and the
rest of the system -- nothing downstream of this module should ever
need to handle a malformed record; it either passes here as a typed,
validated object, or it is caught here and reported as a structured
IngestionError with enough evidence to classify it later.

Design boundary: this module answers exactly one question --
"is this record valid enough to enter the pipeline?" -- and nothing
more. No normalization, no matching, no tax logic belongs here.
"""

from __future__ import annotations
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, TypeVar, Type

from pydantic import BaseModel, ValidationError

from src.models import PGSettlementRecord, BankStatementRecord, InvoiceRecord
from src.models import UNSUPPORTED_TRANSACTION_TYPE

T = TypeVar("T", bound=BaseModel)


@dataclass
class IngestionError:
    """A record that failed validation. Carries enough evidence to
    later classify it as a CORRUPTED_RECORD exception -- the raw
    payload, the batch index, and the exact validation failure, not
    just 'it broke'.

    error_code is a stable, aggregatable identifier -- distinct from
    error_message, which is Pydantic's free-text explanation and
    varies by field and reason. A dashboard or log aggregator groups
    by error_code ("12 SCHEMA_VALIDATION_FAILED this run"), not by
    parsing prose strings."""
    source: str            # "pg" | "bank" | "invoice"
    index: int              # position within the source file, for fast lookup
    raw_record: dict[str, Any]
    error_message: str
    error_code: str = "SCHEMA_VALIDATION_FAILED"


@dataclass
class IngestionResult:
    """Result of loading one source file: the records that validated
    successfully, plus every record that didn't, with evidence."""
    valid_records: list = field(default_factory=list)
    errors: list[IngestionError] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.valid_records) + len(self.errors)


def _load_raw_json(path: Path) -> list[dict]:
    """Read a JSON file. A structurally broken FILE (not just a
    structurally broken RECORD) is a hard failure -- there's no
    reasonable way to partially recover from unparseable JSON at the
    file level, so this raises rather than silently returning []."""
    if not path.exists():
        raise FileNotFoundError(f"Source file not found: {path.absolute()}")
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Malformed JSON in {path}: {e}")
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array in {path}, got {type(data).__name__}")
    return data


def load_json_records(path: Path, model_cls: Type[T], source_name: str) -> IngestionResult:
    """
    Generic loader: reads a JSON array file and validates each element
    against model_cls. One reusable function instead of three near-
    identical loops -- load_pg_settlements/load_bank_statements/
    load_invoices below are now thin, readable wrappers over this.

    Only ValidationError is caught per-record. A different exception
    (e.g. a real programming bug) is allowed to propagate rather than
    being silently swallowed and misreported as a data-quality issue --
    catching bare Exception here would mask bugs in our own code as if
    they were bad input data, which is the wrong failure mode for a
    system whose whole premise is "verify correctness."
    """
    result = IngestionResult()
    raw_records = _load_raw_json(path)

    for idx, raw in enumerate(raw_records):
        try:
            validated = model_cls(**raw)
            result.valid_records.append(validated)
        except ValidationError as exc:
            message = str(exc)

            # A malformed record and an unsupported transaction type are
            # both rejections, and they are not the same finding. One is
            # "this row is broken"; the other is "this row is fine and
            # describes something we have chosen not to model". The
            # error_code docstring above calls itself a stable,
            # aggregatable identifier -- so a refund aggregates as a
            # refund rather than hiding inside the schema-failure count.
            code = (
                UNSUPPORTED_TRANSACTION_TYPE
                if UNSUPPORTED_TRANSACTION_TYPE in message
                else "SCHEMA_VALIDATION_FAILED"
            )

            result.errors.append(IngestionError(
                source=source_name,
                index=idx,
                raw_record=raw,
                error_message=message,
                error_code=code,
            ))

    return result


def load_pg_settlements(path: Path) -> IngestionResult:
    return load_json_records(path, PGSettlementRecord, source_name="pg")


def load_bank_statements(path: Path) -> IngestionResult:
    return load_json_records(path, BankStatementRecord, source_name="bank")


def load_invoices(path: Path) -> IngestionResult:
    return load_json_records(path, InvoiceRecord, source_name="invoice")


@dataclass
class LoadedBatch:
    """The full loaded batch across all three sources, plus every
    ingestion-level error collected along the way. This is what
    normalization/engine.py consumes next -- one typed contract
    instead of a nested dict, so downstream code gets autocomplete
    and doesn't have to remember string keys."""
    pg: IngestionResult
    bank: IngestionResult
    invoice: IngestionResult

    @property
    def total_errors(self) -> int:
        return len(self.pg.errors) + len(self.bank.errors) + len(self.invoice.errors)

    def summary(self) -> str:
        lines = [
            f"PG settlements : {len(self.pg.valid_records)} valid, {len(self.pg.errors)} rejected",
            f"Bank statements: {len(self.bank.valid_records)} valid, {len(self.bank.errors)} rejected",
            f"Invoices       : {len(self.invoice.valid_records)} valid, {len(self.invoice.errors)} rejected",
        ]
        return "\n".join(lines)


def load_batch(raw_dir: Path) -> LoadedBatch:
    """
    Single entry point for Phase 2: load all three sources from a
    directory. This is what demo.py / the main pipeline calls.

    Filenames match what scripts/generate_data.py actually writes:
    pg_settlement.json, bank_statement.json, merchant_invoice.json.
    """
    pg = load_pg_settlements(raw_dir / "pg_settlement.json")
    bank = load_bank_statements(raw_dir / "bank_statement.json")
    invoice = load_invoices(raw_dir / "merchant_invoice.json")
    return LoadedBatch(pg=pg, bank=bank, invoice=invoice)