# scripts/verify_data.py
"""
Sanity-check / validation script for the synthetic dataset generated
by scripts/generate_data.py.

CORRECTED VERSION: bank records are now indexed by the txn_id embedded
in their bank_ref (e.g. "BANKREF_TXN_00025" -> "TXN_00025"), NOT by
UTR. UTR is deliberately unreliable in the reference_mismatch_fuzzy and
unresolvable categories -- indexing by UTR was a bug in the first
version of this script that produced false-positive failures on
exactly those two categories, since it mistook "the UTR was
deliberately corrupted" for "the bank record is missing."

Run:
    python scripts/verify_data.py
"""

from __future__ import annotations
import json
from decimal import Decimal
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.config import (
    GST_RATE_ON_FEE,
    TDS_ANNUAL_THRESHOLD,
    TAX_TOLERANCE,
    BATCH_DISTRIBUTION,
    money,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_DIR = DATA_DIR / "raw"

failures: list[str] = []
warnings: list[str] = []
checks_run = 0


def fail(msg: str):
    global checks_run
    checks_run += 1
    failures.append(msg)


def warn(msg: str):
    warnings.append(msg)


def ok():
    global checks_run
    checks_run += 1


def load_json(path: Path):
    with open(path) as f:
        return json.load(f)


def index_by_txn_id(records: list[dict], key: str = "txn_id") -> dict:
    idx = {}
    for r in records:
        idx.setdefault(r.get(key), []).append(r)
    return idx


def index_bank_by_pg_txn(bank_records: list[dict]) -> dict:
    """Index bank rows by the txn_id embedded in bank_ref, e.g.
    'BANKREF_TXN_00025' -> 'TXN_00025', and 'BANKREF_TXN_00025_DUP'
    also maps to 'TXN_00025'. This is UTR-independent, which matters
    because UTR is deliberately corrupted/nulled in two categories."""
    idx = {}
    for r in bank_records:
        ref = r.get("bank_ref", "")
        if ref.startswith("BANKREF_"):
            remainder = ref[len("BANKREF_"):]
            txn_id = remainder.split("_DUP")[0]
            idx.setdefault(txn_id, []).append(r)
    return idx


def main():
    print("Loading generated dataset...\n")

    pg = load_json(RAW_DIR / "pg_settlement.json")
    bank = load_json(RAW_DIR / "bank_statement.json")
    invoice = load_json(RAW_DIR / "merchant_invoice.json")
    ground_truth = load_json(DATA_DIR / "ground_truth.json")

    pg_by_txn = index_by_txn_id(pg)
    bank_by_txn = index_bank_by_pg_txn(bank)
    invoice_by_txn = index_by_txn_id(invoice)

    print(f"  pg_settlement.json   : {len(pg)} records")
    print(f"  bank_statement.json  : {len(bank)} records")
    print(f"  merchant_invoice.json: {len(invoice)} records")
    print(f"  ground_truth.json    : {len(ground_truth)} records\n")

    # -------------------------------------------------------------
    print("=" * 70)
    print("CHECK 1: Category distribution")
    print("=" * 70)

    gt_category_counts = {}
    for entry in ground_truth:
        cat = entry["category"]
        gt_category_counts[cat] = gt_category_counts.get(cat, 0) + 1

    for category, expected_count in BATCH_DISTRIBUTION.items():
        actual_count = gt_category_counts.get(category, 0)
        expected_gt_count = expected_count * 2 if category == "ambiguous" else expected_count
        if actual_count != expected_gt_count:
            fail(f"Category '{category}': expected {expected_gt_count} ground-truth "
                 f"entries, found {actual_count}")
        else:
            ok()
        print(f"  {category:<28} expected={expected_gt_count:<4} actual={actual_count}")

    # -------------------------------------------------------------
    print("\n" + "=" * 70)
    print("CHECK 2: PG settlement tax math (GST 18% on fee)")
    print("=" * 70)

    tax_checked = 0
    tax_correct = 0

    for record in pg:
        try:
            gross = Decimal(record["gross_amount"])
        except Exception:
            warn(f"  {record.get('txn_id', '?')}: gross_amount unparseable "
                 f"(expected for corrupted-category records)")
            continue

        fee = Decimal(record["pg_fee"])
        expected_gst = money(fee * GST_RATE_ON_FEE)
        actual_gst = Decimal(record["gst_on_fee"])

        tax_checked += 1
        if abs(expected_gst - actual_gst) > TAX_TOLERANCE:
            fail(f"  {record['txn_id']}: GST mismatch -- expected {expected_gst}, "
                 f"generator wrote {actual_gst}")
        else:
            tax_correct += 1
            ok()

    print(f"  GST math verified correct on {tax_correct}/{tax_checked} PG records")

    # -------------------------------------------------------------
    print("\n" + "=" * 70)
    print("CHECK 3: TDS threshold behavior sanity")
    print("=" * 70)

    valid_pg = [r for r in pg if r["gross_amount"] != "NOT_A_NUMBER"]
    zero_tds = [r for r in valid_pg if Decimal(r["tds_withheld"]) == 0]
    nonzero_tds = [r for r in valid_pg if Decimal(r["tds_withheld"]) > 0]

    print(f"  Records with TDS = 0    : {len(zero_tds)} "
          f"(merchants still under INR {TDS_ANNUAL_THRESHOLD} cumulative)")
    print(f"  Records with TDS > 0    : {len(nonzero_tds)} "
          f"(merchants who have crossed the threshold)")

    if len(nonzero_tds) == 0:
        fail("  No records have TDS > 0 -- the threshold boundary is present "
             "in code but never genuinely exercised by this dataset.")
    else:
        ok()

    # -------------------------------------------------------------
    print("\n" + "=" * 70)
    print("CHECK 4: Referential integrity (does each record exist where expected?)")
    print("=" * 70)

    for entry in ground_truth:
        txn_id = entry["txn_id"]
        category = entry["category"]

        in_pg = txn_id in pg_by_txn
        in_bank = txn_id in bank_by_txn
        in_invoice = txn_id in invoice_by_txn

        if category == "missing_in_source":
            if in_pg and (in_bank != in_invoice):
                ok()
            else:
                fail(f"  {txn_id} (missing_in_source): expected exactly one of "
                     f"bank/invoice absent, got bank={in_bank}, invoice={in_invoice}")
        elif category == "corrupted":
            ok()
        else:
            if in_pg and in_bank and in_invoice:
                ok()
            else:
                fail(f"  {txn_id} ({category}): expected present in all 3 sources, "
                     f"got pg={in_pg}, bank={in_bank}, invoice={in_invoice}")

    print(f"  Checked referential integrity for all {len(ground_truth)} ground-truth entries.")

    # -------------------------------------------------------------
    print("\n" + "=" * 70)
    print("CHECK 5: Duplicate category has a real duplicate bank row")
    print("=" * 70)

    duplicate_txns = [e["txn_id"] for e in ground_truth if e["category"] == "duplicate"]
    for txn_id in duplicate_txns:
        bank_rows = bank_by_txn.get(txn_id, [])
        if len(bank_rows) == 2:
            ok()
        else:
            fail(f"  {txn_id}: expected 2 bank rows, found {len(bank_rows)}")

    print(f"  Checked {len(duplicate_txns)} duplicate-category transactions.")

    # -------------------------------------------------------------
    print("\n" + "=" * 70)
    print("CHECK 6: Reference-mismatch category has a REAL UTR discrepancy")
    print("=" * 70)

    ref_mismatch_txns = [e["txn_id"] for e in ground_truth
                          if e["category"] == "reference_mismatch_fuzzy"]
    for txn_id in ref_mismatch_txns:
        pg_record = pg_by_txn.get(txn_id, [{}])[0]
        bank_rows = bank_by_txn.get(txn_id, [])
        if not bank_rows:
            fail(f"  {txn_id}: no linked bank record found via bank_ref")
            continue
        pg_utr = pg_record.get("utr")
        bank_utr = bank_rows[0].get("utr")
        if pg_utr != bank_utr and bank_utr is not None:
            ok()
        else:
            fail(f"  {txn_id}: expected a genuinely corrupted UTR, "
                 f"pg={pg_utr} bank={bank_utr}")

    print(f"  Checked {len(ref_mismatch_txns)} reference-mismatch transactions.")

    # -------------------------------------------------------------
    print("\n" + "=" * 70)
    print("CHECK 7: Ambiguous category pairs share amount + date")
    print("=" * 70)

    ambiguous_entries = [e for e in ground_truth if e["category"] == "ambiguous"]
    signature_groups = {}
    for entry in ambiguous_entries:
        pg_record = pg_by_txn.get(entry["txn_id"], [{}])[0]
        sig = (pg_record.get("gross_amount"), pg_record.get("timestamp", "")[:10])
        signature_groups.setdefault(sig, []).append(entry["txn_id"])

    genuine_pairs = sum(1 for group in signature_groups.values() if len(group) >= 2)
    if genuine_pairs == 0 and ambiguous_entries:
        fail("  No ambiguous entries actually share amount+date.")
    else:
        ok()
    print(f"  Found {genuine_pairs} genuinely ambiguous group(s) "
          f"across {len(ambiguous_entries)} ambiguous-category entries.")

    # -------------------------------------------------------------
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Checks run   : {checks_run}")
    print(f"  Failures     : {len(failures)}")
    print(f"  Warnings     : {len(warnings)}")

    if failures:
        print("\n  FAILURES:")
        for f in failures:
            print(f"    - {f}")

    if warnings:
        print("\n  WARNINGS:")
        for w in warnings:
            print(f"    - {w}")

    print()
    if not failures:
        print("  All automated checks passed. Spend 5 minutes manually skimming")
        print("  a few records per category before treating Phase 1 as closed --")
        print("  this script checks structure and math, not human judgment calls.")
    else:
        print("  One or more checks failed. Review before proceeding to Phase 2.")

    return len(failures) == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)