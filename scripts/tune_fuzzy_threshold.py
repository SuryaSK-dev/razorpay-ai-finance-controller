# scripts/tune_fuzzy_threshold.py
"""
Sweeps the fuzzy-match threshold against the ACTUAL guarded matcher
behavior -- amount + date agreement required before fuzzy similarity
is even computed, exactly as find_bank_candidates() enforces in
production. The previous version of this script computed raw fuzzy
similarity across every PG/bank pair unconditionally, which measured
a different, unguarded algorithm than what actually ships -- that
was a real bug in the benchmark, not evidence the production matcher
is unreliable.
"""

from __future__ import annotations
import sys
import json
from pathlib import Path
from decimal import Decimal

sys.path.append(str(Path(__file__).resolve().parent.parent))

from rapidfuzz import fuzz
from src.ingestion.loader import load_batch
from src.normalization.engine import normalize_batch
from src.config import AMOUNT_TOLERANCE, DATE_TOLERANCE_DAYS

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
GT_PATH = Path(__file__).resolve().parent.parent / "data" / "ground_truth.json"


def _within_amount_tolerance(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= AMOUNT_TOLERANCE


def _within_date_window(pg_date, other_date) -> bool:
    return abs((other_date - pg_date).days) <= DATE_TOLERANCE_DAYS


def _pg_expected_net(pg) -> Decimal:
    fee = pg.fee if pg.fee is not None else Decimal("0")
    gst = pg.gst if pg.gst is not None else Decimal("0")
    tds = pg.tds if pg.tds is not None else Decimal("0")
    return pg.amount - fee - gst - tds


def main():
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    ground_truth = {g["txn_id"]: g for g in json.load(open(GT_PATH))}

    pg_records = [r for r in normalized.records if r.source == "pg"]
    bank_records = [r for r in normalized.records if r.source == "bank"]

    print("Fuzzy threshold sweep -- measured against the ACTUAL guarded")
    print("matcher (amount + date agreement required before fuzzy scoring).\n")
    print(f"{'Threshold':<12}{'TP':<6}{'FP':<6}{'FN':<6}{'Precision':<12}{'Recall':<10}")
    print("-" * 56)

    for threshold in range(60, 100, 5):
        tp = fp = fn = 0
        for pg in pg_records:
            gt = ground_truth.get(pg.txn_id, {})
            should_match_fuzzy = gt.get("category") == "reference_mismatch_fuzzy"

            # Only consider bank candidates that pass the SAME guard
            # the real matcher enforces -- this is the fix.
            guarded_candidates = [
                b for b in bank_records
                if _within_amount_tolerance(_pg_expected_net(pg), b.amount)
                and _within_date_window(pg.date_utc.date(), b.date_utc.date())
            ]

            best_score = 0
            for b in guarded_candidates:
                pg_ref = str(pg.raw_ref.get("utr") or "")
                narration = str(b.raw_ref.get("narration") or "")
                score = fuzz.partial_ratio(pg_ref, narration)
                best_score = max(best_score, score)

            fuzzy_fired = best_score >= threshold
            if should_match_fuzzy and fuzzy_fired:
                tp += 1
            elif not should_match_fuzzy and fuzzy_fired:
                fp += 1
            elif should_match_fuzzy and not fuzzy_fired:
                fn += 1

        precision = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
        recall = tp / (tp + fn) if (tp + fn) > 0 else float("nan")
        print(f"{threshold:<12}{tp:<6}{fp:<6}{fn:<6}{precision:<12.2f}{recall:<10.2f}")

    print("\nHonest caveat: this sweep is against our OWN reference_mismatch_fuzzy")
    print("category (6 records) on self-generated data. Real, defensible for THIS")
    print("dataset -- does not prove generalization to unseen narration formats.")


if __name__ == "__main__":
    main()