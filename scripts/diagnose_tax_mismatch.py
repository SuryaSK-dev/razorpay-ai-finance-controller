# scripts/diagnose_tax_mismatch.py
"""
One-off diagnostic: prints exactly why each TAX_MISMATCH decision
was flagged, cross-referenced against ground truth, so we can see
whether the 17 are genuinely 7 real tax_mismatch-category records
plus 10 false positives, or something else entirely.
"""

import sys
import json
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.ingestion.loader import load_batch
from src.normalization.engine import normalize_batch
from src.matching.engine import run_matching
from src.exceptions.manager import decide_batch
from src.models import DecisionStatus

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
GT_PATH = Path(__file__).resolve().parent.parent / "data" / "ground_truth.json"

batch = load_batch(RAW_DIR)
normalized = normalize_batch(batch)
match_results = run_matching(normalized.records)
decisions = decide_batch(match_results)

ground_truth = {g["txn_id"]: g for g in json.load(open(GT_PATH))}

tax_mismatch_decisions = [d for d in decisions if d.status == DecisionStatus.TAX_MISMATCH]

print(f"Total TAX_MISMATCH decisions: {len(tax_mismatch_decisions)}\n")

correctly_flagged = 0
false_positives = 0

for d in tax_mismatch_decisions:
    gt = ground_truth.get(d.txn_id, {})
    gt_category = gt.get("category", "UNKNOWN")
    gt_status = gt.get("expected_status", "UNKNOWN")
    if d.txn_id in ("TXN_00004", "TXN_00010"):
        matched = [r for r in match_results if r.txn_id == d.txn_id][0]
        print(f"   invoice_record present: {matched.invoice_record is not None}")
        if matched.invoice_record:
            print(f"   invoice raw_ref claimed_tds: {matched.invoice_record.raw_ref.get('claimed_tds')}")
            print(f"   invoice raw_ref txn_id: {matched.invoice_record.raw_ref.get('txn_id')}")
        print(f"   pg raw_ref tds_withheld: {matched.pg_record.raw_ref.get('tds_withheld')}")
    is_correct = gt_category == "tax_mismatch"
    if is_correct:
        correctly_flagged += 1
    else:
        false_positives += 1

    print(f"{d.txn_id}: gt_category={gt_category:<25} gt_expected={gt_status:<15} "
          f"{'[CORRECT]' if is_correct else '[FALSE POSITIVE]'}")
    print(f"   evidence: {d.evidence.get('tax_signals', {})}")
    print()

print(f"\nCorrectly flagged (true tax_mismatch category): {correctly_flagged}")
print(f"False positives (wrong category): {false_positives}")