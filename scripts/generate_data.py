# scripts/generate_data.py
"""
Synthetic dataset generator for the reconciliation engine.

Produces three independent source files (PG settlement, bank statement,
merchant invoice) -- each as both JSON and CSV -- plus a hidden
ground_truth.json that is deliberately NEVER read by the reconciliation
pipeline itself. Ground truth exists solely for the evaluation layer,
so the system's reported match rate is an independently checkable
number rather than a self-reported claim.

Run:
    python scripts/generate_data.py

Reproducibility:
    The random seed is fixed in config.py (RANDOM_SEED). Re-running
    this script produces an identical dataset every time.
"""

from __future__ import annotations
import json
import csv
import random
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys
sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.config import (
    RANDOM_SEED,
    BATCH_DISTRIBUTION,
    GST_RATE_ON_FEE,
    TDS_RATE_SECTION_393,
    TDS_ANNUAL_THRESHOLD,
    money,
)


# =======================================================================
# SERIALIZATION HELPERS
# =======================================================================

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def write_json(data, path: Path):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, cls=DecimalEncoder)


def write_csv(records: list[dict], path: Path):
    if not records:
        path.write_text("")
        return
    fieldnames = list(records[0].keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


# =======================================================================
# DETERMINISTIC RNG & PATHS
# =======================================================================

rng = random.Random(RANDOM_SEED)

BASE_DATE = datetime(2026, 8, 1, tzinfo=timezone.utc)
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_DIR = OUTPUT_DIR / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

PAYMENT_METHODS = ["UPI", "CARD", "NETBANKING"]
NARRATION_METHODS = ["UPI", "NEFT", "IMPS"]


# =======================================================================
# MERCHANT POOL
# Merchants 1-3 are seeded just under the TDS annual threshold
# (INR 495,000 of 500,000) so that a handful of their transactions in
# this batch will genuinely cross into TDS-applicable territory. This
# starting figure represents each merchant's REAL prior-period
# cumulative gross -- i.e. what a real merchant ledger would already
# show before this batch's transactions occur. It is now written
# explicitly into each PG record as merchant_ytd_gross_opening, so
# that any downstream consumer (the seller ledger in tax validation)
# can independently reconstruct the correct threshold decision without
# needing access to this generator's private in-memory state.
# =======================================================================

MERCHANTS = [
    {
        "id": f"MERCH_{i:03d}",
        "gstin": f"29AAAAA{i:04d}A1Z5",
        "annual_gross_so_far": Decimal("495000.00") if i <= 3 else Decimal("0.00"),
    }
    for i in range(1, 16)
]


def pick_merchant():
    return rng.choice(MERCHANTS)


def pick_high_volume_merchant():
    """Used to bias a few clean transactions toward the seeded
    near-threshold merchants, increasing the odds the threshold is
    actually crossed within this batch."""
    return rng.choice(MERCHANTS[:3])


def next_txn_id(counter: list[int]) -> str:
    counter[0] += 1
    return f"TXN_{counter[0]:05d}"


# =======================================================================
# RECORD BUILDERS
# =======================================================================

txn_counter = [0]


def build_clean_transaction(merchant, txn_id, date_offset_days):
    """Baseline: a fully correct, fully matchable transaction."""
    gross = Decimal(rng.choice(["1000.00", "2500.00", "5400.00",
                                 "890.00", "12500.00", "3200.00"]))
    fee = money(gross * Decimal("0.02"))
    gst = money(fee * GST_RATE_ON_FEE)

    opening_gross = merchant["annual_gross_so_far"]  # captured BEFORE
                                                        # this transaction
                                                        # is applied --
                                                        # this is the
                                                        # merchant's true
                                                        # starting point,
                                                        # written into
                                                        # the record so
                                                        # it's real data,
                                                        # not private
                                                        # generator state
    merchant["annual_gross_so_far"] += gross
    tds = (money(gross * TDS_RATE_SECTION_393)
           if merchant["annual_gross_so_far"] > TDS_ANNUAL_THRESHOLD
           else Decimal("0.00"))

    net = money(gross - fee - gst - tds)
    ts = BASE_DATE + timedelta(days=date_offset_days, hours=rng.randint(0, 23))
    utr = f"UTR{rng.randint(100000000, 999999999)}"
    order_id = f"ORD_{txn_id}"
    payment_method = rng.choice(PAYMENT_METHODS)
    narration_method = rng.choice(NARRATION_METHODS)

    pg = {
        "settlement_id": f"SET_{txn_id}",
        "txn_id": txn_id,
        "order_id": order_id,
        "merchant_id": merchant["id"],
        "gross_amount": str(gross),
        "pg_fee": str(fee),
        "gst_on_fee": str(gst),
        "tds_withheld": str(tds),
        "net_payout": str(net),
        "merchant_gstin": merchant["gstin"],
        "merchant_ytd_gross_opening": str(opening_gross),
        "payment_method": payment_method,
        "utr": utr,
        "timestamp": ts.isoformat(),
    }
    bank = {
        "bank_ref": f"BANKREF_{txn_id}",
        "utr": utr,
        "credited_amount": str(net),
        "value_date": (ts + timedelta(days=1)).date().isoformat(),
        "narration": f"{narration_method} CR {utr} {merchant['id']}",
        "bank_charges": "0.00",
    }
    invoice = {
        "invoice_id": f"INV_{txn_id}",
        "txn_id": txn_id,
        "irn": f"{rng.randint(10**15, 10**16-1):064x}"[:64],
        "gstin": merchant["gstin"],
        "invoice_amount": str(money(fee + gst)),
        "claimed_gst": str(gst),
        "claimed_tds": str(tds),
        "period": ts.strftime("%Y-%m"),
    }
    ground_truth = {
        "txn_id": txn_id,
        "expected_status": "MATCHED",
        "expected_exception_code": "NONE",
        "category": "exact_match",
        "notes": ("Clean match across all three sources; TDS applied "
                  f"(merchant crossed INR {TDS_ANNUAL_THRESHOLD} threshold)."
                  if tds > 0 else
                  "Clean match across all three sources; no discrepancies."),
    }
    return pg, bank, invoice, ground_truth


def build_timing_difference(merchant, txn_id, date_offset_days):
    pg, bank, invoice, gt = build_clean_transaction(merchant, txn_id, date_offset_days)
    pg_ts = datetime.fromisoformat(pg["timestamp"])
    lag = rng.choice([2, 3])
    bank["value_date"] = (pg_ts + timedelta(days=lag)).date().isoformat()
    gt["category"] = "timing_difference"
    gt["expected_status"] = "MATCHED"
    gt["notes"] = f"T+{lag} settlement lag with exact amounts; normal, not an exception."
    return pg, bank, invoice, gt


def build_reference_mismatch(merchant, txn_id, date_offset_days):
    """UTR slightly garbled in the bank feed; recoverable only via fuzzy
    match. NOTE: the bank record is still linked to this transaction via
    its bank_ref (BANKREF_<txn_id>) -- only the UTR field itself is
    corrupted, simulating a real-world scenario where the bank's own
    reference is intact but the UTR was mistyped or OCR'd incorrectly."""
    pg, bank, invoice, gt = build_clean_transaction(merchant, txn_id, date_offset_days)
    original = bank["utr"]
    pos = rng.randint(0, len(original) - 1)
    corrupted = original[:pos] + rng.choice("0123456789") + original[pos+1:]
    method = bank["narration"].split(" ")[0]
    bank["utr"] = corrupted
    bank["narration"] = f"{method} CR {corrupted} {merchant['id']}"
    gt["category"] = "reference_mismatch_fuzzy"
    gt["expected_status"] = "MATCHED"
    gt["notes"] = "UTR digit corrupted on bank side; recoverable via amount+date-gated fuzzy match."
    return pg, bank, invoice, gt


def build_amount_discrepancy(merchant, txn_id, date_offset_days):
    pg, bank, invoice, gt = build_clean_transaction(merchant, txn_id, date_offset_days)
    drift = Decimal(rng.choice(["5.00", "12.50", "0.75", "20.00"]))
    bank["credited_amount"] = str(money(Decimal(bank["credited_amount"]) - drift))
    gt["category"] = "amount_fee_discrepancy"
    gt["expected_status"] = "HUMAN_REVIEW"
    gt["expected_exception_code"] = "AMOUNT_MISMATCH"
    gt["notes"] = f"Bank-credited amount is short by INR {drift}; genuine discrepancy, not rounding noise."
    return pg, bank, invoice, gt


def build_tax_mismatch(merchant, txn_id, date_offset_days):
    pg, bank, invoice, gt = build_clean_transaction(merchant, txn_id, date_offset_days)
    error_type = rng.choice(["stale_tds_rate", "wrong_gst_pct"])
    if error_type == "stale_tds_rate":
        gross = Decimal(pg["gross_amount"])
        wrong_tds = money(gross * Decimal("0.01"))
        invoice["claimed_tds"] = str(wrong_tds)
        gt["expected_exception_code"] = "ERR_TDS_VARIANCE"
        gt["notes"] = "Invoice uses stale 1% Section 393 TDS rate instead of current 0.1%."
    else:
        fee = Decimal(pg["pg_fee"])
        wrong_gst = money(fee * Decimal("0.12"))
        invoice["claimed_gst"] = str(wrong_gst)
        gt["expected_exception_code"] = "ERR_GST_MISMATCH"
        gt["notes"] = "Claimed GST on fee reflects a 12% slab instead of the correct 18%."
    gt["category"] = "tax_mismatch"
    gt["expected_status"] = "TAX_MISMATCH"
    return pg, bank, invoice, gt


def build_missing_in_source(merchant, txn_id, date_offset_days):
    pg, bank, invoice, gt = build_clean_transaction(merchant, txn_id, date_offset_days)
    dropped = rng.choice(["bank", "invoice"])
    gt["category"] = "missing_in_source"
    if dropped == "bank":
        gt["expected_exception_code"] = "MISSING_IN_BANK"
        gt["expected_status"] = "UNMATCHED"
        gt["notes"] = "PG and invoice agree; settlement not yet reflected in the bank feed."
        return pg, None, invoice, gt
    else:
        gt["expected_exception_code"] = "MISSING_IN_INVOICE"
        gt["expected_status"] = "PARTIAL_MATCH"
        gt["notes"] = "PG and bank agree; invoice not generated, so tax cannot be verified."
        return pg, bank, None, gt


def build_duplicate(merchant, txn_id, date_offset_days):
    pg, bank, invoice, gt = build_clean_transaction(merchant, txn_id, date_offset_days)
    gt["category"] = "duplicate"
    gt["expected_exception_code"] = "DUPLICATE_DETECTED"
    gt["expected_status"] = "AMBIGUOUS"
    gt["notes"] = "Same transaction credited twice in the bank feed; duplicate row injected at batch assembly."
    return pg, bank, invoice, gt


def build_ambiguous(merchant, txn_id, date_offset_days):
    pg, bank, invoice, gt = build_clean_transaction(merchant, txn_id, date_offset_days)
    gt["category"] = "ambiguous"
    gt["expected_exception_code"] = "AMBIGUOUS_MATCH"
    gt["expected_status"] = "AMBIGUOUS"
    gt["notes"] = "A sibling transaction shares the same amount and date; genuinely ambiguous without a stronger signal."
    return pg, bank, invoice, gt


def build_corrupted(merchant, txn_id, date_offset_days):
    pg, bank, invoice, gt = build_clean_transaction(merchant, txn_id, date_offset_days)
    pg["gross_amount"] = "NOT_A_NUMBER"
    gt["category"] = "corrupted"
    gt["expected_exception_code"] = "CORRUPTED_RECORD"
    gt["expected_status"] = "UNMATCHED"
    gt["notes"] = "PG record has a malformed gross_amount field; must fail validation gracefully."
    return pg, bank, invoice, gt


def build_unresolvable(merchant, txn_id, date_offset_days):
    """No defensible automated resolution. The bank record is still
    linked via bank_ref for referential-integrity purposes, but its UTR
    is nulled and its amount/date both drift -- simulating a case where
    every individual signal is degraded simultaneously."""
    pg, bank, invoice, gt = build_clean_transaction(merchant, txn_id, date_offset_days)
    bank["credited_amount"] = str(money(Decimal(bank["credited_amount"]) - Decimal("50.00")))
    bank["value_date"] = (
        datetime.fromisoformat(pg["timestamp"]) + timedelta(days=9)
    ).date().isoformat()
    bank["utr"] = None
    gt["category"] = "unresolvable"
    gt["expected_exception_code"] = "HUMAN_REVIEW_REQUIRED"
    gt["expected_status"] = "UNMATCHED"
    gt["notes"] = "Amount, date, and UTR all diverge simultaneously; no defensible automated resolution."
    return pg, bank, invoice, gt


# =======================================================================
# BATCH ASSEMBLY
# =======================================================================

BUILDERS = {
    "exact_match": build_clean_transaction,
    "timing_difference": build_timing_difference,
    "reference_mismatch_fuzzy": build_reference_mismatch,
    "amount_fee_discrepancy": build_amount_discrepancy,
    "tax_mismatch": build_tax_mismatch,
    "missing_in_source": build_missing_in_source,
    "duplicate": build_duplicate,
    "ambiguous": build_ambiguous,
    "corrupted": build_corrupted,
    "unresolvable": build_unresolvable,
}

# Categories biased toward the near-threshold merchants, to increase
# the odds the TDS threshold is genuinely crossed within this batch.
HIGH_VOLUME_BIAS_CATEGORIES = {"exact_match", "timing_difference", "tax_mismatch"}


def generate_batch():
    pg_records, bank_records, invoice_records, ground_truth = [], [], [], []
    category_counts = {k: 0 for k in BATCH_DISTRIBUTION}

    day_cursor = 0
    for category, count in BATCH_DISTRIBUTION.items():
        builder = BUILDERS[category]
        for i in range(count):
            # bias roughly 1-in-3 of certain categories toward the
            # near-threshold merchants so the TDS threshold actually
            # gets exercised somewhere in the batch
            if category in HIGH_VOLUME_BIAS_CATEGORIES and i % 3 == 0:
                merchant = pick_high_volume_merchant()
            else:
                merchant = pick_merchant()

            txn_id = next_txn_id(txn_counter)
            pg, bank, invoice, gt = builder(merchant, txn_id, day_cursor)
            day_cursor = (day_cursor + 1) % 20
            category_counts[category] += 1

            if pg is not None:
                pg_records.append(pg)
            if bank is not None:
                bank_records.append(bank)
            if invoice is not None:
                invoice_records.append(invoice)
            ground_truth.append(gt)

            if category == "duplicate" and bank is not None:
                dup = dict(bank)
                dup["bank_ref"] = bank["bank_ref"] + "_DUP"
                bank_records.append(dup)

            if category == "ambiguous" and pg is not None:
                sibling_merchant = pick_merchant()
                sibling_txn_id = next_txn_id(txn_counter)
                s_pg, s_bank, s_invoice, s_gt = build_clean_transaction(
                    sibling_merchant, sibling_txn_id, day_cursor
                )
                s_pg["gross_amount"] = pg["gross_amount"]
                s_pg["timestamp"] = pg["timestamp"]
                s_gt["category"] = "ambiguous"
                s_gt["notes"] = "Sibling of an ambiguous pair; same amount and date as its counterpart."
                pg_records.append(s_pg)
                bank_records.append(s_bank)
                invoice_records.append(s_invoice)
                ground_truth.append(s_gt)

    rng.shuffle(pg_records)
    rng.shuffle(bank_records)
    rng.shuffle(invoice_records)

    return pg_records, bank_records, invoice_records, ground_truth, category_counts


def print_summary(category_counts: dict, ground_truth: list, pg_records: list):
    print(f"\nGenerated dataset (seed={RANDOM_SEED})\n")
    label_width = max(len(k) for k in category_counts) + 2
    for category, count in category_counts.items():
        dots = "." * (label_width - len(category) + 10)
        print(f"  {category} {dots} {count}")
    print(f"\n  {'total base transactions':.<{label_width + 10}} {sum(category_counts.values())}")
    print(f"  {'total ground-truth entries':.<{label_width + 10}} {len(ground_truth)}")
    print("  (ground-truth total exceeds base transactions because the")
    print("   'ambiguous' category injects one extra linked sibling record)")

    tds_positive = sum(1 for r in pg_records
                        if r["gross_amount"] != "NOT_A_NUMBER"
                        and Decimal(r["tds_withheld"]) > 0)
    print(f"\n  records with TDS > 0 (threshold genuinely crossed): {tds_positive}")


def main():
    print(f"Generating synthetic reconciliation batch (seed={RANDOM_SEED})...")
    pg_records, bank_records, invoice_records, ground_truth, category_counts = generate_batch()

    write_json(pg_records, RAW_DIR / "pg_settlement.json")
    write_json(bank_records, RAW_DIR / "bank_statement.json")
    write_json(invoice_records, RAW_DIR / "merchant_invoice.json")
    write_json(ground_truth, OUTPUT_DIR / "ground_truth.json")

    write_csv(pg_records, RAW_DIR / "pg_settlement.csv")
    write_csv(bank_records, RAW_DIR / "bank_statement.csv")
    write_csv(invoice_records, RAW_DIR / "merchant_invoice.csv")

    print_summary(category_counts, ground_truth, pg_records)
    print(f"\nWrote JSON + CSV sources to {RAW_DIR}")
    print(f"Wrote ground_truth.json to {OUTPUT_DIR} "
          f"-- the pipeline must NEVER read this file.")


if __name__ == "__main__":
    main()