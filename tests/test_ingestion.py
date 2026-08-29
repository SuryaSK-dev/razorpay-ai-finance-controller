"""
Phase 2 exit-criteria checks:
  - All valid records load without error
  - A record with a malformed monetary field is rejected, not crashed on
  - Normalization produces a consistent, UTC-dated, common-shaped record
  - Bank txn_id resolution report reflects real linkage outcomes
"""
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.ingestion.loader import load_batch
from src.normalization.engine import normalize_batch

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def test_load_batch_runs_without_crashing():
    batch = load_batch(RAW_DIR)
    assert batch.pg.total > 0
    assert batch.bank.total > 0
    assert batch.invoice.total > 0


def test_corrupted_records_are_caught_not_crashed():
    batch = load_batch(RAW_DIR)
    assert len(batch.pg.errors) >= 1
    corrupted = batch.pg.errors[0]
    assert corrupted.source == "pg"
    assert "gross_amount" in corrupted.error_message or "NOT_A_NUMBER" in str(corrupted.raw_record)


def test_normalization_produces_utc_dates():
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)          # -> NormalizedBatch
    assert len(normalized.records) > 0
    for record in normalized.records:
        assert record.date_utc.tzinfo is not None


def test_all_valid_pg_records_normalize():
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    pg_normalized = [r for r in normalized.records if r.source == "pg"]
    assert len(pg_normalized) == len(batch.pg.valid_records)


def test_bank_txn_id_resolution_report():
    """The report's three buckets (ref-resolved, narration-resolved,
    unresolved) must sum to the total bank record count -- confirms
    resolve_bank_txn_id's fallback chain is being exercised
    consistently between normalize_batch's report and the actual
    per-record txn_id values it assigns."""
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    report = normalized.report

    total_accounted = (report.bank_resolved_via_ref
                        + report.bank_resolved_via_narration
                        + report.bank_unresolved)
    assert total_accounted == report.bank_count

    bank_records = [r for r in normalized.records if r.source == "bank"]
    resolved_count = sum(1 for r in bank_records if r.txn_id is not None)
    unresolved_count = sum(1 for r in bank_records if r.txn_id is None)

    assert resolved_count == report.bank_resolved_via_ref + report.bank_resolved_via_narration
    assert unresolved_count == report.bank_unresolved


if __name__ == "__main__":
    test_load_batch_runs_without_crashing()
    test_corrupted_records_are_caught_not_crashed()
    test_normalization_produces_utc_dates()
    test_all_valid_pg_records_normalize()
    test_bank_txn_id_resolution_report()
    print("All Phase 2 ingestion/normalization checks passed.")