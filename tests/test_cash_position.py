# tests/test_cash_position.py
"""
Tests for get_cash_position() -- the tool that answers the second half
of Track 04's headline, "run the books AND THE CASH POSITION".

WHAT MAKES THIS NUMBER TRUSTWORTHY
----------------------------------
Not that it is computed correctly -- that is table stakes. What makes it
trustworthy is that a reader can CHECK it by hand:

    the four buckets sum to total_expected_settlement
    total_expected - total_bank_credited == variance
    every record lands in exactly one bucket
    no record is silently dropped

If any of those stops holding, the report becomes a number you have to
take on faith, which is the opposite of what a reconciliation artifact
is for. Every arithmetic property below exists so the report stays
verifiable rather than merely produced.

THE EXHAUSTIVENESS GUARD
------------------------
CASH_BUCKET_BY_STATUS is checked against the DecisionStatus enum itself,
not against a hand-written list. A status added later with no bucket
would silently drop real money out of the position; a status in two
buckets would double-count it. Either way the totals stop reconciling
and the report stops being checkable -- so the mapping is verified
against the source of truth rather than a copy of it.
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.financial import settlement_expected_net
from src.models import DecisionStatus
from src.agent.tools.query_tools import (
    CASH_BUCKET_BY_STATUS,
    CASH_BUCKETS,
    BatchQueryContext,
)

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"

_context: BatchQueryContext | None = None


def build_context() -> BatchQueryContext:
    """One pipeline run shared across the module -- it is read-only."""
    global _context
    if _context is None:
        _context = BatchQueryContext(raw_dir=RAW_DIR)
    return _context


# ======================================================================
# STRUCTURE -- the bucket map cannot silently lose money
# ======================================================================

def test_every_status_has_exactly_one_cash_bucket():
    """
    Checked against the ENUM, not a hand-written list.

    A DecisionStatus with no bucket would raise a KeyError at runtime
    for any batch containing one -- but only if such a record happened
    to exist. This fails immediately instead.
    """
    for status in DecisionStatus:
        assert status in CASH_BUCKET_BY_STATUS, (
            f"{status.value} has no cash bucket. Every status must place "
            "its money somewhere, or the totals stop reconciling."
        )

    assert set(CASH_BUCKET_BY_STATUS) == set(DecisionStatus), (
        "CASH_BUCKET_BY_STATUS maps a status that is not a real "
        "DecisionStatus member."
    )


def test_every_bucket_target_is_a_declared_bucket():
    for status, bucket in CASH_BUCKET_BY_STATUS.items():
        assert bucket in CASH_BUCKETS, (
            f"{status.value} maps to {bucket!r}, which is not in "
            f"CASH_BUCKETS."
        )


def test_matched_is_the_only_status_counted_as_settled():
    """
    Mirrors get_match_rate()'s refusal to fold PARTIAL_MATCH into
    `matched`. A partial match is a record whose tax could not be
    verified; counting its value as settled would describe unverified
    money as reconciled.
    """
    settled = [
        status for status, bucket in CASH_BUCKET_BY_STATUS.items()
        if bucket == "settled_and_verified"
    ]

    assert settled == [DecisionStatus.MATCHED]


# ======================================================================
# ARITHMETIC -- the report must be checkable by hand
# ======================================================================

def test_buckets_sum_to_total_expected_settlement():
    """
    THE CORE INVARIANT. Exact Decimal equality, not a tolerance -- these
    are the same numbers added twice, so any drift is a bug, not
    rounding.
    """
    position = build_context().get_cash_position()

    bucket_sum = sum(
        Decimal(position["by_bucket"][bucket]["amount"])
        for bucket in CASH_BUCKETS
    )

    assert bucket_sum == Decimal(position["total_expected_settlement"]), (
        f"buckets sum to {bucket_sum}, headline says "
        f"{position['total_expected_settlement']}"
    )


def test_bucket_record_counts_sum_to_total_records():
    """
    Value reconciles AND population reconciles. A record could otherwise
    contribute its amount to one bucket while being counted in none.
    """
    position = build_context().get_cash_position()

    counted = sum(
        position["by_bucket"][bucket]["records"]
        for bucket in CASH_BUCKETS
    )

    assert counted == position["total_records"]


def test_variance_is_expected_minus_credited():
    position = build_context().get_cash_position()

    expected = Decimal(position["total_expected_settlement"])
    credited = Decimal(position["total_bank_credited"])

    assert Decimal(position["variance_vs_bank_credited"]) == (
        expected - credited
    )


def test_totals_match_an_independent_recomputation():
    """
    Recompute the whole position from match_results directly, using the
    shared settlement definition, and require agreement.

    This is the guard that would catch the tool quietly using a
    different amount -- gross instead of net, say -- while still
    producing internally consistent arithmetic.
    """
    context = build_context()
    position = context.get_cash_position()

    expected_total = sum(
        (settlement_expected_net(r.pg_record) for r in context.match_results),
        Decimal("0"),
    )
    credited_total = sum(
        (
            r.bank_record.amount
            for r in context.match_results
            if r.bank_record is not None
        ),
        Decimal("0"),
    )

    assert Decimal(position["total_expected_settlement"]) == expected_total
    assert Decimal(position["total_bank_credited"]) == credited_total


def test_each_bucket_matches_an_independent_recomputation():
    context = build_context()
    position = context.get_cash_position()

    recomputed = {bucket: Decimal("0") for bucket in CASH_BUCKETS}

    for result in context.match_results:
        decision = context._by_txn[result.txn_id]
        bucket = CASH_BUCKET_BY_STATUS[decision.status]
        recomputed[bucket] += settlement_expected_net(result.pg_record)

    for bucket in CASH_BUCKETS:
        assert Decimal(
            position["by_bucket"][bucket]["amount"]
        ) == recomputed[bucket], bucket


# ======================================================================
# HONESTY -- what the report must not hide
# ======================================================================

def test_uncredited_money_is_reported_not_dropped():
    """
    A record with no bank row is money the merchant is OWED and has not
    received -- the most operationally urgent line in the report.

    Excluding it would understate exposure AND break the bucket
    arithmetic. This asserts it is present with a real amount.
    """
    context = build_context()
    position = context.get_cash_position()

    uncredited = [
        r for r in context.match_results
        if context._by_txn[r.txn_id].status == DecisionStatus.UNMATCHED
    ]

    bucket = position["by_bucket"]["not_yet_credited"]

    assert bucket["records"] == len(uncredited)

    if uncredited:
        assert Decimal(bucket["amount"]) > 0, (
            "uncredited records exist but the bucket reports no value"
        )


def test_ingestion_rejections_are_disclosed_as_a_count_not_a_zero():
    """
    Corrupted records have an unparseable gross, so their value is
    genuinely UNKNOWN. Reporting them as zero would let corrupted money
    quietly balance the books; omitting them silently would hide that
    the batch is incomplete.

    The report must state the count and say the value is excluded.
    """
    position = build_context().get_cash_position()

    assert "records_rejected_at_ingestion" in position
    assert isinstance(position["records_rejected_at_ingestion"], int)

    note = position["rejected_value_note"].lower()
    assert "unknown" in note
    assert "excluded" in note or "not" in note


def test_rejected_records_are_absent_from_the_totals():
    """
    total_records must count decisioned records only. If a rejected
    record leaked in, it would carry no amount and the population
    arithmetic would disagree with the value arithmetic.
    """
    context = build_context()
    position = context.get_cash_position()

    assert position["total_records"] == len(context.decisions)
    assert position["records_rejected_at_ingestion"] > 0, (
        "this dataset is expected to contain corrupted records -- if it "
        "no longer does, this guard is no longer proving anything"
    )


def test_caveat_states_the_duplicate_credit_convention():
    """
    total_bank_credited counts the SELECTED bank row per transaction. A
    duplicate credit is real money that moved, so the convention has to
    be stated rather than left for a reader to infer from a number that
    does not add up the way they expect.
    """
    position = build_context().get_cash_position()
    caveat = position["caveat"].lower()

    assert "duplicate" in caveat
    assert "expected net" in caveat


def test_amounts_agree_with_get_match_rate_population():
    """
    The two headline tools must describe the same batch. A disagreement
    would mean one of them is reading a different snapshot.
    """
    context = build_context()

    position = context.get_cash_position()
    rate = context.get_match_rate()

    assert position["total_records"] == rate["total_records"]
    assert (
        position["by_bucket"]["settled_and_verified"]["records"]
        == rate["matched"]
    )


# ======================================================================
# CONTRACT -- shape, serialisability, read-only
# ======================================================================

def test_amounts_are_strings_not_floats():
    """
    Decimal is not JSON-serialisable and float would reintroduce the
    precision problem models.py rejects at ingestion. Amounts cross this
    boundary as strings.
    """
    position = build_context().get_cash_position()

    money_fields = [
        position["total_expected_settlement"],
        position["total_bank_credited"],
        position["variance_vs_bank_credited"],
    ] + [
        position["by_bucket"][bucket]["amount"] for bucket in CASH_BUCKETS
    ]

    for value in money_fields:
        assert isinstance(value, str), f"{value!r} is not a string"
        Decimal(value)  # must parse


def test_amounts_are_quantised_to_paise():
    position = build_context().get_cash_position()

    for bucket in CASH_BUCKETS:
        amount = position["by_bucket"][bucket]["amount"]
        assert Decimal(amount).as_tuple().exponent == -2, (
            f"{bucket} amount {amount} is not quantised to 2dp"
        )


def test_output_is_json_serialisable():
    import json

    json.dumps(build_context().get_cash_position())


def test_repeated_calls_return_identical_results():
    context = build_context()
    assert context.get_cash_position() == context.get_cash_position()


def test_tool_does_not_mutate_the_decisions():
    """
    get_cash_position() aggregates; it must not touch what it reads.
    """
    context = build_context()

    before = [
        (d.txn_id, d.status.value, d.exception_code.value)
        for d in context.decisions
    ]

    context.get_cash_position()

    after = [
        (d.txn_id, d.status.value, d.exception_code.value)
        for d in context.decisions
    ]

    assert before == after


# ======================================================================
# INTEGRATION -- registry and agent
# ======================================================================

def test_cash_position_is_registered_and_dispatchable():
    from src.agent.tools.registry import TOOL_REGISTRY, dispatch

    assert "get_cash_position" in TOOL_REGISTRY

    envelope = dispatch(build_context(), "get_cash_position", {})

    assert envelope["ok"] is True
    assert envelope["result"] == build_context().get_cash_position()


def test_cash_position_takes_no_arguments():
    """
    An invented argument must be rejected, not dropped -- the same rule
    every other tool follows.
    """
    from src.agent.tools.registry import dispatch

    envelope = dispatch(
        build_context(), "get_cash_position", {"currency": "USD"}
    )

    assert envelope["ok"] is False
    assert envelope["error_type"] == "InvalidToolArgumentsError"


def test_agent_answer_data_matches_a_direct_call():
    """
    THE DATA INVARIANT, applied to the new tool.

    A model that phrases the answer however it likes still cannot change
    what the tool returned.
    """
    import json

    from src.agent.controller import FinanceControllerAgent

    context = build_context()

    def stub(prompt: str) -> str:
        if "tool-selection step" in prompt:
            return json.dumps(
                {"tool_name": "get_cash_position", "arguments": {}}
            )
        return "Most of the batch value is blocked behind exceptions."

    answer = FinanceControllerAgent(stub, context).ask(
        "How much money is stuck?"
    )

    assert answer.tool_used == "get_cash_position"
    assert answer.data == context.get_cash_position()


def test_a_lying_model_cannot_change_the_cash_position():
    import json

    from src.agent.controller import FinanceControllerAgent

    context = build_context()

    def liar(prompt: str) -> str:
        if "tool-selection step" in prompt:
            return json.dumps(
                {"tool_name": "get_cash_position", "arguments": {}}
            )
        return "Everything settled cleanly. Nothing is blocked. Variance is zero."

    answer = FinanceControllerAgent(liar, context).ask(
        "How much money is stuck?"
    )

    assert answer.data == context.get_cash_position()
    assert Decimal(answer.data["variance_vs_bank_credited"]) != 0


def test_fallback_renders_the_cash_position_without_a_model():
    """
    A phrasing failure must not lose the numbers -- they already exist
    by the time phrasing runs.
    """
    import json

    from src.agent.controller import FinanceControllerAgent

    context = build_context()
    calls = {"n": 0}

    def selection_only(prompt: str) -> str:
        calls["n"] += 1
        if "tool-selection step" in prompt:
            return json.dumps(
                {"tool_name": "get_cash_position", "arguments": {}}
            )
        raise ConnectionError("phrasing unavailable")

    answer = FinanceControllerAgent(selection_only, context).ask(
        "How much money is stuck?"
    )

    assert answer.answer_source == "deterministic_fallback"
    assert answer.data == context.get_cash_position()
    assert answer.data["total_expected_settlement"] in answer.answer
