# scripts/tune_fuzzy_threshold.py
"""
Fuzzy tier reachability and selection-accuracy measurement.

WHAT THE PREVIOUS VERSION MEASURED, AND WHY IT WAS WRONG
--------------------------------------------------------
The prior sweep computed, for every PG record:

    fuzzy_fired = best_fuzzy_score >= threshold
    FP          = (category != "reference_mismatch_fuzzy") and fuzzy_fired

That asks "did fuzzy fire?" -- not "did fuzzy pick the wrong record?".

The generator writes bank narration as:

    "NEFT CR UTR123456789 MERCH_001"

The narration EMBEDS THE UTR VERBATIM. So for every clean transaction,
fuzz.partial_ratio(pg.utr, narration) returns 100. It cleared every
threshold, and because its category was not reference_mismatch_fuzzy,
it was counted as a false positive.

Those 43 "false positives" were CORRECT MATCHES being scored as errors.

The diagnostic tell was in the output itself: FP stayed constant at 43
across thresholds 60 through 95. A real similarity-based false-positive
count must fall as the threshold rises. A flat count means the metric
was independent of the thing it claimed to sweep.

The reported precision of 0.12-0.13 was therefore never a property of
fuzzy matching. It was an artifact of the metric definition. It was
also, for a time, wrongly attributed to the amount distribution --
widening that distribution to paise precision drove accidental net
collisions to zero and did not move this number at all, which is what
finally isolated the real cause.

SECOND, MORE SERIOUS QUESTION
-----------------------------
find_bank_candidates() consults three tiers IN ORDER:

    1. exact UTR
    2. exact resolved txn_id (from bank_ref)
    3. guarded fuzzy narration

A record only reaches tier 3 if tiers 1 and 2 both miss.

build_reference_mismatch() corrupts ONLY bank["utr"]. It leaves
bank_ref as "BANKREF_<txn_id>" intact, so _extract_txn_from_bank_ref()
resolves the correct txn_id and TIER 2 MATCHES BEFORE FUZZY IS EVER
CONSULTED.

If that holds, the one category built to exercise the fuzzy tier never
reaches it, and every fuzzy number this project has ever reported was
measured on a code path production does not take. The old sweep could
not detect this because it ran fuzzy unconditionally on all 63 records
instead of only on those that fall through.

This script therefore measures TWO things:

    PART 1 -- Tier reachability.
        Which tier actually resolves each record, replicating
        find_bank_candidates()' exact order and conditions. Answers
        "is the fuzzy tier reachable at all?"

    PART 2 -- Selection accuracy among records that genuinely reach
        the fuzzy tier.
            TP = fuzzy selected the CORRECT bank record
            FP = fuzzy selected a WRONG bank record
            FN = fuzzy selected nothing, but a correct record existed
        Not "did it fire".

Run:
    python scripts/tune_fuzzy_threshold.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from rapidfuzz import fuzz

from src.ingestion.loader import load_batch
from src.normalization.engine import normalize_batch
from src.matching.candidates import CandidateIndex
from src.config import (
    AMOUNT_TOLERANCE,
    DATE_TOLERANCE_DAYS,
    FUZZY_MIN_SIMILARITY,
)


ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
GT_PATH = ROOT / "data" / "ground_truth.json"


# ======================================================================
# GUARDS -- must mirror candidates.py exactly
# ======================================================================

def _within_amount_tolerance(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= AMOUNT_TOLERANCE


def _within_date_window(pg_date, other_date) -> bool:
    return abs((other_date - pg_date).days) <= DATE_TOLERANCE_DAYS


def _pg_expected_net(pg) -> Decimal:
    fee = pg.fee if pg.fee is not None else Decimal("0")
    gst = pg.gst if pg.gst is not None else Decimal("0")
    tds = pg.tds if pg.tds is not None else Decimal("0")
    return pg.amount - fee - gst - tds


# ======================================================================
# PART 1 -- TIER REACHABILITY
# ======================================================================

def resolve_tier(pg, index: CandidateIndex) -> str:
    """
    Replicate find_bank_candidates()' tier order and return which tier
    WOULD resolve this record in production.

    This deliberately re-implements rather than calling the production
    function, because the production function returns the candidates
    and not a clean "which tier fired" signal at the threshold sweep
    granularity we need. The conditions below are copied verbatim from
    candidates.py lines 305-410 -- if that file changes, this must be
    updated in step.
    """
    # Tier 1: exact UTR
    if pg.utr and pg.utr in index.bank_by_utr:
        return "exact_utr"

    # Tier 2: exact resolved txn_id (from bank_ref)
    if pg.txn_id and pg.txn_id in index.bank_by_txn:
        return "exact_txn"

    # Tier 3: guarded fuzzy narration
    expected_net = _pg_expected_net(pg)

    for bank_record in index.bank_pool:
        if not _within_amount_tolerance(expected_net, bank_record.amount):
            continue
        if not _within_date_window(
            pg.date_utc.date(), bank_record.date_utc.date()
        ):
            continue

        similarity = fuzz.partial_ratio(
            str(pg.raw_ref.get("utr") or ""),
            str(bank_record.raw_ref.get("narration") or ""),
        )

        if similarity >= FUZZY_MIN_SIMILARITY:
            return "fuzzy"

    return "none"


def report_tier_reachability(pg_records, index, ground_truth) -> list:
    """
    Print which tier resolves each record, grouped by category, and
    return the list of records that genuinely reach the fuzzy tier.
    """
    print("=" * 72)
    print("PART 1 -- TIER REACHABILITY")
    print("=" * 72)
    print()
    print("Which tier actually resolves each record, in production order:")
    print("  1. exact UTR  ->  2. exact txn_id (bank_ref)  ->  3. fuzzy")
    print()

    by_category: dict[str, Counter] = {}
    fuzzy_records = []

    for pg in pg_records:
        category = ground_truth.get(pg.txn_id, {}).get("category", "unknown")
        tier = resolve_tier(pg, index)

        by_category.setdefault(category, Counter())[tier] += 1

        if tier == "fuzzy":
            fuzzy_records.append(pg)

    header = f"{'category':<28}{'exact_utr':<11}{'exact_txn':<11}{'fuzzy':<8}{'none':<6}"
    print(header)
    print("-" * len(header))

    for category in sorted(by_category):
        counts = by_category[category]
        print(
            f"{category:<28}"
            f"{counts['exact_utr']:<11}"
            f"{counts['exact_txn']:<11}"
            f"{counts['fuzzy']:<8}"
            f"{counts['none']:<6}"
        )

    total_fuzzy = len(fuzzy_records)

    print()
    print(f"Records that genuinely reach the fuzzy tier: {total_fuzzy}")

    # ------------------------------------------------------------------
    # The finding this script exists to surface.
    # ------------------------------------------------------------------
    ref_mismatch = by_category.get("reference_mismatch_fuzzy", Counter())

    if ref_mismatch and ref_mismatch["fuzzy"] == 0:
        print()
        print("  FINDING: the reference_mismatch_fuzzy category does NOT")
        print("  reach the fuzzy tier. Every record in it is resolved")
        print(f"  earlier -- exact_utr={ref_mismatch['exact_utr']}, "
              f"exact_txn={ref_mismatch['exact_txn']}.")
        print()
        print("  build_reference_mismatch() corrupts only bank['utr'] and")
        print("  leaves bank_ref intact, so tier 2 resolves the record")
        print("  before fuzzy is consulted. The category is named for a")
        print("  code path it never exercises.")
        print()
        print("  To genuinely exercise tier 3, the generator must ALSO")
        print("  break bank_ref for these records -- otherwise the fuzzy")
        print("  tier remains dead code on this dataset.")

    if total_fuzzy == 0:
        print()
        print("  The fuzzy tier is UNREACHABLE on the current dataset.")
        print("  Part 2 cannot produce a meaningful measurement; any")
        print("  precision figure would be vacuous rather than good.")

    print()
    return fuzzy_records


# ======================================================================
# PART 2 -- SELECTION ACCURACY
# ======================================================================

def sweep_selection_accuracy(fuzzy_records, index, ground_truth) -> None:
    """
    Among records that genuinely reach the fuzzy tier, does it select
    the CORRECT bank record?

        TP -- fuzzy selected the correct bank record
        FP -- fuzzy selected a bank record belonging to another txn
        FN -- fuzzy selected nothing, though a correct record existed
              within the guards

    "Fired on a record that did not need fuzzy" is NOT a false
    positive. That was the first version's error: it penalised the tier
    for scoring highly on records whose UTR genuinely appears in the
    narration (FAILURE_LOG.md section 32).

    TRUTH LINKAGE -- SECOND CORRECTION
    ----------------------------------
    The second version scored correctness as:

        best_record.txn_id == pg.txn_id

    which is broken for exactly the records this sweep now evaluates.
    UPGRADE B strips both the bank_ref convention and any TXN_ token
    from the narration of the reference_mismatch_fuzzy category --
    that removal is what makes the tier reachable at all. So
    `bank_record.txn_id` is None BY CONSTRUCTION for every record in
    the sweep, `None == "TXN_00025"` is False, and every correct
    selection was counted as a false positive:

        TP = 0, FP = 6, Recall = nan

    The metric was identifying ground truth by the exact field the
    category exists to remove. Same defect class as before: a
    measurement that cannot observe the thing it claims to measure.

    Exact net equality is a valid substitute HERE ONLY BECAUSE
    _report_accidental_net_collisions() in generate_data.py measures
    zero net collisions outside the ambiguous category. If that number
    ever rises, this linkage stops being unique and must change --
    which is why the check is a named function with the dependency
    written down rather than an inline comparison.
    """
    print("=" * 72)
    print("PART 2 -- SELECTION ACCURACY AMONG RECORDS THAT REACH FUZZY")
    print("=" * 72)
    print()

    if not fuzzy_records:
        print("No records reach the fuzzy tier. Nothing to sweep.")
        print()
        print("This is a REAL result, not a missing measurement: a tier")
        print("that never executes has no precision. Reporting a number")
        print("here would be reporting noise.")
        return

    print(f"{'Threshold':<12}{'TP':<6}{'FP':<6}{'FN':<6}"
          f"{'Precision':<12}{'Recall':<10}")
    print("-" * 56)

    for threshold in range(60, 100, 5):
        tp = fp = fn = 0

        for pg in fuzzy_records:
            expected_net = _pg_expected_net(pg)

            def is_correct(bank_record) -> bool:
                """
                Does this bank row belong to the anchor transaction?

                Prefers the resolved txn_id when one exists. Falls back
                to exact net equality for the reference_mismatch_fuzzy
                rows, whose txn_id is deliberately None -- see the
                docstring above for why that fallback is sound on this
                dataset and what would invalidate it.
                """
                if bank_record.txn_id is not None:
                    return bank_record.txn_id == pg.txn_id
                return bank_record.amount == expected_net

            best_record = None
            best_score = -1.0
            correct_exists = False

            for bank_record in index.bank_pool:
                if not _within_amount_tolerance(
                    expected_net, bank_record.amount
                ):
                    continue
                if not _within_date_window(
                    pg.date_utc.date(), bank_record.date_utc.date()
                ):
                    continue

                if is_correct(bank_record):
                    correct_exists = True

                similarity = fuzz.partial_ratio(
                    str(pg.raw_ref.get("utr") or ""),
                    str(bank_record.raw_ref.get("narration") or ""),
                )

                if similarity >= threshold and similarity > best_score:
                    best_score = similarity
                    best_record = bank_record

            if best_record is None:
                if correct_exists:
                    fn += 1
                # else: nothing to find, nothing selected -- not an error
            elif is_correct(best_record):
                tp += 1
            else:
                fp += 1

        precision = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
        recall = tp / (tp + fn) if (tp + fn) > 0 else float("nan")

        print(f"{threshold:<12}{tp:<6}{fp:<6}{fn:<6}"
              f"{precision:<12.2f}{recall:<10.2f}")

    print()
    print(f"Evaluated on {len(fuzzy_records)} record(s) that fall through")
    print("tiers 1 and 2. TP/FP are SELECTION outcomes -- whether the")
    print("chosen bank record was the right one -- not whether the tier")
    print("fired.")
    print()
    print("CAVEAT ON WHAT THIS MEASURES")
    print("-" * 56)
    print("Accidental net collisions in this dataset are zero, so any")
    print("candidate surviving the amount and date guards is already")
    print("the correct one. The fuzzy similarity score is therefore")
    print("doing no discriminating work here -- it is a formality on")
    print("top of a guard that has already decided.")
    print()
    print("A high precision below should be read as 'the guards are")
    print("selective on this dataset', not as 'narration matching")
    print("works'. Making narration load-bearing would require amount")
    print("collisions INSIDE the guard window, which is a deliberate")
    print("dataset change rather than a fix.")


# ======================================================================
# MAIN
# ======================================================================

def main() -> None:
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)

    with GT_PATH.open("r", encoding="utf-8") as handle:
        ground_truth = {g["txn_id"]: g for g in json.load(handle)}

    pg_records = [r for r in normalized.records if r.source == "pg"]
    bank_records = [r for r in normalized.records if r.source == "bank"]
    invoice_records = [r for r in normalized.records if r.source == "invoice"]

    index = CandidateIndex(bank_records, invoice_records)

    print()
    print(f"PG records: {len(pg_records)}  |  "
          f"Bank records: {len(bank_records)}  |  "
          f"Production threshold: {FUZZY_MIN_SIMILARITY}")
    print()

    fuzzy_records = report_tier_reachability(
        pg_records, index, ground_truth
    )

    sweep_selection_accuracy(
        fuzzy_records, index, ground_truth
    )

    print()
    print("=" * 72)
    print("SCOPE")
    print("=" * 72)
    print("Self-generated data. These numbers characterise THIS dataset")
    print("and this narration format. They do not demonstrate")
    print("generalisation to real bank narration, which remains")
    print("unvalidated and is documented as such in FAILURE_LOG.md.")


if __name__ == "__main__":
    main()