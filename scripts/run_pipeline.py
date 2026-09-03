# scripts/run_pipeline.py
"""
The deterministic pipeline, end to end, with no model involved.

WHY THIS EXISTS
---------------
`demo_agent.py` shows the agent. This shows what the agent is standing
on. They answer different questions, and a reviewer usually wants this
one first:

    "Before I look at the AI, does the reconciliation actually work?"

Everything printed below is produced by Phases 0-4. No API key, no
network, no language model — if this file imported one, the separation
it exists to demonstrate would not be real.

WHAT IT PRINTS
--------------
    1. Ingestion       what loaded, what was rejected, and why
    2. Normalization   how bank rows were linked to transactions
    3. Matching        which tier resolved each record, confidence mix
    4. Decisions       the status table and the full exception list
    5. Cash position   the same batch in rupees rather than record counts

The exception list is COMPLETE. Track 04 warns that one cherry-picked
match proves nothing, so nothing here is sampled or truncated.

RUNNING IT
----------
    python scripts/run_pipeline.py
    python scripts/run_pipeline.py --json     machine-readable summary
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from src.exceptions.manager import decide_batch
from src.financial import settlement_expected_net
from src.ingestion.loader import load_batch
from src.matching.completeness import account_for_bank_rows
from src.matching.engine import run_matching, summarize
from src.models import DecisionStatus
from src.normalization.engine import normalize_batch

# The status -> money-bucket mapping is IMPORTED, not restated.
#
# Writing a second copy here is how expected_net came to exist in four
# places (FAILURE_LOG.md section 52): two definitions that agree today
# and nothing keeping them agreeing. This constant is a plain dict of
# enum -> string and pulls in no model code, so importing it costs this
# script nothing it claims not to have.
#
# test_run_pipeline_agrees_with_the_cash_position_tool asserts the two
# produce identical totals over the real batch.
from src.agent.tools.query_tools import CASH_BUCKET_BY_STATUS

RAW_DIR = ROOT / "data" / "raw"

RULE = "=" * 74
THIN = "-" * 74


# Ordered most-resolved to least, so the table reads top-down as
# "how much of this batch is finished".
STATUS_ORDER = [
    DecisionStatus.MATCHED,
    DecisionStatus.PARTIAL_MATCH,
    DecisionStatus.TAX_MISMATCH,
    DecisionStatus.AMBIGUOUS,
    DecisionStatus.HUMAN_REVIEW,
    DecisionStatus.UNMATCHED,
]

STATUS_MEANING = {
    DecisionStatus.MATCHED: "clean across all three sources, tax verified",
    DecisionStatus.PARTIAL_MATCH: "invoice missing — tax could not be verified",
    DecisionStatus.TAX_MISMATCH: "GST or TDS variance against statute",
    DecisionStatus.AMBIGUOUS: "a competing record exists; no safe choice",
    DecisionStatus.HUMAN_REVIEW: "amount, duplicate, or fuzzy-only linkage",
    DecisionStatus.UNMATCHED: "bank row absent, or rejected at ingestion",
}


def _rupees(value: Decimal) -> str:
    return f"{value:,.2f}"


# ======================================================================
# STAGES
# ======================================================================

def stage_ingestion(batch) -> None:
    print(RULE)
    print("1. INGESTION")
    print(RULE)
    print()
    print(f"  {batch.summary()}".replace("\n", "\n  "))
    print()

    if batch.total_errors:
        print(f"  {batch.total_errors} record(s) rejected. They are reported,")
        print("  not dropped — a malformed row must not silently reduce the")
        print("  denominator every later percentage is measured against.")
        print()
        for result in (batch.pg, batch.bank, batch.invoice):
            for error in result.errors:
                offending = {
                    k: v for k, v in error.raw_record.items()
                    if k in ("txn_id", "gross_amount", "credited_amount")
                }
                print(f"    [{error.source}#{error.index}] {error.error_code}")
                print(f"       {offending}")
        print()


def stage_normalization(normalized) -> None:
    report = normalized.report
    print(RULE)
    print("2. NORMALIZATION")
    print(RULE)
    print()
    print(f"  {report.summary()}".replace("\n", "\n  "))
    print()
    print("  Bank rows carry no txn_id of their own. Resolution is tried in")
    print("  order — structured bank_ref, then a regex over free-text")
    print("  narration, then None. Never a sentinel string: a value like")
    print('  "UNRESOLVED" looks like a real identifier and could pass an')
    print("  equality check downstream.")
    print()


def stage_matching(match_results) -> None:
    summary = summarize(match_results)
    tiers = Counter(r.bank_match_type for r in match_results)

    print(RULE)
    print("3. MATCHING")
    print(RULE)
    print()
    print("  Bank candidates by tier (strongest evidence first):")
    print(f"    tier 1  exact UTR         {tiers.get('exact_utr', 0)}")
    print(f"    tier 2  exact txn_id      {tiers.get('exact_txn', 0)}")
    print(f"    tier 3  guarded fuzzy     {tiers.get('fuzzy', 0)}")
    print(f"            no candidate      {tiers.get('none', 0)}")
    print()
    print("  Amount and date are GATES on tier 3, not signals — a record")
    print("  failing either is discarded before similarity is computed.")
    print("  Similarity alone can never authorise a match.")
    print()
    print(f"  {summary.report()}".replace("\n", "\n  "))
    print()


def stage_completeness(report) -> None:
    """
    Every ingested bank row, accounted for.

    Printed the way ingestion rejections are printed, and for the same
    reason: a row nothing claims must be reported rather than dropped.
    Reconciliation is PG-anchored, so before this existed a bank credit
    that no settlement claimed appeared in no decision and no exception.
    """
    print(RULE)
    print("3b. BANK-SIDE COMPLETENESS")
    print(RULE)
    print()
    print(f"  {report.total_bank_rows} bank rows ingested. Every one is")
    print("  accounted for:")
    print()
    print(f"    selected into a match     {len(report.selected):>3}")
    print(f"    duplicate credit          {len(report.duplicate_credits):>3}"
          "   already reported as DUPLICATE_DETECTED")
    print(f"    claimed by nothing        {len(report.orphaned):>3}")
    print()

    if not report.orphaned:
        print("  Nothing unclaimed. Reconciliation is PG-anchored, so this")
        print("  is a property worth asserting rather than assuming.")
        print()
        return

    print(f"  {len(report.orphaned)} bank row(s) that no settlement claims —")
    print(f"  INR {report.orphaned_value:,.2f} the bank moved and this batch")
    print("  cannot explain. Reported here, not folded into the cash")
    print("  position, because it is not part of the 61-record settlement")
    print("  expectation those buckets measure.")
    print()

    for account in report.orphaned:
        print(f"    [{account.bank_ref}] {account.amount:>10}")
        print(f"       resolves to {account.resolved_txn_id}, which has no")
        print("       PG record in this batch")

    print()


def stage_decisions(decisions) -> None:
    counts = Counter(d.status for d in decisions)
    total = len(decisions)

    print(RULE)
    print("4. DECISIONS")
    print(RULE)
    print()
    print(f"  {'Status':<16}{'Count':>7}   Meaning")
    print(f"  {THIN[:70]}")
    for status in STATUS_ORDER:
        print(f"  {status.value:<16}{counts.get(status, 0):>7}   "
              f"{STATUS_MEANING[status]}")
    print(f"  {THIN[:70]}")
    print(f"  {'TOTAL':<16}{total:>7}   every record, unfiltered")
    print()

    matched = counts.get(DecisionStatus.MATCHED, 0)
    rate = round(100.0 * matched / total, 2) if total else 0.0
    print(f"  Match rate: {matched}/{total} ({rate}%)")
    print()
    print("  The dataset is deliberately adversarial — ten anomaly")
    print("  categories, 18 clean records by construction. A HIGH match")
    print("  rate here would mean the exceptions were not being caught.")
    print()


def stage_exceptions(decisions) -> None:
    unresolved = [d for d in decisions if d.status != DecisionStatus.MATCHED]

    print(RULE)
    print(f"   THE COMPLETE EXCEPTION LIST — {len(unresolved)} records")
    print(RULE)
    print()
    print("  Not a sample. Track 04: 'one cherry-picked match proves")
    print("  nothing.' Every unresolved record, with the rule that fired.")
    print()
    print(f"  {'txn_id':<12}{'status':<15}{'exception':<22}{'conf':>5}  rule")
    print(f"  {THIN[:72]}")

    for decision in sorted(unresolved, key=lambda d: d.txn_id):
        rule = (decision.evidence or {}).get("matched_rule", "-")
        print(f"  {decision.txn_id:<12}{decision.status.value:<15}"
              f"{decision.exception_code.value:<22}"
              f"{decision.confidence_score:>5}  {rule}")

    print()
    print("  Status classifies; reason codes explain. A record can carry")
    print("  more than one violation — the table picks the primary, and")
    print("  reason_codes preserves the complete set:")
    print()

    multi = [
        d for d in sorted(unresolved, key=lambda d: d.txn_id)
        if len(d.reason_codes) > 1
    ]
    for decision in multi[:5]:
        codes = ", ".join(c.value for c in decision.reason_codes)
        print(f"    {decision.txn_id}  [{codes}]")
    if len(multi) > 5:
        print(f"    ... and {len(multi) - 5} more with multiple violations")
    if not multi:
        print("    (no record in this batch carries more than one violation)")
    print()


def stage_cash(match_results, decisions, rejected: int) -> dict:
    """
    The batch in rupees.

    Record counts and value tell different stories. Twenty clean small
    settlements and one blocked large one reads as "20 matched, 1
    blocked" either way — but the cash position says which one the
    finance controller should care about.
    """
    by_txn = {d.txn_id: d for d in decisions}
    buckets = {
        "settled and verified": Decimal("0"),
        "awaiting verification": Decimal("0"),
        "blocked in exceptions": Decimal("0"),
        "not yet credited": Decimal("0"),
    }
    counts = {k: 0 for k in buckets}

    # Display labels for the shared bucket keys. Only the wording lives
    # here; which status lands in which bucket is imported.
    label = {
        "settled_and_verified": "settled and verified",
        "awaiting_verification": "awaiting verification",
        "blocked_in_exceptions": "blocked in exceptions",
        "not_yet_credited": "not yet credited",
    }

    credited = Decimal("0")
    for result in match_results:
        bucket = label[CASH_BUCKET_BY_STATUS[by_txn[result.txn_id].status]]
        buckets[bucket] += settlement_expected_net(result.pg_record)
        counts[bucket] += 1
        if result.bank_record is not None:
            credited += result.bank_record.amount

    expected = sum(buckets.values(), Decimal("0"))
    variance = expected - credited

    print(RULE)
    print("5. CASH POSITION")
    print(RULE)
    print()
    for name, amount in buckets.items():
        print(f"  {name:<24}  INR {_rupees(amount):>14}   "
              f"{counts[name]:>3} records")
    print(f"  {THIN[:60]}")
    print(f"  {'total expected':<24}  INR {_rupees(expected):>14}")
    print(f"  {'bank actually credited':<24}  INR {_rupees(credited):>14}")
    print(f"  {'variance':<24}  INR {_rupees(variance):>14}")
    print()

    blocked_pct = (
        round(100.0 * float(buckets["blocked in exceptions"]) / float(expected), 1)
        if expected else 0.0
    )
    print(f"  {blocked_pct}% of the batch's VALUE is blocked behind")
    print("  exceptions. No record count shows that.")
    print()

    if rejected:
        print(f"  {rejected} record(s) rejected at ingestion carry no")
        print("  parseable amount. Their value is UNKNOWN and excluded")
        print("  above — reporting an unknown as zero would let corrupted")
        print("  money quietly balance the books.")
        print()

    return {
        "by_bucket": {k: str(v) for k, v in buckets.items()},
        "total_expected_settlement": str(expected),
        "total_bank_credited": str(credited),
        "variance_vs_bank_credited": str(variance),
        "records_rejected_at_ingestion": rejected,
    }


# ======================================================================
# MAIN
# ======================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the deterministic pipeline. No model involved."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable summary instead of the report.",
    )
    parser.add_argument(
        "--raw-dir",
        default=str(RAW_DIR),
        help="Directory holding the three source files.",
    )
    args = parser.parse_args()

    raw_dir = Path(args.raw_dir)

    batch = load_batch(raw_dir)
    normalized = normalize_batch(batch)
    match_results = run_matching(normalized.records)
    decisions = decide_batch(match_results)

    if args.json:
        counts = Counter(d.status.value for d in decisions)
        matched = counts.get(DecisionStatus.MATCHED.value, 0)
        print(json.dumps({
            "total_records": len(decisions),
            "matched": matched,
            "match_rate_pct": (
                round(100.0 * matched / len(decisions), 2) if decisions else 0.0
            ),
            "by_status": dict(sorted(counts.items())),
            "rejected_at_ingestion": batch.total_errors,
            "exceptions": [
                {
                    "txn_id": d.txn_id,
                    "status": d.status.value,
                    "exception_code": d.exception_code.value,
                    "reason_codes": [c.value for c in d.reason_codes],
                    "confidence_score": d.confidence_score,
                    "matched_rule": (d.evidence or {}).get("matched_rule"),
                }
                for d in sorted(decisions, key=lambda x: x.txn_id)
                if d.status != DecisionStatus.MATCHED
            ],
        }, indent=2))
        return

    print()
    print(RULE)
    print("AI FINANCE CONTROLLER — DETERMINISTIC PIPELINE")
    print(RULE)
    print()
    print("  ingest -> normalize -> match -> verify tax -> decide")
    print()
    print("  No model is involved in anything below this line. Every")
    print("  number is produced by deterministic code, and would be")
    print("  identical with the AI layer deleted entirely.")
    print()

    stage_ingestion(batch)
    stage_normalization(normalized)
    stage_matching(match_results)
    stage_completeness(account_for_bank_rows(normalized.records, match_results))
    stage_decisions(decisions)
    stage_exceptions(decisions)
    stage_cash(match_results, decisions, batch.total_errors)

    print(RULE)
    print("WHAT THIS DEMONSTRATED")
    print(RULE)
    print()
    print("  - one finance-ops loop, closed end to end")
    print("  - the full batch, unfiltered and unsampled")
    print("  - match rate and the complete exception list")
    print("  - every decision traceable to the rule that produced it")
    print("  - the batch in rupees, not only in record counts")
    print()
    print("  For the agent layer on top of this:")
    print("      python scripts/demo_agent.py --offline")
    print()
    print("  For measured accuracy against independent ground truth:")
    print("      python scripts/report_accuracy.py")
    print()


if __name__ == "__main__":
    main()
