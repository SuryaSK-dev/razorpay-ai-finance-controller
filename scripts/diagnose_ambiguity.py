# scripts/diagnose_ambiguity.py
"""
Pinpoint why ambiguity detection is (or is not) firing.

The ambiguous category asserts AMBIGUOUS in ground truth. The engine
returns MATCHED. The data has been verified to contain a genuine
amount+date collision, so the failure is now somewhere between
candidate generation and the is_ambiguous flag.

This script replays the exact guards inside
find_bank_ambiguity_candidates() for every ambiguous-category record
and prints which guard rejects each competing bank row -- so the
failing condition is observed rather than guessed.

Run:
    python scripts/diagnose_ambiguity.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from src.ingestion.loader import load_batch
from src.normalization.engine import normalize_batch
from src.matching.candidates import (
    CandidateIndex,
    generate_candidate_sets,
    find_bank_ambiguity_candidates,
    _pg_expected_net,
    _within_amount_tolerance,
    _within_date_window,
)
from src.matching.engine import _candidate_set_is_ambiguous


GT_PATH = ROOT / "data" / "ground_truth.json"
RAW_DIR = ROOT / "data" / "raw"


def main() -> None:
    ground_truth = {
        g["txn_id"]: g
        for g in json.load(open(GT_PATH, encoding="utf-8"))
    }

    ambiguous_ids = {
        txn_id
        for txn_id, g in ground_truth.items()
        if g.get("category") == "ambiguous"
    }

    print(f"Ambiguous-category txn_ids in ground truth: {len(ambiguous_ids)}")
    print(sorted(ambiguous_ids))
    print()

    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    records = normalized.records

    bank_pool = [r for r in records if r.source == "bank"]
    invoice_pool = [r for r in records if r.source == "invoice"]
    pg_pool = [r for r in records if r.source == "pg"]

    print(f"pool sizes -- pg:{len(pg_pool)} bank:{len(bank_pool)} "
          f"invoice:{len(invoice_pool)}")
    print()

    index = CandidateIndex(bank_pool, invoice_pool)

    # ------------------------------------------------------------------
    # Per-record guard replay
    # ------------------------------------------------------------------
    for pg_record in pg_pool:
        if pg_record.txn_id not in ambiguous_ids:
            continue

        expected_net = _pg_expected_net(pg_record)

        print("=" * 70)
        print(f"PG {pg_record.txn_id}")
        print(f"  gross(amount) : {pg_record.amount}")
        print(f"  fee/gst/tds   : {pg_record.fee} / {pg_record.gst} / {pg_record.tds}")
        print(f"  expected_net  : {expected_net}")
        print(f"  date          : {pg_record.date_utc.date()}")

        # Which bank rows even come close?
        near = []
        for bank_record in bank_pool:
            amount_ok = _within_amount_tolerance(expected_net, bank_record.amount)
            date_ok = _within_date_window(
                pg_record.date_utc.date(),
                bank_record.date_utc.date(),
            )
            same_txn = (
                pg_record.txn_id
                and bank_record.txn_id
                and bank_record.txn_id == pg_record.txn_id
            )
            if amount_ok or (date_ok and bank_record.amount == expected_net):
                near.append((bank_record, amount_ok, date_ok, same_txn))

        print(f"  bank rows with amount within tolerance: {len(near)}")
        for bank_record, amount_ok, date_ok, same_txn in near:
            verdict = (
                "SKIPPED:same_txn_id" if same_txn
                else "AMBIGUITY_EVIDENCE" if (amount_ok and date_ok)
                else f"REJECTED amount_ok={amount_ok} date_ok={date_ok}"
            )
            print(f"    bank txn={bank_record.txn_id!s:<14} "
                  f"amt={bank_record.amount!s:<12} "
                  f"date={bank_record.date_utc.date()} -> {verdict}")

        print()

    # ------------------------------------------------------------------
    # What the real pipeline concludes
    # ------------------------------------------------------------------
    print("=" * 70)
    print("CANDIDATE SET / is_ambiguous AS THE REAL PIPELINE COMPUTES IT")
    print("=" * 70)

    candidate_sets = generate_candidate_sets(records)

    for candidate_set in candidate_sets:
        txn_id = candidate_set.pg_record.txn_id
        if txn_id not in ambiguous_ids:
            continue

        bank_amb = getattr(candidate_set, "bank_ambiguity_candidates", [])
        inv_amb = getattr(candidate_set, "invoice_ambiguity_candidates", [])

        flag = _candidate_set_is_ambiguous(candidate_set, False)

        print(f"{txn_id}: "
              f"bank_candidates={len(candidate_set.bank_candidates)} "
              f"invoice_candidates={len(candidate_set.invoice_candidates)} "
              f"bank_ambiguity={len(bank_amb)} "
              f"invoice_ambiguity={len(inv_amb)} "
              f"-> is_ambiguous={flag}")

        # Direct call, in case the stored field diverges from a fresh call
        fresh, evidence = find_bank_ambiguity_candidates(
            candidate_set.pg_record,
            index,
            candidate_set.bank_candidates,
        )
        print(f"    fresh find_bank_ambiguity_candidates -> {len(fresh)} "
              f"({evidence.get('reason', '')[:50]})")


if __name__ == "__main__":
    main()