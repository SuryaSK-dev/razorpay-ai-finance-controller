# tests/test_ingestion.py
"""
Phase 2 unit tests: ingestion + normalization.
Run: python -m pytest tests/test_ingestion.py -v
"""

import json
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.ingestion.loader import load_json_records, load_batch, LoadedBatch
from src.models import PGSettlementRecord, BankStatementRecord, InvoiceRecord
from src.normalization.engine import (
    normalize_batch,
    _extract_txn_from_narration,
    _to_utc,
)


# =======================================================================
# LOADER -- isolated fixture-based tests, not dependent on generate_data.py
# =======================================================================

def test_load_valid_pg_record():
    raw = [{
        "settlement_id": "SET-001",
        "txn_id": "TXN-001",
        "order_id": "ORD-001",
        "merchant_id": "MERC-001",
        "gross_amount": "1000.00",
        "pg_fee": "20.00",
        "gst_on_fee": "3.60",
        "tds_withheld": "0.00",
        "net_payout": "976.40",
        "merchant_gstin": "29AAAAA0001A1Z5",
        "payment_method": "UPI",
        "utr": "UTR1234567890",
        "timestamp": "2026-08-15T00:00:00",
    }]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(raw, f)
        f.flush()
        result = load_json_records(Path(f.name), PGSettlementRecord, source_name="pg")
        assert len(result.valid_records) == 1
        assert len(result.errors) == 0
        assert result.valid_records[0].gross_amount == Decimal("1000.00")
    Path(f.name).unlink()


def test_rejects_corrupted_record_without_crashing():
    """A record with a non-numeric gross_amount must be rejected
    gracefully -- exactly our 'corrupted' synthetic category."""
    raw = [{
        "settlement_id": "SET-CORR",
        "txn_id": "TXN-CORR",
        "order_id": "ORD-CORR",
        "merchant_id": "MERC-001",
        "gross_amount": "NOT_A_NUMBER",
        "pg_fee": "20.00",
        "gst_on_fee": "3.60",
        "tds_withheld": "0.00",
        "net_payout": "976.40",
        "merchant_gstin": "29AAAAA0001A1Z5",
        "payment_method": "UPI",
        "utr": "UTRCORRUPTED",
        "timestamp": "2026-08-15T00:00:00",
    }]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(raw, f)
        f.flush()
        result = load_json_records(Path(f.name), PGSettlementRecord, source_name="pg")
        assert len(result.valid_records) == 0
        assert len(result.errors) == 1
        assert result.errors[0].index == 0
        assert result.errors[0].source == "pg"
    Path(f.name).unlink()


def test_load_batch_integration():
    """Full 3-source load, using tempfile fixtures -- isolated from
    whatever currently sits in data/raw/."""
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_dir = Path(tmpdir)

        pg = [{
            "settlement_id": "SET-001", "txn_id": "TXN-001", "order_id": "ORD-001",
            "merchant_id": "MERC-001", "gross_amount": "1000.00", "pg_fee": "20.00",
            "gst_on_fee": "3.60", "tds_withheld": "0.00", "net_payout": "976.40",
            "merchant_gstin": "29AAAAA0001A1Z5", "payment_method": "UPI",
            "utr": "UTR1", "timestamp": "2026-08-15T00:00:00",
        }]
        bank = [{
            "bank_ref": "BANKREF_TXN-001", "utr": "UTR1", "credited_amount": "976.40",
            "value_date": "2026-08-15", "narration": "NEFT CR UTR1 MERC-001",
            "bank_charges": "0",
        }]
        invoice = [{
            "invoice_id": "INV-001", "txn_id": "TXN-001", "irn": "a" * 64,
            "gstin": "29AAAAA0001A1Z5", "invoice_amount": "23.60",
            "claimed_gst": "3.60", "claimed_tds": "0.00", "period": "2026-08",
        }]

        (raw_dir / "pg_settlement.json").write_text(json.dumps(pg), encoding="utf-8")
        (raw_dir / "bank_statement.json").write_text(json.dumps(bank), encoding="utf-8")
        (raw_dir / "merchant_invoice.json").write_text(json.dumps(invoice), encoding="utf-8")

        batch = load_batch(raw_dir)
        assert isinstance(batch, LoadedBatch)
        assert len(batch.pg.valid_records) == 1
        assert len(batch.bank.valid_records) == 1
        assert len(batch.invoice.valid_records) == 1


# =======================================================================
# NORMALIZATION -- isolated, against real field names and real
# fallback-chain behavior
# =======================================================================

def test_bank_txn_id_resolved_via_narration_when_ref_absent():
    """If bank_ref doesn't follow the BANKREF_<txn_id> convention,
    resolution must fall back to narration regex."""
    assert _extract_txn_from_narration("REF TXN_88441 DONE") == "TXN_88441"
    assert _extract_txn_from_narration("NO ID HERE") is None


def test_utc_normalization_naive_datetime():
    naive = datetime(2026, 8, 15, 12, 0, 0)
    utc = _to_utc(naive)
    assert utc.tzinfo == timezone.utc


def test_normalize_batch_produces_report_and_records():
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_dir = Path(tmpdir)
        pg = [{
            "settlement_id": "SET-001", "txn_id": "TXN-001", "order_id": "ORD-001",
            "merchant_id": "MERC-001", "gross_amount": "1000.00", "pg_fee": "20.00",
            "gst_on_fee": "3.60", "tds_withheld": "0.00", "net_payout": "976.40",
            "merchant_gstin": "29AAAAA0001A1Z5", "payment_method": "UPI",
            "utr": "UTR1", "timestamp": "2026-08-15T00:00:00",
        }]
        bank = [{
            "bank_ref": "BANKREF_TXN-001", "utr": "UTR1", "credited_amount": "976.40",
            "value_date": "2026-08-15", "narration": "NEFT CR UTR1 MERC-001",
            "bank_charges": "0",
        }]
        invoice = [{
            "invoice_id": "INV-001", "txn_id": "TXN-001", "irn": "a" * 64,
            "gstin": "29AAAAA0001A1Z5", "invoice_amount": "23.60",
            "claimed_gst": "3.60", "claimed_tds": "0.00", "period": "2026-08",
        }]
        (raw_dir / "pg_settlement.json").write_text(json.dumps(pg), encoding="utf-8")
        (raw_dir / "bank_statement.json").write_text(json.dumps(bank), encoding="utf-8")
        (raw_dir / "merchant_invoice.json").write_text(json.dumps(invoice), encoding="utf-8")

        from src.ingestion.loader import load_batch
        batch = load_batch(raw_dir)
        result = normalize_batch(batch)

        assert len(result.records) == 3
        assert result.report.bank_resolved_via_ref == 1
        assert result.report.bank_unresolved == 0

        bank_normalized = [r for r in result.records if r.source == "bank"][0]
        assert bank_normalized.txn_id == "TXN-001"


if __name__ == "__main__":
    test_load_valid_pg_record()
    test_rejects_corrupted_record_without_crashing()
    test_load_batch_integration()
    test_bank_txn_id_resolved_via_narration_when_ref_absent()
    test_utc_normalization_naive_datetime()
    test_normalize_batch_produces_report_and_records()
    print("All Phase 2 unit tests passed (isolated fixtures, not dependent on data/raw/).")