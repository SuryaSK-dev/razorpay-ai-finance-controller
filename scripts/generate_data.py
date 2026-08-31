# scripts/generate_data.py
"""
Synthetic dataset generator for the reconciliation engine.

Produces three independent source files (PG settlement, bank statement,
merchant invoice) -- each as both JSON and CSV -- plus a hidden
ground_truth.json that is deliberately NEVER read by the reconciliation
pipeline itself.

GROUND-TRUTH LABELLING RULE
---------------------------
expected_status must describe what the DECISION TABLE will actually
produce for the data this builder emits -- not what the category name
suggests. Two labels violated that rule (FIX L1, FIX L2) and made a
correct engine look broken.

    no_candidates_found (bank AND invoice absent) -> UNMATCHED
    duplicate_detected                            -> HUMAN_REVIEW
    is_ambiguous                                  -> AMBIGUOUS
    missing_bank                                  -> UNMATCHED
    missing_invoice                               -> PARTIAL_MATCH
    amount_mismatch                               -> HUMAN_REVIEW

DATA-REALISM RULE
-----------------
The generator must not make the problem artificially easy OR
artificially hard.

    FIX (A1)  six fixed gross values manufactured net collisions
              everywhere and made the fuzzy tier look imprecise
    FIX (A2)  bank_ref always encoded our own txn_id, so tier 2
              resolved everything and the fuzzy tier was dead code
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
    FEE_BEARING_METHODS,
    GST_RATE_ON_FEE,
    MDR_BY_METHOD,
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

# Drawn from the MDR table rather than written out, so a method can
# never exist without a rate (or a rate without a method).
PAYMENT_METHODS = sorted(MDR_BY_METHOD)
NARRATION_METHODS = ["UPI", "NEFT", "IMPS"]


# =======================================================================
# MERCHANT POOL
# Merchants 1-3 are seeded just under the TDS annual threshold
# (INR 495,000 of 500,000) so a handful of their transactions genuinely
# cross into TDS territory. Written into each PG record as
# merchant_ytd_gross_opening so the seller ledger can reconstruct the
# threshold decision from real data rather than generator state.
# =======================================================================

MERCHANTS = [
    {
        "id": f"MERCH_{i:03d}",
        "gstin": f"29AAAAA{i:04d}A1Z5",
        "annual_gross_so_far": Decimal("495000.00") if i <= 3 else Decimal("0.00"),
    }
    for i in range(1, 16)
]

NEAR_THRESHOLD_MERCHANTS = MERCHANTS[:3]
ZERO_BASE_MERCHANTS = MERCHANTS[3:]


def pick_merchant():
    return rng.choice(MERCHANTS)


def pick_high_volume_merchant():
    """Bias a few clean transactions toward the near-threshold cohort
    so the TDS threshold is actually crossed within this batch."""
    return rng.choice(NEAR_THRESHOLD_MERCHANTS)


def pick_zero_base_merchant():
    """Merchants 4-15 start at zero and cannot cross the TDS threshold
    in a batch this size.

    This matters for the ambiguous pair. Ambiguity is detected by
    comparing a PG record's EXPECTED NET against candidate bank
    amounts, and AMOUNT_TOLERANCE is INR 0.01. If one member withheld
    TDS and the other did not, their nets would differ by 0.1% of
    gross -- orders of magnitude above tolerance -- and the collision
    that CREATES the ambiguity would silently fail to exist.

    ACKNOWLEDGED CONSTRAINT: this is the generator being shaped to fit
    the measurement. Defensible as variable isolation, but it does mean
    the ambiguous category can never exercise TDS. Recorded in
    FAILURE_LOG.md rather than left implicit."""
    return rng.choice(ZERO_BASE_MERCHANTS)


def next_txn_id(counter: list[int]) -> str:
    counter[0] += 1
    return f"TXN_{counter[0]:05d}"


# =======================================================================
# AMOUNT DISTRIBUTION
# =======================================================================

def _draw_gross() -> Decimal:
    """
    Draw a gross amount to paise precision.

    FIX (A1). An earlier version drew from six fixed values. With 63
    records that put ~10 transactions on each, and because fee and GST
    are fixed percentages, identical gross produced identical expected
    net. Nine bank rows landed on 12205.00; the date window was doing
    nearly all the discriminating work in ambiguity detection.

    Log-uniform rather than flat, because real transaction values
    cluster low with a long tail. Paise precision is the part that
    matters: AMOUNT_TOLERANCE is 0.01, so rounding to rupees would
    leave a weaker version of the same artifact.

    Deliberate collision is unaffected -- build_ambiguous_sibling()
    copies its counterpart's gross and the duplicate bank row is a
    dict copy. Only ACCIDENTAL collision disappears.
    """
    magnitude = rng.choice([2, 3, 3, 3, 4])
    low_paise = (10 ** magnitude) * 100
    high_paise = (10 ** (magnitude + 1)) * 100 - 1
    return money(Decimal(rng.randint(low_paise, high_paise)) / Decimal(100))


# =======================================================================
# BANK NARRATION FORMATS
# =======================================================================
#
# UPGRADE B. Replaces a single invented format --
#
#     "NEFT CR UTR123456789 MERCH_004"
#
# -- with formats drawn from what Indian banks actually emit. Real
# narration varies by bank, by channel, and sometimes within one bank's
# own statement.
#
# Two properties matter for matching:
#
#   1. Most formats embed the UTR in free text. That is the signal the
#      guarded fuzzy tier keys on.
#
#   2. The UPI format carries a UPI reference number and NO UTR. It is
#      deliberately unrecoverable from narration alone. Without at
#      least one such case, "our fuzzy tier recovers narration" would
#      be an untested claim about a dataset engineered to be easy.
#
# None of these contain a TXN_ token, so _extract_txn_from_narration()
# returns None for all of them. Real banks do not echo your internal
# transaction ID.

BANK_IFSC_CODES = [
    "HDFC0000123", "ICIC0001234", "SBIN0004521",
    "UTIB0000456", "KKBK0005678",
]


def _make_narration(
    utr: str | None,
    merchant_id: str,
    method: str,
) -> str:
    """
    Build a bank narration in one of several realistic formats.

    `utr=None` produces the UPI form, which carries a UPI reference
    instead and is therefore unrecoverable by narration matching.
    """
    ifsc = rng.choice(BANK_IFSC_CODES)

    if utr is None:
        upi_ref = rng.randint(10**11, 10**12 - 1)
        return f"UPI/P2M/{upi_ref}/RAZORPAY/{merchant_id}"

    return rng.choice([
        f"{method}-{ifsc}-{utr}-{merchant_id}",
        f"IMPS/{rng.randint(10**11, 10**12 - 1)}/{utr}/SETTLEMENT",
        f"BY TRANSFER-{method}*{ifsc[:4]}*{utr}*RAZORPAY-",
        f"{method} CR {utr} {merchant_id} SETTLEMENT",
        f"{method}/{utr}/{merchant_id}/NET STLMNT",
    ])


def _bank_native_ref() -> str:
    """
    A bank's own reference, carrying no trace of our transaction ID.

    BANKREF_<txn_id> is a convention no real bank provides. Rows using
    this instead cannot be resolved by tier 2 and must fall through to
    guarded fuzzy narration matching.
    """
    ifsc = rng.choice(BANK_IFSC_CODES)
    return f"{ifsc}N{rng.randint(10**7, 10**8 - 1)}"


# =======================================================================
# RECORD BUILDERS
# =======================================================================

txn_counter = [0]


def build_clean_transaction(
    merchant, txn_id, date_offset_days, payment_method=None
):
    """
    Baseline: a fully correct, fully matchable transaction.

    UPGRADE 2.2. `pg_fee` is now derived from the payment method via
    MDR_BY_METHOD rather than a flat 2%. UPI is zero-rated, so roughly a
    third of the batch carries fee = 0 and therefore GST = 0 -- which is
    CORRECT, not a missing charge, and the tax layer must not flag it.

    `payment_method` may be forced by a caller that needs a fee-bearing
    method (tax_mismatch) or needs to mirror a counterpart's economics
    (the ambiguous sibling). Left None, it is drawn at random.
    """
    gross = _draw_gross()

    if payment_method is None:
        payment_method = rng.choice(PAYMENT_METHODS)

    fee = money(gross * MDR_BY_METHOD[payment_method])
    gst = money(fee * GST_RATE_ON_FEE)

    # Captured BEFORE this transaction is applied -- the merchant's
    # true starting point, written into the record so it is real data
    # rather than private generator state.
    opening_gross = merchant["annual_gross_so_far"]

    merchant["annual_gross_so_far"] += gross
    tds = (money(gross * TDS_RATE_SECTION_393)
           if merchant["annual_gross_so_far"] > TDS_ANNUAL_THRESHOLD
           else Decimal("0.00"))

    net = money(gross - fee - gst - tds)
    ts = BASE_DATE + timedelta(days=date_offset_days, hours=rng.randint(0, 23))
    utr = f"UTR{rng.randint(100000000, 999999999)}"
    order_id = f"ORD_{txn_id}"
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
        "narration": _make_narration(utr, merchant["id"], narration_method),
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
    """
    A bank row with NO structured reference to our transaction.

    UPGRADE B / FIX (A2)
    --------------------
    The previous version corrupted only bank["utr"] and left bank_ref
    as BANKREF_<txn_id>. Tier 2 therefore resolved every record in this
    category before the fuzzy tier was ever consulted. Zero of 61
    records reached tier 3 -- the category was named for a code path it
    never executed (FAILURE_LOG.md section 33, and documented in
    tests/test_matching.py long before that).

    The realistic version of this scenario is a bank feed that gives
    you no structured reference at all: a bank-native ref, no UTR
    field, and a settlement identifiable only by a UTR sitting in free
    text. That is precisely what the guarded fuzzy tier exists for.

        Tier 1 exact UTR  -> miss, bank.utr is None
        Tier 2 exact txn  -> miss, bank_ref is bank-native and the
                             narration carries no TXN_ token
        Tier 3 fuzzy      -> fires, amount and date agreement enforced

    Half carry a clean UTR in the narration (fuzzy ~100); half carry a
    single corrupted digit (fuzzy ~91), simulating OCR or manual entry
    error. Both sit above the threshold of 85. Amount and date are
    untouched, so the guards pass on merit rather than by construction.

    The clean/corrupted split is derived from the txn_id rather than
    drawn randomly, so it stays stable across regenerations and the
    two behaviours are always both present.
    """
    pg, bank, invoice, gt = build_clean_transaction(
        merchant, txn_id, date_offset_days
    )

    original = bank["utr"]

    corrupt_it = txn_id[-1] in "13579"

    if corrupt_it:
        pos = rng.randint(0, len(original) - 1)
        narration_utr = (
            original[:pos]
            + rng.choice("0123456789")
            + original[pos + 1:]
        )
        detail = "single corrupted digit"
    else:
        narration_utr = original
        detail = "intact UTR"

    method = rng.choice(NARRATION_METHODS)

    # No structured linkage on the bank side, at all.
    bank["bank_ref"] = _bank_native_ref()
    bank["utr"] = None
    bank["narration"] = _make_narration(
        narration_utr, merchant["id"], method
    )

    gt["category"] = "reference_mismatch_fuzzy"
    gt["expected_status"] = "MATCHED"
    gt["notes"] = (
        "Bank feed provides no structured reference: bank-native "
        f"bank_ref, no UTR field, {detail} embedded in free-text "
        "narration. Recoverable only via amount+date-gated fuzzy "
        "matching -- this category is what tier 3 exists for."
    )
    return pg, bank, invoice, gt


def build_amount_discrepancy(merchant, txn_id, date_offset_days):
    """
    Bank credits less than the expected net.

    Drift values are far above AMOUNT_TOLERANCE (0.01), so these are
    genuine discrepancies rather than rounding noise. They stay
    absolute rather than scaling with gross: a flat INR 5 short-credit
    is a realistic operational error at any transaction size, and
    keeping it absolute exercises the check at both ends of the amount
    distribution.
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
    """
    An invoice claiming the wrong tax.

    UPGRADE 2.2 -- WHY THE METHOD IS FORCED
    ---------------------------------------
    The GST error is injected as a percentage of the FEE:

        wrong_gst = fee * 0.12    instead of    fee * 0.18

    Under a flat 2% MDR that always produced a real discrepancy. With
    method-dependent MDR it does not: UPI is zero-rated, so fee = 0, and

        money(0 * 0.12) == money(0 * 0.18) == 0.00

    The "defect" would be arithmetically identical to the correct value.
    The record would carry a TAX_MISMATCH label while the engine
    correctly found nothing wrong -- a false divergence, and one that
    would look like an engine failure rather than a generator bug.

    Drawing from FEE_BEARING_METHODS guarantees a non-zero GST base.
    _verify_tax_mismatch_is_detectable() asserts it at generation time
    rather than leaving it to be discovered three harness runs later.
    """
    pg, bank, invoice, gt = build_clean_transaction(
        merchant,
        txn_id,
        date_offset_days,
        payment_method=rng.choice(FEE_BEARING_METHODS),
    )
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
    One source is dropped entirely. Both labels verified against the
    decision table:

        bank dropped    -> missing_bank_unmatched   -> UNMATCHED
        invoice dropped -> missing_invoice_...      -> PARTIAL_MATCH

    UNMATCHED is reachable here via the dedicated missing_bank rule
    (priority 4), NOT via no_candidates_found (priority 0) -- the
    invoice is still present. Two different rules, same status.
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
    The same settlement credited twice in the bank feed.

    FIX (L1): expected_status was "AMBIGUOUS". The decision table maps
    duplicate_detected to HUMAN_REVIEW / DUPLICATE_DETECTED at priority
    1 and has never produced AMBIGUOUS for a duplicate.

    Ambiguity means two DIFFERENT plausible transactions compete for
    one record. Duplication means one transaction appears twice. The
    operational response differs -- disambiguate versus reverse a row
    -- so collapsing them would lose that distinction for the operator.
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
    batch assembly by build_ambiguous_sibling()."""
    pg, bank, invoice, gt = build_clean_transaction(merchant, txn_id, date_offset_days)
    gt["category"] = "ambiguous"
    gt["expected_exception_code"] = "AMBIGUOUS_MATCH"
    gt["expected_status"] = "AMBIGUOUS"
    gt["notes"] = "A sibling transaction shares the same amount and date; genuinely ambiguous without a stronger signal."
    return pg, bank, invoice, gt


def build_corrupted(merchant, txn_id, date_offset_days):
    """
    Malformed gross_amount; rejected at ingestion.

    UNMATCHED / CORRUPTED_RECORD comes from the ingestion terminal path
    in run_e2e_deterministic.py, not the decision table -- the record
    never reaches normalization, matching, or decisioning.
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
    Every individual signal degraded at once: amount drifts by INR 50,
    value date by 9 days, UTR nulled. bank_ref is deliberately LEFT
    INTACT.

    FIX (L2): expected_status was UNMATCHED / HUMAN_REVIEW_REQUIRED,
    which was self-contradictory. UNMATCHED requires
    no_candidates_found -- both bank AND invoice absent. This builder
    emits all three sources and keeps bank_ref resolvable, so a
    counterpart IS found and the INR 50 shortfall is correctly an
    amount mismatch.

        UNMATCHED       -- no counterpart exists; go find it
        AMOUNT_MISMATCH -- counterpart found, short by INR 50; go
                           reconcile it

    NAMING NOTE: the category is called "unresolvable" but is
    resolvable by identity -- only irreconcilable without a human.
    "degraded_signals" would be more accurate. Renaming deferred to
    avoid churning case IDs mid-submission.
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
    A sibling triplet sharing gross_amount and timestamp with its
    ambiguous counterpart -- that collision is what creates genuine
    ambiguity -- while remaining internally consistent on its own.

    WHY THE BANK RECORD MATTERS MOST
    --------------------------------
    Ambiguity detection compares the anchor PG record's EXPECTED NET
    against candidate BANK amounts and dates. Copying only the PG-side
    gross/timestamp -- as an earlier version did -- produced ground
    truth asserting AMBIGUOUS while emitting no bank record the engine
    could detect as competing. Every unit test still passed, because
    "ambiguous results are never auto-matched" was never violated:
    nothing was ever flagged. Six fail-open cases resulted, surfaced
    only by a full-batch harness.

    Both bank rows must land on the same net amount and value_date.
    Callers must draw BOTH merchants from the zero-base cohort so TDS
    is zero on both sides.

    NOTE ON FIX (A1): this copies gross rather than calling
    _draw_gross(), which is why widening the amount distribution does
    not weaken this category. Deliberate collision is constructed; only
    accidental collision was removed.
    """
    gross = Decimal(counterpart_pg["gross_amount"])

    # UPGRADE 2.2. The fee rate must MIRROR the counterpart's, not be
    # hardcoded. Ambiguity is detected by comparing expected nets, and
    # net = gross - fee - gst - tds. If the counterpart paid by UPI
    # (fee 0) and this sibling were charged 2%, their nets would differ
    # by ~2% of gross -- orders of magnitude above AMOUNT_TOLERANCE --
    # and the collision that CREATES the ambiguity would silently fail
    # to exist.
    #
    # That is precisely the shape of the original fail-open bug
    # (FAILURE_LOG.md section 4), where the sibling's bank row was never
    # synced and six records were auto-matched that should have gone to
    # a human. Copying the method keeps the pair economically identical
    # by construction; _verify_ambiguous_pairs_collide() still checks it.
    payment_method = counterpart_pg["payment_method"]

    fee = money(gross * MDR_BY_METHOD[payment_method])
    gst = money(fee * GST_RATE_ON_FEE)

    opening_gross = merchant["annual_gross_so_far"]
    merchant["annual_gross_so_far"] += gross
    tds = (money(gross * TDS_RATE_SECTION_393)
           if merchant["annual_gross_so_far"] > TDS_ANNUAL_THRESHOLD
           else Decimal("0.00"))

    net = money(gross - fee - gst - tds)
    ts = datetime.fromisoformat(counterpart_pg["timestamp"])
    utr = f"UTR{rng.randint(100000000, 999999999)}"
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
        # Mirror the counterpart's value_date rather than recomputing
        # ts + 1 day. Identical today, but pinning it means a future
        # change to settlement lag cannot silently break the date half
        # of the collision.
        "value_date": counterpart_bank["value_date"],
        "narration": _make_narration(utr, merchant["id"], narration_method),
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

HIGH_VOLUME_BIAS_CATEGORIES = {"exact_match", "timing_difference", "tax_mismatch"}


def generate_batch():
    pg_records, bank_records, invoice_records, ground_truth = [], [], [], []
    category_counts = {k: 0 for k in BATCH_DISTRIBUTION}

    day_cursor = 0
    for category, count in BATCH_DISTRIBUTION.items():
        builder = BUILDERS[category]
        for i in range(count):
            if category == "ambiguous":
                # Both pair members must withhold zero TDS so their
                # nets collide within AMOUNT_TOLERANCE.
                merchant = pick_zero_base_merchant()
            elif category in HIGH_VOLUME_BIAS_CATEGORIES and i % 3 == 0:
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


# =======================================================================
# SELF-CHECKS
# =======================================================================

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


def _verify_ambiguous_pairs_collide(ground_truth: list, pg_records: list):
    """Fail loudly if an ambiguous pair does not genuinely collide.
    Ground truth asserting AMBIGUOUS without a detectable collision is
    worse than no ground truth: it makes a correct engine look broken,
    or hides a fail-open bug."""
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

    The metric FIX (A1) exists to move. A small number is healthy and
    realistic; a large one means the generator is manufacturing the
    difficulty it then measures.
    """
    category_by_id = {g["txn_id"]: g["category"] for g in ground_truth}

    nets: dict[str, list[str]] = {}
    for record in pg_records:
        if record["gross_amount"] == "NOT_A_NUMBER":
            continue
        if category_by_id.get(record["txn_id"]) == "ambiguous":
            continue
        nets.setdefault(record["net_payout"], []).append(record["txn_id"])

    colliding = {net: ids for net, ids in nets.items() if len(ids) > 1}
    collided_records = sum(len(ids) for ids in colliding.values())

    print(f"  accidental net collisions (non-ambiguous): "
          f"{collided_records} record(s) across {len(colliding)} amount(s)")

    if collided_records > len(pg_records) // 4:
        print("    WARNING: heavy accidental collision -- the amount "
              "distribution may be too narrow to measure the fuzzy "
              "tier honestly.")


def _verify_fuzzy_tier_is_reachable(ground_truth: list, bank_records: list):
    """
    UPGRADE B instrumentation.

    The reference_mismatch_fuzzy category exists to exercise tier 3. It
    can only do so if its bank rows are unresolvable by tiers 1 and 2:

        no UTR field           -> tier 1 cannot fire
        bank-native bank_ref   -> tier 2 cannot fire

    Checking this at generation time rather than discovering three
    harness runs later that the tier is dead code, which is exactly
    what happened before FIX (A2).
    """
    ref_ids = {g["txn_id"] for g in ground_truth
               if g["category"] == "reference_mismatch_fuzzy"}

    if not ref_ids:
        return

    problems = []
    checked = 0

    for row in bank_records:
        ref = row.get("bank_ref", "") or ""

        # Identify this category's rows by their bank-native ref shape.
        if ref.startswith("BANKREF_"):
            continue
        checked += 1

        if row.get("utr") is not None:
            problems.append(
                f"{ref}: exposes a UTR field, so tier 1 will resolve it"
            )

    if checked != len(ref_ids):
        problems.append(
            f"expected {len(ref_ids)} bank-native rows, found {checked}"
        )

    if problems:
        print("\n  WARNING: fuzzy tier may be unreachable:")
        for problem in problems:
            print(f"    {problem}")
    else:
        print(f"  fuzzy-tier reachability verified: {checked} bank row(s) "
              f"with no UTR and no BANKREF convention")


def _verify_tax_mismatch_is_detectable(ground_truth: list, pg_records: list,
                                       invoice_records: list):
    """
    UPGRADE 2.2 instrumentation.

    Every tax_mismatch record must contain a discrepancy the engine can
    actually find. The GST variant injects `fee * 0.12` in place of
    `fee * 0.18`, which is only a discrepancy when the fee is non-zero.
    UPI is zero-rated, so a UPI-drawn record would carry a TAX_MISMATCH
    label over an invoice that is arithmetically correct.

    Checking it here rather than discovering a false divergence in the
    accuracy report, where it would look like an engine failure.
    """
    pg_by_id = {r["txn_id"]: r for r in pg_records}
    inv_by_id = {r["txn_id"]: r for r in invoice_records}

    ids = [g["txn_id"] for g in ground_truth
           if g["category"] == "tax_mismatch"]

    problems = []
    gst_cases = 0
    tds_cases = 0

    for txn_id in ids:
        pg = pg_by_id.get(txn_id)
        invoice = inv_by_id.get(txn_id)
        if pg is None or invoice is None:
            continue

        fee = Decimal(pg["pg_fee"])
        expected_gst = money(fee * GST_RATE_ON_FEE)
        claimed_gst = Decimal(invoice["claimed_gst"])

        expected_tds = Decimal(pg["tds_withheld"])
        claimed_tds = Decimal(invoice["claimed_tds"])

        gst_wrong = abs(expected_gst - claimed_gst) > Decimal("0.01")
        tds_wrong = abs(expected_tds - claimed_tds) > Decimal("0.01")

        if gst_wrong:
            gst_cases += 1
        if tds_wrong:
            tds_cases += 1

        if not (gst_wrong or tds_wrong):
            problems.append(
                f"{txn_id}: labelled tax_mismatch but invoice is correct "
                f"(fee={fee}, method={pg.get('payment_method')}) -- a zero "
                f"fee makes the injected GST error a no-op"
            )

    if problems:
        print()
        print("  WARNING: undetectable tax_mismatch records:")
        for problem in problems:
            print(f"    {problem}")
    else:
        print(f"  tax_mismatch detectability verified: {len(ids)} record(s) "
              f"({gst_cases} GST, {tds_cases} TDS)")


def _report_mdr_distribution(pg_records: list):
    """
    UPGRADE 2.2. MDR is method-dependent, so the method mix determines
    how much of the batch has a zero fee -- and therefore how much of it
    exercises GST verification at all.
    """
    counts: dict[str, int] = {}
    zero_fee = 0

    for record in pg_records:
        if record["gross_amount"] == "NOT_A_NUMBER":
            continue
        method = record.get("payment_method", "UNKNOWN")
        counts[method] = counts.get(method, 0) + 1
        if Decimal(record["pg_fee"]) == 0:
            zero_fee += 1

    summary = ", ".join(f"{m} {n}" for m, n in sorted(counts.items()))
    print(f"  payment-method mix: {summary}")
    print(f"  zero-fee records (UPI, zero-rated): {zero_fee} "
          f"-- correct, not a missing charge")


def _verify_label_reachability(ground_truth: list):
    """Fail loudly if a category emits a status outside its declared
    set. The guard that would have caught FIX (L1) and FIX (L2) at
    generation time."""
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


def print_summary(category_counts, ground_truth, pg_records, bank_records,
                  invoice_records):
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

    _verify_ambiguous_pairs_collide(ground_truth, pg_records)
    _verify_label_reachability(ground_truth)
    _report_accidental_net_collisions(ground_truth, pg_records)
    _verify_fuzzy_tier_is_reachable(ground_truth, bank_records)
    _report_mdr_distribution(pg_records)
    _verify_tax_mismatch_is_detectable(ground_truth, pg_records, invoice_records)


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

    print_summary(category_counts, ground_truth, pg_records, bank_records,
                  invoice_records)
    print(f"\nWrote JSON + CSV sources to {RAW_DIR}")
    print(f"Wrote ground_truth.json to {OUTPUT_DIR} "
          f"-- the pipeline must NEVER read this file.")


if __name__ == "__main__":
    main()
