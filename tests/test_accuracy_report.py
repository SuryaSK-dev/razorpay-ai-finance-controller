# tests/test_accuracy_report.py
"""
Accuracy report tests.

This report is the answer to one third of Track 04's stated bar
("measured accuracy"), so the properties that matter are about
whether the number can be trusted, not whether it is high:

    1. The denominator is honest -- rejected records are counted, not
       dropped, because dropping them inflates the percentage.
    2. It measures against INDEPENDENT ground truth, not against the
       engine's own prior output.
    3. Every divergence is itemised. No sampling.
    4. The label-correction history is disclosed in the artifact.
    5. The numbers are internally consistent.

Deliberately does NOT assert a minimum accuracy. A test that fails
when accuracy drops would tempt someone to adjust ground truth to make
it pass, which is exactly the failure this project already recorded
twice.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.models import DecisionStatus, ExceptionCode


ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = ROOT / "data" / "eval" / "accuracy_report.json"
GT_PATH = ROOT / "data" / "ground_truth.json"


def load_report() -> dict:
    assert REPORT_PATH.exists(), (
        f"{REPORT_PATH.name} missing. Run scripts/report_accuracy.py"
    )
    with REPORT_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_ground_truth() -> list[dict]:
    with GT_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


# ======================================================================
# THE DENOMINATOR IS HONEST
# ======================================================================

def test_every_ground_truth_entry_is_accounted_for():
    """
    evaluated + rejected must equal the full ground-truth set. A record
    that is neither evaluated nor explicitly rejected has vanished from
    the denominator.
    """
    report = load_report()

    assert (
        report["decisions_evaluated"] + report["rejected_at_ingestion"]
        == report["ground_truth_entries"]
    )


def test_ground_truth_entry_count_matches_the_file():
    report = load_report()
    assert report["ground_truth_entries"] == len(load_ground_truth())


def test_rejected_records_are_itemised_not_just_counted():
    report = load_report()

    assert (
        len(report["rejected_records"])
        == report["rejected_at_ingestion"]
    )

    for record in report["rejected_records"]:
        assert record["txn_id"]
        assert record["category"]


def test_rejected_records_are_the_corrupted_category():
    """
    Only ingestion-rejected records should be missing from the
    decisions. Anything else disappearing is a pipeline bug, not an
    expected exclusion.
    """
    report = load_report()

    for record in report["rejected_records"]:
        assert record["category"] == "corrupted", (
            f"{record['txn_id']} ({record['category']}) produced no "
            "decision but is not a corrupted record"
        )


# ======================================================================
# THE NUMBERS ARE INTERNALLY CONSISTENT
# ======================================================================

def test_status_accuracy_arithmetic():
    report = load_report()
    accuracy = report["status_accuracy"]

    assert accuracy["total"] == report["decisions_evaluated"]
    assert 0 <= accuracy["correct"] <= accuracy["total"]

    expected_pct = round(
        100.0 * accuracy["correct"] / accuracy["total"], 2
    )
    assert accuracy["percent"] == expected_pct


def test_exception_code_accuracy_arithmetic():
    report = load_report()
    accuracy = report["exception_code_accuracy"]

    assert accuracy["total"] == report["decisions_evaluated"]
    assert 0 <= accuracy["correct"] <= accuracy["total"]

    expected_pct = round(
        100.0 * accuracy["correct"] / accuracy["total"], 2
    )
    assert accuracy["percent"] == expected_pct


def test_divergence_count_reconciles_with_accuracy():
    """
    A record diverges if EITHER status or code is wrong, so the
    divergence list must be at least as large as the worse of the two
    error counts and no larger than their sum.
    """
    report = load_report()

    status_errors = (
        report["status_accuracy"]["total"]
        - report["status_accuracy"]["correct"]
    )
    code_errors = (
        report["exception_code_accuracy"]["total"]
        - report["exception_code_accuracy"]["correct"]
    )
    divergences = len(report["divergences"])

    assert divergences >= max(status_errors, code_errors)
    assert divergences <= status_errors + code_errors


def test_per_category_totals_sum_to_the_ground_truth_set():
    report = load_report()

    total = sum(
        stats["total"] for stats in report["by_category"].values()
    )
    assert total == report["ground_truth_entries"]


def test_per_category_correct_counts_sum_to_the_headline():
    report = load_report()

    status_sum = sum(
        stats["status_ok"] for stats in report["by_category"].values()
    )
    code_sum = sum(
        stats["code_ok"] for stats in report["by_category"].values()
    )

    assert status_sum == report["status_accuracy"]["correct"]
    assert code_sum == report["exception_code_accuracy"]["correct"]


def test_non_evaluable_records_are_named_not_scored_zero():
    """
    A record rejected at ingestion produces no decision. Scoring it
    zero-correct made the category table sum to 55/63 (87.30%) while the
    headline above it divided by 61 (90.16%) -- two different accuracies
    from one artifact (FAILURE_LOG.md section 71).

    Zero-correct and not-evaluable are different facts and the artifact
    must say which it means.
    """
    report = load_report()

    for category, stats in report["by_category"].items():
        assert "not_evaluable" in stats, (
            f"{category}: by_category entries must declare not_evaluable, "
            f"even when it is zero"
        )
        assert stats["not_evaluable"] <= stats["total"]

        if stats["not_evaluable"]:
            assert "reason" in stats and len(stats["reason"]) > 20, (
                f"{category}: {stats['not_evaluable']} non-evaluable "
                f"records with no reason attached"
            )
            assert stats["evaluable"] == (
                stats["total"] - stats["not_evaluable"]
            )


def test_evaluable_totals_sum_to_the_accuracy_denominator():
    """
    THE ONE THAT WOULD HAVE CAUGHT IT.

    Summing the category table must reproduce the headline's denominator.
    `test_per_category_totals_sum_to_the_ground_truth_set` checks the
    table against 63; nothing checked it against the 61 the percentage is
    actually divided by, so the two could drift apart silently.
    """
    report = load_report()

    evaluable = sum(
        stats["total"] - stats["not_evaluable"]
        for stats in report["by_category"].values()
    )

    assert evaluable == report["decisions_evaluated"]
    assert evaluable == report["status_accuracy"]["total"]
    assert evaluable == report["exception_code_accuracy"]["total"]


def test_no_category_scores_more_correct_than_it_could_evaluate():
    report = load_report()

    for category, stats in report["by_category"].items():
        evaluable = stats["total"] - stats["not_evaluable"]
        assert stats["status_ok"] <= evaluable, (
            f"{category}: {stats['status_ok']} correct out of "
            f"{evaluable} evaluable"
        )
        assert stats["code_ok"] <= evaluable


# ======================================================================
# DIVERGENCES ARE COMPLETE AND USABLE
# ======================================================================

def test_every_divergence_is_fully_described():
    """
    A divergence a reader cannot act on is not an honest report.
    """
    report = load_report()

    for item in report["divergences"]:
        assert item["txn_id"]
        assert item["category"]
        assert item["expected_status"]
        assert item["actual_status"]
        assert item["expected_exception_code"]
        assert item["actual_exception_code"]
        assert isinstance(item["status_matched"], bool)
        assert isinstance(item["code_matched"], bool)


def test_divergences_are_genuinely_divergent():
    """
    Nothing may appear in the list that actually matched on both
    fields -- that would overstate the error count.
    """
    report = load_report()

    for item in report["divergences"]:
        assert not (item["status_matched"] and item["code_matched"]), (
            f"{item['txn_id']} is listed as divergent but matched on "
            "both status and exception code"
        )


def test_divergent_values_are_real_enum_members():
    """
    A divergence citing a status the engine cannot produce would mean
    the comparison itself is broken.
    """
    report = load_report()

    valid_statuses = {s.value for s in DecisionStatus}
    valid_codes = {c.value for c in ExceptionCode}

    for item in report["divergences"]:
        assert item["actual_status"] in valid_statuses
        assert item["actual_exception_code"] in valid_codes


# ======================================================================
# DISCLOSURE
# ======================================================================

def test_label_corrections_are_disclosed_in_the_artifact():
    """
    Ground truth was corrected twice after the engine disagreed with
    it. That belongs in the artifact, not only in a log a reader might
    not open.
    """
    report = load_report()

    corrections = report["ground_truth_label_corrections"]
    assert corrections, "label corrections must be disclosed"

    for correction in corrections:
        assert correction["category"]
        assert correction["was"]
        assert correction["now"]
        assert len(correction["reason"]) > 40
        assert "FAILURE_LOG" in correction["log_ref"]


def test_scope_limitation_is_stated():
    report = load_report()

    scope = report["scope"].lower()
    assert "self-generated" in scope
    assert "generalis" in scope or "generaliz" in scope


def test_method_describes_ground_truth_independence():
    """
    The claim that makes this an accuracy measurement rather than a
    consistency check is that ground truth is independent. It has to
    be stated.
    """
    report = load_report()

    method = report["method"].lower()
    assert "never read by the pipeline" in method


def main() -> None:
    test_every_ground_truth_entry_is_accounted_for()
    test_ground_truth_entry_count_matches_the_file()
    test_rejected_records_are_itemised_not_just_counted()
    test_rejected_records_are_the_corrupted_category()
    test_status_accuracy_arithmetic()
    test_exception_code_accuracy_arithmetic()
    test_divergence_count_reconciles_with_accuracy()
    test_per_category_totals_sum_to_the_ground_truth_set()
    test_per_category_correct_counts_sum_to_the_headline()
    test_every_divergence_is_fully_described()
    test_divergences_are_genuinely_divergent()
    test_divergent_values_are_real_enum_members()
    test_label_corrections_are_disclosed_in_the_artifact()
    test_scope_limitation_is_stated()
    test_method_describes_ground_truth_independence()

    print("Accuracy report tests passed.")


if __name__ == "__main__":
    main()