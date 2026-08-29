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

GROUND-TRUTH LABELLING RULE
---------------------------
expected_status must describe what the DECISION TABLE will actually
produce for the data this builder emits -- not what the category name
suggests, and not what a human would informally call the situation.

Two labels violated that rule and were corrected (see FIX (L1) and
FIX (L2) below). Both made a correct engine look broken. Ground truth
that disagrees with a correct engine is worse than no ground truth:
it burns review time on phantom defects and trains the team to ignore
the harness.

The decision table's actual mapping, for reference:

    no_candidates_found (bank AND invoice absent) -> UNMATCHED
    duplicate_detected                            -> HUMAN_REVIEW
    is_ambiguous                                  -> AMBIGUOUS
    missing_bank                                  -> UNMATCHED
    missing_invoice                               -> PARTIAL_MATCH
    amount_mismatch                               -> HUMAN_REVIEW

DATA-REALISM RULE
-----------------
The generator must not make the reconciliation problem artificially
easy OR artificially hard. See FIX (A1) in _draw_gross(): an earlier
version drew gross from six fixed values, which manufactured exact
net collisions throughout the batch and caused the fuzzy tier to
measure 0.13 precision. That number described the generator, not the
engine.
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

# Merchants 1-3 are the seeded near-threshold cohort.
NEAR_THRESHOLD_MERCHANTS = MERCHANTS[:3]
ZERO_BASE_MERCHANTS = MERCHANTS[3:]


def pick_merchant():
    return rng.choice(MERCHANTS)


def pick_high_volume_merchant():
    """Used to bias a few clean transactions toward the seeded
    near-threshold merchants, increasing the odds the threshold is
    actually crossed within this batch."""
    return rng.choice(NEAR_THRESHOLD_MERCHANTS)


def pick_zero_base_merchant():
    """Merchants 4-15 start at zero cumulative gross and, across a
    batch this size, cannot accumulate the INR 500,000 needed to cross
    the TDS threshold.

    This matters specifically for the ambiguous pair. Ambiguity is
    detected by comparing a PG record's EXPECTED NET against candidate
    bank amounts, and AMOUNT_TOLERANCE is INR 0.01. If one member of
    the pair withheld TDS and the other did not, their nets would
    differ by 0.1% of gross -- orders of magnitude above the tolerance
    -- and the amount collision that CREATES the ambiguity would
    silently fail to exist.

    Drawing both members from the zero-base cohort guarantees TDS == 0
    on both sides, so identical gross yields identical net. The pair
    therefore genuinely collides, which is the whole point of the
    category. TDS is exercised by other categories, not this one.

    ACKNOWLEDGED CONSTRAINT: this is the generator being shaped to fit
    the measurement. It is defensible -- isolating one variable is
    normal test design -- but it does mean the ambiguous category can
    never exercise TDS. Recorded in FAILURE_LOG.md rather than left
    implicit."""
    return rng.choice(ZERO_BASE_MERCHANTS)


def next_txn_id(counter: list[int]) -> str:
    counter[0] += 1
    return f"TXN_{counter[0]:05d}"


# =======================================================================
# AMOUNT DISTRIBUTION
# =======================================================================

def _draw_gross() -> Decimal:
    """
    Draw a transaction gross amount to paise precision.

    FIX (A1) -- WHY NOT A FIXED SET OF VALUES
    -----------------------------------------
    An earlier version drew gross from six fixed amounts:

        ["1000.00", "2500.00", "5400.00", "890.00", "12500.00", "3200.00"]

    With 63 records that placed roughly ten transactions on each value.
    Because pg_fee and GST are fixed percentages of gross, identical
    gross produced an IDENTICAL EXPECTED NET -- so exact net collisions
    were everywhere by construction, not by accident:

        - nine bank rows landed on exactly 12205.00
        - the fuzzy tier measured precision 0.13 at threshold 85,
          with 41 false positives against 6 true positives
        - the date window was doing nearly all the discriminating work
          in ambiguity detection, because the amount guard could not
          separate anything
        - unrelated categories supplied accidental ambiguity evidence
          to each other

    Every one of those numbers described the GENERATOR, not the engine.
    Documenting 0.13 as a fuzzy-matching limitation understated the
    matching layer and mis-attributed a data artifact to the code.

    LOG-UNIFORM, NOT FLAT
    ---------------------
    Real transaction values cluster at the low end with a long tail. A
    flat uniform draw across the full range would over-represent large
    amounts and misrepresent the distribution a matcher actually faces.
    Drawing the order of magnitude first -- weighted toward thousands
    -- then a mantissa within it, approximates that shape cheaply and
    deterministically.

    PAISE PRECISION IS THE POINT
    ----------------------------
    AMOUNT_TOLERANCE is INR 0.01. Drawing to paise rather than whole
    rupees is what makes accidental collision within tolerance go from
    routine to vanishingly unlikely. Rounding to rupees would leave a
    weaker version of the same artifact.

    DELIBERATE COLLISION IS UNAFFECTED
    ----------------------------------
    The two categories that REQUIRE collision still get it, because
    both copy rather than redraw:

        ambiguous  -- build_ambiguous_sibling() copies the
                      counterpart's gross_amount explicitly
        duplicate  -- the duplicate bank row is dict(bank)

    What disappears is only ACCIDENTAL collision, which was
    contaminating the measurements.
    """
    # Weighted toward thousands; hundreds and ten-thousands present so
    # the matcher sees genuine order-of-magnitude spread rather than
    # one narrow band.
    magnitude = rng.choice([2, 3, 3, 3, 4])

    low_paise = (10 ** magnitude) * 100
    high_paise = (10 ** (magnitude + 1)) * 100 - 1

    return money(Decimal(rng.randint(low_paise, high_paise)) / Decimal(100))


# =======================================================================
# RECORD BUILDERS
# =======================================================================

txn_counter = [0]


def build_clean_transaction(merchant, txn_id, date_offset_days):
    """Baseline: a fully correct, fully matchable transaction."""
    gross = _draw_gross()
    fee = money(gross * Decimal("0.02"))
    gst = money(fee * GST_RATE_ON_FEE)

    # Captured BEFORE this transaction is applied -- this is the
    # merchant's true starting point, written into the record so it is
    # real data rather than private generator state.
    opening_gross = merchant["annual_gross_so_far"]

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
    """
    Bank credits less than the expected net.

    Drift values are all far above AMOUNT_TOLERANCE (0.01), so these
    are genuine discrepancies rather than rounding noise. They remain
    fixed rather than scaled to gross: a flat INR 5 short-credit is a
    realistic operational error regardless of transaction size, and
    keeping it absolute means the check is exercised at both ends of
    the amount distribution.
    """
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
    """
    One source is dropped entirely.

    Both labels here are correct as written and were verified against
    the decision table:

        bank dropped    -> missing_bank_unmatched   -> UNMATCHED
        invoice dropped -> missing_invoice_...      -> PARTIAL_MATCH

    Note that UNMATCHED is reachable here via the dedicated
    `missing_bank` rule (priority 4), NOT via `no_candidates_found`
    (priority 0) -- the invoice is still present, so the transaction is
    not source-less. Two different rules, same status.
    """
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
    """
    The same settlement is credited twice in the bank feed.

    FIX (L1): expected_status was "AMBIGUOUS". The decision table maps
    `duplicate_detected` to HUMAN_REVIEW / DUPLICATE_DETECTED at
    priority 1 -- it has never produced AMBIGUOUS for a duplicate, and
    should not: a duplicate is not an ambiguity.

    Ambiguity means "two DIFFERENT plausible transactions compete for
    one record". Duplication means "one transaction appears twice".
    The operational response differs: ambiguity needs disambiguation,
    duplication needs one row reversed. Collapsing them into one status
    would lose that distinction for the finance operator.

    The exception code was already correct, so this divergence showed
    up as a status-only mismatch -- the clearest possible signal that
    the label, not the engine, was wrong.
    """
    pg, bank, invoice, gt = build_clean_transaction(merchant, txn_id, date_offset_days)
    gt["category"] = "duplicate"
    gt["expected_exception_code"] = "DUPLICATE_DETECTED"
    gt["expected_status"] = "HUMAN_REVIEW"
    gt["notes"] = ("Same transaction credited twice in the bank feed; "
                   "duplicate row injected at batch assembly. Routed to "
                   "human review for reversal, not treated as ambiguity.")
    return pg, bank, invoice, gt


def build_ambiguous(merchant, txn_id, date_offset_days):
    """First member of an ambiguous pair. Its sibling is injected at
    batch assembly by build_ambiguous_sibling(), which reuses this
    record's gross_amount and timestamp to create the collision."""
    pg, bank, invoice, gt = build_clean_transaction(merchant, txn_id, date_offset_days)
    gt["category"] = "ambiguous"
    gt["expected_exception_code"] = "AMBIGUOUS_MATCH"
    gt["expected_status"] = "AMBIGUOUS"
    gt["notes"] = "A sibling transaction shares the same amount and date; genuinely ambiguous without a stronger signal."
    return pg, bank, invoice, gt


def build_corrupted(merchant, txn_id, date_offset_days):
    """
    Malformed gross_amount; must be rejected at ingestion.

    UNMATCHED / CORRUPTED_RECORD is produced by the ingestion terminal
    path in run_e2e_deterministic.py, not by the decision table -- the
    record never reaches normalization, matching, or decisioning. That
    is why this label is correct despite UNMATCHED normally requiring
    no_candidates_found.
    """
    pg, bank, invoice, gt = build_clean_transaction(merchant, txn_id, date_offset_days)
    pg["gross_amount"] = "NOT_A_NUMBER"
    gt["category"] = "corrupted"
    gt["expected_exception_code"] = "CORRUPTED_RECORD"
    gt["expected_status"] = "UNMATCHED"
    gt["notes"] = "PG record has a malformed gross_amount field; must fail validation gracefully."
    return pg, bank, invoice, gt


def build_unresolvable(merchant, txn_id, date_offset_days):
    """
    Every individual signal is degraded simultaneously: the amount
    drifts by INR 50, the value date drifts by 9 days, and the UTR is
    nulled. The bank_ref linkage is deliberately LEFT INTACT.

    FIX (L2): expected_status was "UNMATCHED" / HUMAN_REVIEW_REQUIRED.

    That was self-contradictory. UNMATCHED requires
    `no_candidates_found`, which requires BOTH bank and invoice to be
    absent. This builder emits all three sources and keeps bank_ref
    resolvable, so a counterpart IS found -- the engine then correctly
    reports the INR 50 shortfall as AMOUNT_MISMATCH / HUMAN_REVIEW.

    The two statuses mean different things operationally:

        UNMATCHED       -- no counterpart exists; go find it
        AMOUNT_MISMATCH -- counterpart found, short by INR 50; go
                           reconcile it

    The second is strictly more actionable and is what actually
    happened. Widening UNMATCHED to cover found-but-discrepant records
    would make the status semantically meaningless and break the
    decision table's 512-combination coverage.

    NAMING NOTE: the category is called "unresolvable" but is, by
    construction, resolvable by identity -- only irreconcilable without
    a human. "degraded_signals" would be more accurate. Renaming is
    deferred to avoid churning case IDs mid-submission; recorded in
    FAILURE_LOG.md.
    """
    pg, bank, invoice, gt = build_clean_transaction(merchant, txn_id, date_offset_days)
    bank["credited_amount"] = str(money(Decimal(bank["credited_amount"]) - Decimal("50.00")))
    bank["value_date"] = (
        datetime.fromisoformat(pg["timestamp"]) + timedelta(days=9)
    ).date().isoformat()
    bank["utr"] = None
    gt["category"] = "unresolvable"
    gt["expected_exception_code"] = "AMOUNT_MISMATCH"
    gt["expected_status"] = "HUMAN_REVIEW"
    gt["notes"] = ("Amount, date, and UTR all diverge simultaneously "
                   "while bank_ref linkage survives; resolvable by "
                   "identity but not reconcilable without a human.")
    return pg, bank, invoice, gt


def build_ambiguous_sibling(merchant, txn_id, counterpart_pg, counterpart_bank):
    """
    Build a sibling PG/bank/invoice triplet that intentionally shares
    gross_amount and timestamp with its ambiguous counterpart -- that
    collision is what creates genuine ambiguity -- while remaining
    internally consistent on its own: fee/GST/TDS/net_payout/bank
    credited_amount/invoice amounts are all derived from the SAME
    gross_amount used for the collision, computed once, rather than
    generated for an unrelated amount and overwritten afterward.

    WHY THE BANK RECORD MATTERS MOST
    --------------------------------
    Ambiguity detection (find_bank_ambiguity_candidates) compares the
    anchor PG record's EXPECTED NET against candidate BANK amounts and
    dates. Copying only the PG-side gross/timestamp -- as an earlier
    version of this generator did -- produced ground truth asserting
    AMBIGUOUS while emitting no bank record the engine could ever
    detect as competing. Every unit test still passed, because
    "ambiguous results are never auto-matched" was never violated:
    nothing was ever flagged ambiguous in the first place. The result
    was six fail-open cases that only a full-batch regression harness
    surfaced.

    Both bank rows must therefore land on the same net amount and the
    same value_date. Callers must draw BOTH merchants from the
    zero-base cohort so TDS is zero on both sides (see
    pick_zero_base_merchant for why AMOUNT_TOLERANCE makes this
    mandatory).

    NOTE ON FIX (A1): this function copies gross rather than calling
    _draw_gross(), which is precisely why widening the amount
    distribution does not weaken the ambiguous category. Deliberate
    collision is constructed; only accidental collision was removed.
    """
    gross = Decimal(counterpart_pg["gross_amount"])
    fee = money(gross * Decimal("0.02"))
    gst = money(fee * GST_RATE_ON_FEE)

    opening_gross = merchant["annual_gross_so_far"]
    merchant["annual_gross_so_far"] += gross
    tds = (money(gross * TDS_RATE_SECTION_393)
           if merchant["annual_gross_so_far"] > TDS_ANNUAL_THRESHOLD
           else Decimal("0.00"))

    net = money(gross - fee - gst - tds)
    ts = datetime.fromisoformat(counterpart_pg["timestamp"])
    utr = f"UTR{rng.randint(100000000, 999999999)}"
    payment_method = rng.choice(PAYMENT_METHODS)
    narration_method = rng.choice(NARRATION_METHODS)

    pg = {
        "settlement_id": f"SET_{txn_id}",
        "txn_id": txn_id,
        "order_id": f"ORD_{txn_id}",
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
        # Explicitly mirror the counterpart's value_date rather than
        # recomputing ts + 1 day. Both are identical today, but pinning
        # it here means a future change to the counterpart's settlement
        # lag cannot silently break the date half of the collision.
        "value_date": counterpart_bank["value_date"],
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
        "expected_status": "AMBIGUOUS",
        "expected_exception_code": "AMBIGUOUS_MATCH",
        "category": "ambiguous",
        "notes": ("Sibling of an ambiguous pair; same net amount and "
                  "value date as its counterpart, internally consistent "
                  "on its own financial fields."),
    }
    return pg, bank, invoice, ground_truth


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
            if category == "ambiguous":
                # Both members of an ambiguous pair must withhold zero
                # TDS so their nets collide within AMOUNT_TOLERANCE.
                merchant = pick_zero_base_merchant()
            elif category in HIGH_VOLUME_BIAS_CATEGORIES and i % 3 == 0:
                # bias roughly 1-in-3 of certain categories toward the
                # near-threshold merchants so the TDS threshold actually
                # gets exercised somewhere in the batch
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

            if category == "ambiguous" and pg is not None and bank is not None:
                sibling_merchant = pick_zero_base_merchant()
                sibling_txn_id = next_txn_id(txn_counter)
                s_pg, s_bank, s_invoice, s_gt = build_ambiguous_sibling(
                    sibling_merchant, sibling_txn_id, pg, bank
                )
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

    # Self-check: every ambiguous pair must actually collide on net
    # amount and value date, otherwise the category is asserting a
    # condition the data does not contain.
    _verify_ambiguous_pairs_collide(ground_truth, pg_records)

    # Self-check: no ground-truth label may assert a status the
    # decision table cannot produce for the data as emitted.
    _verify_label_reachability(ground_truth)

    # FIX (A1) instrumentation: report how much ACCIDENTAL net
    # collision the amount distribution is producing.
    _report_accidental_net_collisions(ground_truth, pg_records)


def _verify_ambiguous_pairs_collide(ground_truth: list, pg_records: list):
    """Fail loudly at generation time if an ambiguous pair does not
    genuinely collide. Ground truth that asserts AMBIGUOUS without a
    detectable collision is worse than no ground truth at all: it makes
    a correct engine look broken, or hides a fail-open bug."""
    pg_by_id = {r["txn_id"]: r for r in pg_records}
    ambiguous_ids = [g["txn_id"] for g in ground_truth
                     if g["category"] == "ambiguous"]

    nets = {}
    for txn_id in ambiguous_ids:
        record = pg_by_id.get(txn_id)
        if record is None:
            continue
        key = (record["net_payout"], record["timestamp"])
        nets.setdefault(key, []).append(txn_id)

    orphans = [ids for ids in nets.values() if len(ids) < 2]
    if orphans:
        print("\n  WARNING: ambiguous records with no colliding sibling:")
        for ids in orphans:
            print(f"    {ids}")
    else:
        print(f"  ambiguous pairs verified colliding: {len(nets)}")


def _report_accidental_net_collisions(ground_truth: list, pg_records: list):
    """
    Count net-amount collisions OUTSIDE the ambiguous category.

    This is the metric FIX (A1) exists to move. Under the old six-value
    distribution this number was large -- roughly ten records shared
    each gross, so nearly every record collided with several others,
    and the fuzzy tier had no amount signal left to discriminate on.

    A small number here is healthy and realistic: real batches DO
    contain occasional equal amounts. A large number means the
    generator is manufacturing the difficulty it then measures.
    """
    category_by_id = {g["txn_id"]: g["category"] for g in ground_truth}

    nets: dict[str, list[str]] = {}
    for record in pg_records:
        if record["gross_amount"] == "NOT_A_NUMBER":
            continue
        if category_by_id.get(record["txn_id"]) == "ambiguous":
            continue          # deliberate collision, not accidental
        nets.setdefault(record["net_payout"], []).append(record["txn_id"])

    colliding = {net: ids for net, ids in nets.items() if len(ids) > 1}
    collided_records = sum(len(ids) for ids in colliding.values())

    print(f"  accidental net collisions (non-ambiguous): "
          f"{collided_records} record(s) across {len(colliding)} amount(s)")

    if collided_records > len(pg_records) // 4:
        print("    WARNING: heavy accidental collision -- the amount "
              "distribution may be too narrow to measure the fuzzy "
              "tier honestly.")


# The status each category is expected to produce, per the decision
# table. Kept here as the generator's own declaration of intent so a
# label change cannot silently drift from the policy it describes.
EXPECTED_STATUS_BY_CATEGORY = {
    "exact_match": "MATCHED",
    "timing_difference": "MATCHED",
    "reference_mismatch_fuzzy": "MATCHED",
    "amount_fee_discrepancy": "HUMAN_REVIEW",
    "tax_mismatch": "TAX_MISMATCH",
    "missing_in_source": {"UNMATCHED", "PARTIAL_MATCH"},
    "duplicate": "HUMAN_REVIEW",
    "ambiguous": "AMBIGUOUS",
    "corrupted": "UNMATCHED",
    "unresolvable": "HUMAN_REVIEW",
}


def _verify_label_reachability(ground_truth: list):
    """Fail loudly if a category emits a status outside its declared
    set. This is the guard that would have caught FIX (L1) and
    FIX (L2) at generation time rather than three harness runs later."""
    problems = []

    for entry in ground_truth:
        category = entry["category"]
        status = entry["expected_status"]
        allowed = EXPECTED_STATUS_BY_CATEGORY.get(category)

        if allowed is None:
            problems.append(f"{entry['txn_id']}: unknown category {category!r}")
            continue

        allowed_set = allowed if isinstance(allowed, set) else {allowed}

        if status not in allowed_set:
            problems.append(
                f"{entry['txn_id']} ({category}): expected_status "
                f"{status!r} not in {sorted(allowed_set)}"
            )

    if problems:
        print("\n  WARNING: ground-truth labels disagree with declared policy:")
        for problem in problems:
            print(f"    {problem}")
    else:
        print(f"  label reachability verified: {len(ground_truth)} entries")


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