# tests/test_unsupported_transaction_types.py
"""
A refund must be refused, not absorbed.

WHAT THIS CLOSES
----------------
Before this guard, injecting a refund into the bank feed -- a negative
credit referencing an already-matched transaction -- changed NOTHING:

    bank rows ingested      : 64 -> 65
    ingestion errors        :  2 ->  2
    decisions               : 61 -> 61
    exceptions              : 37 -> 37
    TXN_00001               : MATCHED / NONE  ->  MATCHED / NONE

The row ingested cleanly, was discarded by the tier-3 amount gate as a
non-candidate, and then appeared in no decision, no exception and no
error count. It was absorbed in total silence.

THE MECHANISM, WHICH IS THE INTERESTING PART
--------------------------------------------
This was not a hole in the guards. The amount gate that makes fuzzy
matching safe is EXACTLY what made the refund invisible: -1204.78 cannot
match an expected net of +1204.78, so it was correctly rejected as a
candidate and then silently forgotten.

The system was fail-closed against a wrong MATCH and silent about an
unmodelled TRANSACTION TYPE. Those are different properties, and only
one of them was guarded. See FAILURE_LOG.md section 65.

WHAT IS AND IS NOT BEING CLAIMED
--------------------------------
This does NOT implement refunds. It refuses them, explicitly and
countably, which is the same treatment the two corrupted records get:
reported, never dropped. Implementing refund semantics without the
settlement lifecycle around them would be worse than refusing them.
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from src.ingestion.loader import load_batch
from src.models import (
    UNSUPPORTED_TRANSACTION_TYPE,
    BankStatementRecord,
    InvoiceRecord,
    PGSettlementRecord,
    reject_negative,
)

RAW_DIR = ROOT / "data" / "raw"


# ======================================================================
# THE CONTRACT
# ======================================================================

def test_a_negative_bank_credit_is_refused():
    """The exact row that used to vanish."""
    with pytest.raises(ValidationError) as excinfo:
        BankStatementRecord(
            bank_ref="BANKREF_TXN_00001",
            utr="UTRREFUND01",
            narration="NEFT RETURN/REFUND MERCH_001",
            credited_amount="-1204.78",
            value_date="2026-08-01",
        )
    assert UNSUPPORTED_TRANSACTION_TYPE in str(excinfo.value)


def test_a_negative_pg_gross_is_refused():
    from datetime import datetime, timezone

    with pytest.raises(ValidationError) as excinfo:
        PGSettlementRecord(
            settlement_id="SET_TXN_00001", txn_id="TXN_00001",
            merchant_id="MERCH_001", gross_amount="-1000.00",
            pg_fee="20.00", gst_on_fee="3.60", tds_withheld="1.00",
            net_payout="-1024.60",
            timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
    assert UNSUPPORTED_TRANSACTION_TYPE in str(excinfo.value)


def test_a_negative_invoice_amount_is_refused():
    with pytest.raises(ValidationError) as excinfo:
        InvoiceRecord(
            invoice_id="INV_1", txn_id="TXN_00001",
            invoice_amount="-29.15", claimed_gst="3.60",
            claimed_tds="1.00", period="2026-08",
        )
    assert UNSUPPORTED_TRANSACTION_TYPE in str(excinfo.value)


def test_zero_is_not_negative():
    """
    THE BOUNDARY.

    A zero-value row is unusual, not unsupported -- and UPI is
    zero-rated, so a zero fee is CORRECT rather than missing. Rejecting
    zero here would refuse legitimate records and quietly change what
    the tax layer sees.
    """
    assert reject_negative(__import__("decimal").Decimal("0.00")) == 0


# ======================================================================
# THE FIELDS DELIBERATELY LEFT UNGUARDED
# ======================================================================

def test_fee_components_are_not_guarded():
    """
    The guard covers a TRANSACTION VALUE, never a component or a
    balance. `net_payout` can legitimately go negative when fees exceed
    a small gross -- that is an anomaly for the decision table to route,
    not an unsupported transaction type.

    Asserted so the guard's scope is a decision on record rather than an
    accident of which fields someone happened to annotate.
    """
    from datetime import datetime, timezone

    record = PGSettlementRecord(
        settlement_id="SET_X", txn_id="TXN_X", merchant_id="M1",
        gross_amount="10.00", pg_fee="20.00", gst_on_fee="3.60",
        tds_withheld="0.00", net_payout="-13.60",
        timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert record.net_payout < 0, (
        "net_payout was guarded -- it should not be; fees exceeding a "
        "small gross is an anomaly, not an unsupported type"
    )


# ======================================================================
# END TO END -- THE PART THAT ACTUALLY MATTERED
# ======================================================================

@pytest.fixture
def batch_with_a_refund(tmp_path):
    """A copy of the real batch with one refund row appended."""
    for f in RAW_DIR.iterdir():
        shutil.copy(f, tmp_path / f.name)

    rows = json.loads((tmp_path / "bank_statement.json").read_text(encoding="utf-8"))
    refund = dict(rows[0])
    refund.update(
        bank_ref="BANKREF_TXN_00001",
        utr="UTRREFUND01",
        narration="NEFT RETURN/REFUND MERCH_001",
        credited_amount="-1204.78",
    )
    rows.append(refund)
    (tmp_path / "bank_statement.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8"
    )
    return tmp_path


def test_a_refund_in_the_feed_is_counted_not_absorbed(batch_with_a_refund):
    """
    THE REGRESSION THIS FILE EXISTS FOR.

    The refund must raise the ingestion error count. Silence is the
    failure being prevented -- not the refund itself.
    """
    baseline = load_batch(RAW_DIR)
    withrefund = load_batch(batch_with_a_refund)

    assert withrefund.total_errors == baseline.total_errors + 1, (
        "a refund row did not change the ingestion error count -- it is "
        "being absorbed silently again"
    )


def test_the_refund_carries_its_own_error_code(batch_with_a_refund):
    """
    A malformed row and an unsupported transaction type are both
    rejections and are not the same finding. `IngestionError.error_code`
    documents itself as a stable, aggregatable identifier, so a refund
    must aggregate as a refund rather than hide inside the
    schema-failure count.
    """
    loaded = load_batch(batch_with_a_refund)
    codes = [e.error_code for e in loaded.bank.errors]

    assert UNSUPPORTED_TRANSACTION_TYPE in codes, (
        f"refund classified as {codes} -- it should have its own code"
    )


def test_the_rejected_refund_keeps_its_evidence(batch_with_a_refund):
    """
    A rejection an operator cannot inspect is barely better than a
    silent one. The raw payload must survive so the row can be found.
    """
    loaded = load_batch(batch_with_a_refund)
    refund = next(
        e for e in loaded.bank.errors
        if e.error_code == UNSUPPORTED_TRANSACTION_TYPE
    )

    assert refund.raw_record["credited_amount"] == "-1204.78"
    assert refund.raw_record["bank_ref"] == "BANKREF_TXN_00001"
    assert refund.source == "bank"
    assert isinstance(refund.index, int)


def test_the_real_batch_is_completely_unaffected():
    """
    THE CONTROL, and the reason this change was safe to make four days
    before a deadline.

    No record in the shipped batch carries a negative transaction value,
    so the guard must be invisible: same record counts, same two
    ingestion errors, and neither of them reclassified.
    """
    loaded = load_batch(RAW_DIR)

    assert loaded.total_errors == 2
    assert len(loaded.pg.valid_records) == 61
    assert len(loaded.bank.valid_records) == 64
    assert len(loaded.invoice.valid_records) == 60

    codes = {e.error_code for e in loaded.pg.errors} | {
        e.error_code for e in loaded.bank.errors
    } | {e.error_code for e in loaded.invoice.errors}
    assert codes == {"SCHEMA_VALIDATION_FAILED"}, (
        f"the guard reclassified an existing rejection: {codes}"
    )
