# tests/test_query_tools.py
"""
Phase 6 Step 1 — read-only query tool tests.

These tools sit directly under the language model in Step 3, so the
properties that matter most are not "does it return data" but:

    1. The numbers agree with decide_batch(). The tools are a VIEW,
       not a second calculation.
    2. Nothing is truncated. Track 04 warns that one cherry-picked
       match proves nothing; the exception list must be complete.
    3. A hallucinated transaction ID raises rather than returning
       something empty and plausible.
    4. Output is JSON-serialisable, because the layer above builds
       prompts and payloads.
    5. Repeated calls return identical results. A model that asks the
       same question twice must not get two different numbers.

No LLM, no network, no API key. These run anywhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.models import DecisionStatus
from src.ingestion.loader import load_batch
from src.normalization.engine import normalize_batch
from src.matching.engine import run_matching
from src.exceptions.manager import decide_batch
from src.agent.tools.query_tools import (
    BatchQueryContext,
    TxnNotFoundError,
    RESOLVED_STATUSES,
)


ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"


def build_context() -> BatchQueryContext:
    return BatchQueryContext(raw_dir=RAW_DIR)


def independent_decisions():
    """
    Run the pipeline directly, without the tools, to compare against.

    The point of these tests is that the tools do not compute anything
    new. Comparing tool output against an independent pipeline run is
    the only way to show that.
    """
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    return decide_batch(run_matching(normalized.records))


# ======================================================================
# CONSTRUCTION
# ======================================================================

def test_context_loads_the_batch_at_construction():
    context = build_context()

    assert context.decisions, "no decisions loaded"
    assert len(context.decisions) == len(context.match_results)


def test_context_matches_an_independent_pipeline_run():
    """
    The tools must be a view over decide_batch(), not a reimplementation.
    """
    context = build_context()
    expected = independent_decisions()

    assert len(context.decisions) == len(expected)

    by_txn = {d.txn_id: d for d in expected}

    for decision in context.decisions:
        reference = by_txn[decision.txn_id]
        assert decision.status == reference.status
        assert decision.exception_code == reference.exception_code
        assert decision.confidence_score == reference.confidence_score
        assert decision.tax_verified == reference.tax_verified


# ======================================================================
# TOOL 1 -- MATCH RATE
# ======================================================================

def test_match_rate_counts_every_record():
    context = build_context()
    report = context.get_match_rate()

    assert report["total_records"] == len(context.decisions)
    assert sum(report["by_status"].values()) == report["total_records"]


def test_match_rate_agrees_with_raw_decisions():
    context = build_context()
    report = context.get_match_rate()

    expected_matched = sum(
        1 for d in context.decisions
        if d.status == DecisionStatus.MATCHED
    )

    assert report["matched"] == expected_matched
    assert report["unresolved"] == report["total_records"] - expected_matched


def test_match_rate_percentage_is_consistent():
    context = build_context()
    report = context.get_match_rate()

    recomputed = round(
        100.0 * report["matched"] / report["total_records"], 2
    )
    assert report["match_rate_pct"] == recomputed


def test_partial_match_is_not_counted_as_matched():
    """
    PARTIAL_MATCH means tax could not be verified. Folding it into the
    headline match rate would describe an unverified record as
    reconciled.
    """
    context = build_context()
    report = context.get_match_rate()

    partial = report["by_status"].get(
        DecisionStatus.PARTIAL_MATCH.value, 0
    )

    if partial:
        assert report["matched"] + partial <= report["total_records"]
        assert report["matched"] != report["total_records"]


def test_confidence_tiers_cover_every_record():
    context = build_context()
    report = context.get_match_rate()

    assert (
        sum(report["by_confidence_tier"].values())
        == report["total_records"]
    )


# ======================================================================
# TOOL 2 -- EXCEPTIONS
# ======================================================================

def test_exceptions_list_is_complete_not_sampled():
    """
    Track 04: "One cherry-picked match proves nothing." The list must
    contain every unresolved record.
    """
    context = build_context()
    report = context.get_exceptions()

    expected = [
        d for d in context.decisions
        if d.status not in RESOLVED_STATUSES
    ]

    assert report["count"] == len(expected)
    assert len(report["exceptions"]) == len(expected)

    returned_ids = {e["txn_id"] for e in report["exceptions"]}
    assert returned_ids == {d.txn_id for d in expected}


def test_exceptions_and_match_rate_agree():
    context = build_context()

    rate = context.get_match_rate()
    exceptions = context.get_exceptions()

    assert rate["unresolved"] == exceptions["count"]
    assert rate["matched"] + exceptions["count"] == rate["total_records"]


def test_exceptions_never_include_a_matched_record():
    context = build_context()

    for item in context.get_exceptions()["exceptions"]:
        assert item["status"] != DecisionStatus.MATCHED.value


def test_every_exception_carries_a_reason():
    """
    An unresolved record with no exception code tells an operator
    nothing actionable.
    """
    context = build_context()

    for item in context.get_exceptions()["exceptions"]:
        assert item["exception_code"] != "NONE", (
            f"{item['txn_id']} is unresolved with no exception code"
        )
        assert item["reason_codes"], (
            f"{item['txn_id']} has no reason codes"
        )


def test_status_filter_returns_only_that_status():
    context = build_context()
    everything = context.get_exceptions()

    seen = {e["status"] for e in everything["exceptions"]}

    for status in seen:
        filtered = context.get_exceptions(status=status)
        assert filtered["filter_status"] == status
        assert all(
            e["status"] == status for e in filtered["exceptions"]
        )


def test_status_filters_partition_the_full_list():
    context = build_context()
    everything = context.get_exceptions()

    seen = {e["status"] for e in everything["exceptions"]}
    total = sum(
        context.get_exceptions(status=s)["count"] for s in seen
    )

    assert total == everything["count"]


def test_unknown_status_raises_rather_than_returning_empty():
    """
    An empty list would read as "no exceptions of that kind", which is
    a different and false claim. The model can supply this argument, so
    it has to fail loudly.
    """
    context = build_context()

    try:
        context.get_exceptions(status="NOT_A_REAL_STATUS")
        assert False, "expected ValueError for an unknown status"
    except ValueError as exc:
        assert "Unknown status" in str(exc)


def test_exception_list_is_deterministically_ordered():
    context = build_context()

    first = [e["txn_id"] for e in context.get_exceptions()["exceptions"]]
    second = [e["txn_id"] for e in context.get_exceptions()["exceptions"]]

    assert first == second
    assert first == sorted(first)


# ======================================================================
# TOOL 3 -- EVIDENCE
# ======================================================================

def test_evidence_returns_the_real_decision():
    context = build_context()
    sample = context.decisions[0]

    evidence = context.get_evidence(sample.txn_id)

    assert evidence["txn_id"] == sample.txn_id
    assert evidence["status"] == sample.status.value
    assert evidence["exception_code"] == sample.exception_code.value
    assert evidence["confidence_score"] == sample.confidence_score


def test_evidence_for_hallucinated_txn_id_raises():
    """
    THE IMPORTANT ONE. Step 3 lets a model supply this argument. A model
    can propose an ID that does not exist. The only safe outcomes are
    the real record or an explicit failure -- never something empty and
    plausible reaching an operator.
    """
    context = build_context()

    for fake in ("TXN_99999", "TXN_00000", "NOT_A_TXN", ""):
        try:
            context.get_evidence(fake)
            assert False, f"expected TxnNotFoundError for {fake!r}"
        except TxnNotFoundError as exc:
            assert fake in str(exc) or "No transaction" in str(exc)


def test_evidence_exposes_tax_evaluated_flag():
    """
    C4 made tax_verified three-state. The evidence view must surface
    tax_evaluated so an operator can tell "not evaluated" from
    "evaluated and clean".
    """
    context = build_context()

    for decision in context.decisions:
        evidence = context.get_evidence(decision.txn_id)

        if evidence["tax_evaluated"] is False:
            assert evidence["tax_verified"] is None, (
                f"{decision.txn_id}: tax not evaluated but "
                f"tax_verified={evidence['tax_verified']}"
            )


def test_evidence_is_available_for_every_record():
    context = build_context()

    for decision in context.decisions:
        evidence = context.get_evidence(decision.txn_id)
        assert evidence["txn_id"] == decision.txn_id


# ======================================================================
# TOOL 4 -- THROUGHPUT
# ======================================================================

def test_throughput_report_reads_the_benchmark():
    context = build_context()
    report = context.get_throughput_report()

    if not report["available"]:
        # Legitimate state -- benchmark not yet run.
        assert report["reason"]
        assert report["runs"] == []
        return

    assert report["runs"]
    assert report["peak_records_per_second"] > 0
    assert report["peak_at_batch_size"] in report["batch_sizes"]
    assert "caveat" in report


def test_missing_throughput_file_is_not_an_error():
    """
    An absent benchmark is a gap in evidence, not a broken tool. The
    agent should be able to say so rather than crash.
    """
    context = BatchQueryContext(
        raw_dir=RAW_DIR,
        throughput_path=ROOT / "data" / "does_not_exist.json",
    )

    report = context.get_throughput_report()

    assert report["available"] is False
    assert report["runs"] == []
    assert "reason" in report


# ======================================================================
# CONTRACT PROPERTIES
# ======================================================================

def test_all_tool_output_is_json_serialisable():
    """
    Step 3 builds prompts and JSON payloads from these. A Decimal or an
    enum leaking through would fail there instead of here.
    """
    context = build_context()

    payloads = [
        context.get_match_rate(),
        context.get_exceptions(),
        context.get_throughput_report(),
        context.get_evidence(context.decisions[0].txn_id),
    ]

    for payload in payloads:
        json.dumps(payload)          # raises if anything is not JSON-safe


def test_repeated_calls_return_identical_results():
    """
    A model asking the same question twice must not get two different
    numbers.
    """
    context = build_context()

    assert context.get_match_rate() == context.get_match_rate()
    assert context.get_exceptions() == context.get_exceptions()

    txn_id = context.decisions[0].txn_id
    assert context.get_evidence(txn_id) == context.get_evidence(txn_id)


def test_two_contexts_over_the_same_data_agree():
    """
    Determinism across instances, not just within one.
    """
    assert build_context().get_match_rate() == build_context().get_match_rate()


def test_tools_expose_no_mutation_surface():
    """
    Structural check: this module must not grow a tool that recomputes
    a financial outcome. A `re_evaluate` or `set_status` tool would
    reopen the exact failure mode the architecture exists to prevent,
    so the absence is asserted rather than left to code review.
    """
    forbidden = (
        "re_evaluate", "reevaluate", "rematch", "recompute",
        "set_status", "update_", "override", "approve", "resolve_",
    )

    public = [
        name for name in dir(BatchQueryContext)
        if not name.startswith("_")
    ]

    for name in public:
        for pattern in forbidden:
            assert pattern not in name.lower(), (
                f"BatchQueryContext.{name} looks like a mutation or "
                f"recomputation surface ({pattern!r}). Query tools are "
                "read-only by design."
            )


def main() -> None:
    test_context_loads_the_batch_at_construction()
    test_context_matches_an_independent_pipeline_run()
    test_match_rate_counts_every_record()
    test_match_rate_agrees_with_raw_decisions()
    test_match_rate_percentage_is_consistent()
    test_partial_match_is_not_counted_as_matched()
    test_confidence_tiers_cover_every_record()
    test_exceptions_list_is_complete_not_sampled()
    test_exceptions_and_match_rate_agree()
    test_exceptions_never_include_a_matched_record()
    test_every_exception_carries_a_reason()
    test_status_filter_returns_only_that_status()
    test_status_filters_partition_the_full_list()
    test_unknown_status_raises_rather_than_returning_empty()
    test_exception_list_is_deterministically_ordered()
    test_evidence_returns_the_real_decision()
    test_evidence_for_hallucinated_txn_id_raises()
    test_evidence_exposes_tax_evaluated_flag()
    test_evidence_is_available_for_every_record()
    test_throughput_report_reads_the_benchmark()
    test_missing_throughput_file_is_not_an_error()
    test_all_tool_output_is_json_serialisable()
    test_repeated_calls_return_identical_results()
    test_two_contexts_over_the_same_data_agree()
    test_tools_expose_no_mutation_surface()

    print("Phase 6 Step 1 query tool tests passed.")


if __name__ == "__main__":
    main()