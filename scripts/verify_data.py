# scripts/verify_data.py
"""
Sanity-check / validation script for the synthetic dataset generated
by scripts/generate_data.py.

BANK LINKAGE, AND WHY IT CHANGED TWICE
--------------------------------------
Version 1 indexed bank rows by UTR. That was wrong: UTR is
deliberately corrupted or nulled in two categories, so the verifier
mistook "the UTR was corrupted on purpose" for "the bank record is
missing" and produced false-positive failures on exactly the
categories that mattered.

Version 2 indexed by the txn_id embedded in bank_ref
("BANKREF_TXN_00025" -> "TXN_00025"), which was UTR-independent and
correct at the time.

UPGRADE B broke that assumption on purpose. The
reference_mismatch_fuzzy category now emits bank rows with a
BANK-NATIVE reference and no UTR field, because BANKREF_<txn_id> is a
convention no real bank provides and its presence meant tier 2
resolved every record before the fuzzy tier was ever consulted.

So the verifier now uses two linkage paths, and the second one is a
VERIFICATION AFFORDANCE that the pipeline does not get:

    1. bank_ref of the form BANKREF_<txn_id> (and _DUP)
    2. fuzzy match of a PG record's UTR against the bank narration

Path 2 uses the same rapidfuzz call the engine uses, which looks
circular but is not. The verifier's job here is only to answer "does a
bank row for this transaction exist in the file at all?" -- a question
about the DATA. The engine has to answer a different question: "which
bank row, if any, may I safely link, given amount and date guards and
a candidate pool of 63 rows?" That is the property under test, and
nothing here does it for the engine.

Run:
    python scripts/verify_data.py
"""

from __future__ import annotations
import json
from decimal import Decimal
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parent.parent))

from rapidfuzz import fuzz

from src.config import (
    GST_RATE_ON_FEE,
    TDS_ANNUAL_THRESHOLD,
    TAX_TOLERANCE,
    BATCH_DISTRIBUTION,
    FUZZY_MIN_SIMILARITY,
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


def index_bank_by_pg_txn(bank_records: list[dict], pg_records: list[dict]) -> dict:
    """
    Index bank rows against the transaction they belong to.

    Two paths, because after UPGRADE B not every bank row carries our
    reference convention:

      1. bank_ref of the form BANKREF_<txn_id> (and _DUP). Used by most
         categories.

      2. A UTR matched fuzzily against the narration. Used by
         reference_mismatch_fuzzy, whose rows carry a bank-native ref,
         no UTR field, and sometimes a corrupted UTR inside the
         narration text -- so exact substring matching would miss half
         of them.

    Path 2 is a VERIFICATION affordance. It answers "does a bank row
    for this transaction exist in the file?", which is a question about
    the data. The pipeline must independently answer "which row may I
    safely link?" through guarded matching against the full candidate
    pool, and nothing here does that for it.
    """
    utr_by_txn = {
        r["txn_id"]: r["utr"]
        for r in pg_records
        if r.get("utr") and r.get("txn_id")
    }

    idx: dict = {}

    for row in bank_records:
        ref = row.get("bank_ref", "") or ""

        if ref.startswith("BANKREF_"):
            remainder = ref[len("BANKREF_"):]
            txn_id = remainder.split("_DUP")[0]
            idx.setdefault(txn_id, []).append(row)
            continue

        # Bank-native ref: fall back to the UTR in the narration.
        narration = row.get("narration", "") or ""
        if not narration:
            continue

        best_txn = None
        best_score = 0.0

        for txn_id, utr in utr_by_txn.items():
            score = fuzz.partial_ratio(utr, narration)
            if score > best_score:
                best_score = score
                best_txn = txn_id

        if best_txn is not None and best_score >= FUZZY_MIN_SIMILARITY:
            idx.setdefault(best_txn, []).append(row)

    return idx


def main():
    print("Loading generated dataset...\n")

    pg = load_json(RAW_DIR / "pg_settlement.json")
    bank = load_json(RAW_DIR / "bank_statement.json")
    invoice = load_json(RAW_DIR / "merchant_invoice.json")
    ground_truth = load_json(DATA_DIR / "ground_truth.json")

    pg_by_txn = index_by_txn_id(pg)
    bank_by_txn = index_bank_by_pg_txn(bank, pg)
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
    print("CHECK 6: Reference-mismatch rows genuinely require the fuzzy tier")
    print("=" * 70)
    #
    # UPGRADE B replaced this check entirely.
    #
    # It used to assert that bank["utr"] differed from pg["utr"]. That
    # no longer describes the category: the bank row now exposes NO utr
    # field at all, because a feed that gives you a clean structured
    # UTR does not need the fuzzy tier in the first place.
    #
    # The property that actually matters is whether these rows are
    # unresolvable by tiers 1 and 2:
    #
    #     utr is None                  -> tier 1 cannot fire
    #     bank_ref is bank-native      -> tier 2 cannot fire
    #
    # If either fails, the fuzzy tier goes back to being dead code and
    # every number measured on it is vacuous. That is exactly what
    # happened before FIX (A2), and it went unnoticed for a long time.

    ref_mismatch_txns = [e["txn_id"] for e in ground_truth
                          if e["category"] == "reference_mismatch_fuzzy"]

    for txn_id in ref_mismatch_txns:
        bank_rows = bank_by_txn.get(txn_id, [])

        if not bank_rows:
            fail(f"  {txn_id}: no linked bank record found at all")
            continue

        row = bank_rows[0]
        ref = str(row.get("bank_ref", "") or "")

        if row.get("utr") is not None:
            fail(f"  {txn_id}: bank row exposes a UTR field, so tier 1 "
                 f"will resolve it and the fuzzy tier stays dead")
        elif ref.startswith("BANKREF_"):
            fail(f"  {txn_id}: bank_ref still uses our own convention "
                 f"({ref}), so tier 2 will resolve it before fuzzy")
        elif not row.get("narration"):
            fail(f"  {txn_id}: no narration, so the fuzzy tier has "
                 f"nothing to match against")
        else:
            ok()

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
    print("CHECK 8: Narration formats are varied, not one template")
    print("=" * 70)
    #
    # UPGRADE B. A single narration template made the fuzzy tier look
    # better than it is: every record scored 100 because the format was
    # uniform and the UTR always sat in the same position.
    #
    # This does not prove the formats are REALISTIC -- they are still
    # invented, and README.md says so. It proves only that the matcher
    # is not being handed one shape repeatedly.

    narrations = [r.get("narration", "") for r in bank if r.get("narration")]
    shapes = set()
    for narration in narrations:
        # Crude shape signature: which separators appear.
        shapes.add((
            "-" in narration,
            "/" in narration,
            "*" in narration,
            " CR " in narration,
        ))

    if len(shapes) < 3:
        fail(f"  Only {len(shapes)} distinct narration shape(s); the "
             f"fuzzy tier is being measured against a uniform format")
    else:
        ok()

    print(f"  Distinct narration shapes: {len(shapes)}")

    # -------------------------------------------------------------
    print("\n" + "=" * 70)
    print("CHECK 9: Corrupted UTRs remain recoverable")
    print("=" * 70)
    #
    # CORRECTION. An earlier version of CHECK 8 counted narrations with
    # no literal "UTR" substring and labelled them:
    #
    #     "UPI form -- unrecoverable by narration matching, on purpose"
    #
    # That was wrong on both counts, and worth recording rather than
    # quietly editing.
    #
    # The one row it found was:
    #
    #     UPI/3TR694524394/MERCH_004/NET STLMNT
    #
    # That is NOT the UPI form. It is the standard
    # "{method}/{utr}/{merchant}/NET STLMNT" shape where the
    # single-character corruption in build_reference_mismatch landed on
    # index 0 -- turning "UTR694524394" into "3TR694524394". Eleven of
    # twelve characters still match, so partial_ratio recovers it
    # comfortably. It is entirely recoverable.
    #
    # The UPI branch of _make_narration is in fact UNREACHABLE: every
    # caller passes a real UTR, so the `utr is None` path never fires.
    # The label described a code path that does not execute.
    #
    # This check now measures the real property: a corrupted UTR must
    # stay above the fuzzy threshold. Corruption landing on the "UTR"
    # prefix rather than the digits is realistic -- OCR and manual
    # entry both do it -- and it is worth seeing when it happens rather
    # than mislabelling it.

    pg_by_id = {r["txn_id"]: r for r in pg}
    prefix_corrupted = []
    below_threshold = []

    for txn_id in ref_mismatch_txns:
        pg_record = pg_by_id.get(txn_id)
        bank_rows = bank_by_txn.get(txn_id, [])

        if pg_record is None or not bank_rows:
            continue

        narration = bank_rows[0].get("narration", "") or ""
        original_utr = pg_record.get("utr") or ""

        score = fuzz.partial_ratio(original_utr, narration)

        if "UTR" not in narration:
            prefix_corrupted.append((txn_id, narration, score))

        if score < FUZZY_MIN_SIMILARITY:
            below_threshold.append((txn_id, score))
            fail(f"  {txn_id}: narration similarity {score:.0f} is below "
                 f"the production threshold {FUZZY_MIN_SIMILARITY}; the "
                 f"fuzzy tier cannot recover it")
        else:
            ok()

    print(f"  Checked {len(ref_mismatch_txns)} reference-mismatch rows against "
          f"the production threshold ({FUZZY_MIN_SIMILARITY}).")

    if prefix_corrupted:
        print(f"  {len(prefix_corrupted)} row(s) had the corruption land on the "
              f"'UTR' prefix rather than the digits:")
        for txn_id, narration, score in prefix_corrupted:
            print(f"    {txn_id}  similarity {score:.0f}  {narration}")
        print("    (still recoverable -- 11 of 12 characters match)")

    if not below_threshold:
        print("  Every corrupted UTR remains above the threshold.")

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
        print("  One or more checks failed. Review before proceeding.")

    return len(failures) == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)