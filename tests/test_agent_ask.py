# tests/test_agent_ask.py
"""
Phase 6 Step 3 — tool selection and controller.ask() tests.

The model is now in the loop, so the properties that matter are about
what it CANNOT do:

    1. Selection is strict -- a malformed or invented choice is
       rejected, never guessed at.
    2. The model cannot authoritatively change a financial number.
       The deterministic tool result is always attached as `data`.
    3. LLM phrasing is accepted only when its numeric literals are
       grounded in the authoritative deterministic tool result.
    4. A grounding failure falls back to deterministic rendering.
    5. A phrasing failure does not lose the answer.
    6. A hallucinated transaction ID produces an honest failure,
       not a fabricated record.
    7. The tool selection contract cannot carry a financial fact.

Every LLM here is a deterministic stub. No network, no API key.
Real model behaviour is measured separately in the 5C evaluation
harness; these tests are about the control flow around it.

Run:
    python tests/test_agent_ask.py

or:

    python -m pytest tests/test_agent_ask.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.agent.controller import AgentAnswer, FinanceControllerAgent
from src.agent.tool_selection import (
    NO_TOOL,
    ToolSelection,
    build_selection_prompt,
    parse_selection,
    selection_rejection_reason,
    validate_selection,
)
from src.agent.tools.query_tools import BatchQueryContext


ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"


def build_context() -> BatchQueryContext:
    return BatchQueryContext(raw_dir=RAW_DIR)


# ======================================================================
# STUB MODELS
# ======================================================================


def stub_selecting(
    tool_name: str,
    arguments: dict | None = None,
):
    """
    A model that always selects one tool, then phrases anything.

    The selection prompt and the phrasing prompt are distinguishable by
    content, so one stub can serve both calls.
    """

    payload = json.dumps({
        "tool_name": tool_name,
        "arguments": arguments or {},
    })

    def call(prompt: str) -> str:
        if "tool-selection step" in prompt:
            return payload

        return "STUBBED ANSWER"

    return call


def stub_selection_only(
    tool_name: str,
    arguments: dict | None = None,
):
    """
    Selects successfully, then fails at phrasing.
    """

    payload = json.dumps({
        "tool_name": tool_name,
        "arguments": arguments or {},
    })

    def call(prompt: str) -> str:
        if "tool-selection step" in prompt:
            return payload

        raise RuntimeError("phrasing model unavailable")

    return call


def stub_broken(prompt: str) -> str:
    raise RuntimeError("model unavailable")


def stub_garbage(prompt: str) -> str:
    return "I think you should probably look at the exceptions?"


# ======================================================================
# CONTRACT
# ======================================================================


def test_selection_contract_has_no_financial_fields():
    """
    Same structural guarantee as NarrationExtraction: the model can
    name a tool, and has nowhere to put a status or an amount.
    """

    fields = set(ToolSelection.__dataclass_fields__)

    assert fields == {"tool_name", "arguments"}

    forbidden = (
        "amount",
        "status",
        "gst",
        "tds",
        "tax",
        "decision",
        "exception",
        "confidence",
        "matched",
        "verdict",
    )

    for name in fields:
        for pattern in forbidden:
            assert pattern not in name.lower()


def test_selection_contract_is_frozen():
    selection = ToolSelection(tool_name="get_match_rate")

    try:
        selection.tool_name = "get_exceptions"
        assert False, "ToolSelection should be frozen"
    except Exception:
        pass


def test_selection_contract_rejects_bad_construction():
    for bad in ("", None, 42):
        try:
            ToolSelection(tool_name=bad)
            assert False, f"expected rejection for {bad!r}"
        except (ValueError, TypeError):
            pass


# ======================================================================
# PROMPT
# ======================================================================


def test_selection_prompt_lists_every_tool():
    prompt = build_selection_prompt("how many matched?")

    for name in (
        "get_match_rate",
        "get_exceptions",
        "get_evidence",
        "get_throughput_report",
    ):
        assert name in prompt


def test_selection_prompt_forbids_answering():
    prompt = build_selection_prompt("how many matched?")

    assert "not answering" in prompt.lower()
    assert "do not have access to the data" in prompt.lower()


def test_selection_prompt_includes_the_question():
    prompt = build_selection_prompt("what failed in this batch?")

    assert "what failed in this batch?" in prompt


# ======================================================================
# PARSING -- STRICT
# ======================================================================


def test_parses_clean_json():
    selection = parse_selection(
        '{"tool_name": "get_match_rate", "arguments": {}}'
    )

    assert selection.tool_name == "get_match_rate"
    assert selection.arguments == {}


def test_unwraps_a_fenced_block():
    """
    The one leniency -- models add fences reflexively.
    """

    selection = parse_selection(
        '```json\n'
        '{"tool_name": "get_match_rate", "arguments": {}}\n'
        '```'
    )

    assert selection.tool_name == "get_match_rate"


def test_rejects_prose():
    for raw in (
        "",
        "   ",
        "use get_match_rate",
        "not json at all",
    ):
        try:
            parse_selection(raw)
            assert False, f"expected rejection for {raw!r}"
        except ValueError:
            pass


def test_rejects_unexpected_keys():
    """
    An extra key usually means the model is improvising -- including
    trying to supply the answer itself.
    """

    try:
        parse_selection(
            '{"tool_name": "get_match_rate", "arguments": {}, '
            '"answer": "61 matched"}'
        )

        assert False, "expected rejection"

    except ValueError as exc:
        assert "answer" in str(exc)


def test_rejects_non_object_arguments():
    try:
        parse_selection(
            '{"tool_name": "get_evidence", '
            '"arguments": "TXN_00042"}'
        )

        assert False, "expected rejection"

    except ValueError:
        pass


def test_does_not_fuzzy_match_tool_names():
    """
    A near-miss is not corrected.

    "get_match_rates" stays wrong because a model that is improvising
    the name is the model least likely to have chosen correctly.
    """

    selection = parse_selection(
        '{"tool_name": "get_match_rates", "arguments": {}}'
    )

    assert selection.tool_name == "get_match_rates"
    assert validate_selection(selection) is False


# ======================================================================
# VALIDATION
# ======================================================================


def test_valid_selections_pass():
    for name, args in [
        ("get_match_rate", {}),
        ("get_exceptions", {}),
        ("get_exceptions", {"status": "HUMAN_REVIEW"}),
        ("get_evidence", {"txn_id": "TXN_00001"}),
        ("get_throughput_report", {}),
        (NO_TOOL, {}),
    ]:
        assert validate_selection(
            ToolSelection(
                tool_name=name,
                arguments=args,
            )
        ) is True, f"{name} {args} should validate"


def test_invalid_selections_fail():
    for name, args in [
        ("get_refunds", {}),
        ("get_exceptions", {"limit": 5}),
        ("get_exceptions", {"status": "PENDING"}),
        ("get_evidence", {}),
        (NO_TOOL, {"txn_id": "TXN_00001"}),
    ]:
        assert validate_selection(
            ToolSelection(
                tool_name=name,
                arguments=args,
            )
        ) is False, f"{name} {args} should be rejected"


def test_rejection_reason_is_human_readable():
    reason = selection_rejection_reason(
        ToolSelection(tool_name="get_refunds")
    )

    assert "get_refunds" in reason
    assert "Available" in reason


# ======================================================================
# ask() -- HAPPY PATH
# ======================================================================


def test_ask_returns_real_numbers():
    context = build_context()

    agent = FinanceControllerAgent(
        stub_selecting("get_match_rate"),
        context,
    )

    answer = agent.ask("how many matched?")

    assert answer.answer_source == "llm"
    assert answer.tool_used == "get_match_rate"
    assert answer.data == context.get_match_rate()


def test_ask_passes_arguments_through():
    context = build_context()

    agent = FinanceControllerAgent(
        stub_selecting(
            "get_exceptions",
            {"status": "HUMAN_REVIEW"},
        ),
        context,
    )

    answer = agent.ask("what needs human review?")

    assert answer.tool_arguments == {
        "status": "HUMAN_REVIEW"
    }

    for item in answer.data["exceptions"]:
        assert item["status"] == "HUMAN_REVIEW"


def test_ask_evidence_for_a_real_transaction():
    context = build_context()
    txn_id = context.decisions[0].txn_id

    agent = FinanceControllerAgent(
        stub_selecting(
            "get_evidence",
            {"txn_id": txn_id},
        ),
        context,
    )

    answer = agent.ask(
        f"why is {txn_id} in that state?"
    )

    assert answer.data["txn_id"] == txn_id


def test_data_is_always_attached_on_success():
    """
    The prose must be checkable against the authoritative data it
    describes.
    """

    context = build_context()

    for tool in (
        "get_match_rate",
        "get_exceptions",
        "get_throughput_report",
    ):
        agent = FinanceControllerAgent(
            stub_selecting(tool),
            context,
        )

        answer = agent.ask("something")

        assert answer.data is not None


# ======================================================================
# ask() -- MODEL CANNOT CHANGE AUTHORITATIVE FINANCIAL DATA
# ======================================================================


def test_different_models_produce_identical_data():
    """
    THE CENTRAL DATA INVARIANT.

    Two different phrasing models, same selection:
        - the prose may differ;
        - the authoritative data may not.

    The deterministic tool result is independent of model phrasing.
    """

    context = build_context()

    def terse(prompt: str) -> str:
        if "tool-selection step" in prompt:
            return (
                '{"tool_name": "get_match_rate", '
                '"arguments": {}}'
            )

        return "Short."


    def verbose(prompt: str) -> str:
        if "tool-selection step" in prompt:
            return (
                '{"tool_name": "get_match_rate", '
                '"arguments": {}}'
            )

        return (
            "A considerably longer and more florid answer "
            "entirely."
        )


    a = FinanceControllerAgent(
        terse,
        context,
    ).ask("how did we do?")

    b = FinanceControllerAgent(
        verbose,
        context,
    ).ask("how did we do?")

    assert a.answer != b.answer
    assert a.data == b.data


def test_a_lying_model_cannot_corrupt_the_data():
    """
    A model that states false numbers in prose cannot change `data`.

    P1 grounding behaviour:
        1. deterministic tool returns the authoritative result;
        2. model invents 9999;
        3. grounding validator rejects the prose;
        4. controller returns deterministic fallback;
        5. authoritative data remains unchanged.
    """

    context = build_context()

    def liar(prompt: str) -> str:
        if "tool-selection step" in prompt:
            return (
                '{"tool_name": "get_match_rate", '
                '"arguments": {}}'
            )

        return (
            "All 9999 records matched perfectly with zero exceptions."
        )

    answer = FinanceControllerAgent(
        liar,
        context,
    ).ask("how did we do?")

    # Authoritative data must remain untouched.
    assert answer.data == context.get_match_rate()
    assert answer.data["total_records"] != 9999

    # The hallucinated prose must NOT be returned.
    assert answer.answer_source == "deterministic_fallback"
    assert "9999" not in answer.answer

    # The grounding violation must be observable for evaluation/debugging.
    assert answer.agent_metadata["grounding_valid"] is False
    assert answer.agent_metadata["grounding_violations"] == [
        "9999"
    ]


def test_large_fabricated_financial_number_is_rejected():
    """
    Stronger P1 regression.

    A model inventing a completely unrealistic financial value must
    also be rejected, even when it uses a currency symbol and decimal
    places.
    """

    context = build_context()

    def liar(prompt: str) -> str:
        if "tool-selection step" in prompt:
            return (
                '{"tool_name": "get_match_rate", '
                '"arguments": {}}'
            )

        return (
            "The batch has ₹999999999.99 in matched settlement value."
        )

    answer = FinanceControllerAgent(
        liar,
        context,
    ).ask("how did we do?")

    assert answer.answer_source == "deterministic_fallback"

    assert answer.data == context.get_match_rate()

    assert "999999999.99" not in answer.answer

    assert answer.agent_metadata["grounding_valid"] is False

    assert answer.agent_metadata["grounding_violations"] == [
        "999999999.99"
    ]


def test_grounded_numeric_answer_is_accepted():
    """
    A model is allowed to phrase an answer using numbers that are
    actually present in the authoritative tool result.

    This confirms the validator is not simply rejecting every number.
    """

    context = build_context()
    expected = context.get_match_rate()

    matched = expected["matched"]
    total = expected["total_records"]
    rate = expected["match_rate_pct"]

    def truthful(prompt: str) -> str:
        if "tool-selection step" in prompt:
            return (
                '{"tool_name": "get_match_rate", '
                '"arguments": {}}'
            )

        return (
            f"{matched} of {total} records matched "
            f"({rate}%)."
        )

    answer = FinanceControllerAgent(
        truthful,
        context,
    ).ask("how did we do?")

    assert answer.answer_source == "llm"
    assert answer.data == expected

    assert answer.agent_metadata["grounding_valid"] is True
    assert answer.agent_metadata["grounding_violations"] == []

    assert str(matched) in answer.answer
    assert str(total) in answer.answer
    assert str(rate) in answer.answer


def test_repeated_questions_return_identical_data():
    context = build_context()

    agent = FinanceControllerAgent(
        stub_selecting("get_match_rate"),
        context,
    )

    first = agent.ask("how many matched?")
    second = agent.ask("how many matched?")

    assert first.data == second.data


# ======================================================================
# ask() -- FAILURE PATHS
# ======================================================================


def test_selection_failure_is_honest():
    context = build_context()

    agent = FinanceControllerAgent(
        stub_broken,
        context,
    )

    answer = agent.ask("how many matched?")

    assert answer.answer_source == "error"
    assert answer.data is None
    assert "could not determine" in answer.answer.lower()


def test_garbage_selection_is_rejected():
    context = build_context()

    agent = FinanceControllerAgent(
        stub_garbage,
        context,
    )

    answer = agent.ask("how many matched?")

    assert answer.answer_source == "error"
    assert answer.data is None


def test_hallucinated_tool_is_rejected():
    context = build_context()

    agent = FinanceControllerAgent(
        stub_selecting("get_refunds"),
        context,
    )

    answer = agent.ask("show me refunds")

    assert answer.answer_source == "error"
    assert answer.data is None


def test_hallucinated_txn_id_is_honest():
    """
    Not a crash, and emphatically not a fabricated record.
    """

    context = build_context()

    agent = FinanceControllerAgent(
        stub_selecting(
            "get_evidence",
            {"txn_id": "TXN_99999"},
        ),
        context,
    )

    answer = agent.ask(
        "why is TXN_99999 unresolved?"
    )

    assert answer.answer_source == "error"
    assert answer.data is None
    assert "TXN_99999" in answer.answer


def test_phrasing_failure_does_not_lose_the_answer():
    """
    THE OTHER IMPORTANT ONE.

    The numbers exist by the time phrasing runs. A cosmetic model
    failure must not discard a correct deterministic answer.
    """

    context = build_context()

    agent = FinanceControllerAgent(
        stub_selection_only("get_match_rate"),
        context,
    )

    answer = agent.ask("how many matched?")

    assert answer.answer_source == "deterministic_fallback"
    assert answer.data == context.get_match_rate()
    assert str(answer.data["matched"]) in answer.answer


def test_fallback_renders_every_tool():
    context = build_context()
    txn_id = context.decisions[0].txn_id

    cases = [
        ("get_match_rate", {}),
        ("get_exceptions", {}),
        (
            "get_evidence",
            {"txn_id": txn_id},
        ),
        ("get_throughput_report", {}),
    ]

    for tool, args in cases:
        agent = FinanceControllerAgent(
            stub_selection_only(tool, args),
            context,
        )

        answer = agent.ask("something")

        assert answer.answer_source == (
            "deterministic_fallback"
        )
        assert answer.answer
        assert len(answer.answer) > 20


def test_none_selection_is_a_clean_refusal():
    context = build_context()

    agent = FinanceControllerAgent(
        stub_selecting(NO_TOOL),
        context,
    )

    answer = agent.ask(
        "what is the weather in Chennai?"
    )

    assert answer.answer_source == "no_tool"
    assert answer.data is None
    assert answer.tool_used is None


def test_missing_context_is_reported_not_improvised():
    agent = FinanceControllerAgent(
        stub_selecting("get_match_rate")
    )

    answer = agent.ask("how many matched?")

    assert answer.answer_source == "error"
    assert answer.data is None


def test_empty_question_is_rejected():
    context = build_context()

    agent = FinanceControllerAgent(
        stub_selecting("get_match_rate"),
        context,
    )

    for question in ("", "   "):
        answer = agent.ask(question)

        assert answer.answer_source == "error"


# ======================================================================
# PHASE 5 REGRESSION
# ======================================================================


def test_explain_still_works_without_a_query_context():
    """
    Phase 6 must not break the Phase 5 capability.

    An agent with no batch loaded can still narrate a decision it is
    handed.
    """

    from src.models import (
        DecisionStatus,
        ExceptionCode,
        MatchDecision,
    )

    decision = MatchDecision(
        txn_id="TXN_00001",
        status=DecisionStatus.MATCHED,
        confidence_score=95,
        matched_sources=[
            "pg",
            "bank",
            "invoice",
        ],
        tax_verified=True,
        exception_code=ExceptionCode.NONE,
        reason_codes=[ExceptionCode.NONE],
        evidence={},
    )

    agent = FinanceControllerAgent(
        lambda prompt: "Clean match."
    )

    response = agent.explain(decision)

    assert response.txn_id == "TXN_00001"
    assert response.status == "MATCHED"


def test_ask_answer_is_json_serialisable():
    context = build_context()

    agent = FinanceControllerAgent(
        stub_selecting("get_match_rate"),
        context,
    )

    answer = agent.ask("how many matched?")

    json.dumps({
        "question": answer.question,
        "answer": answer.answer,
        "answer_source": answer.answer_source,
        "tool_used": answer.tool_used,
        "tool_arguments": answer.tool_arguments,
        "data": answer.data,
        "agent_metadata": answer.agent_metadata,
    })


# ======================================================================
# DIRECT SCRIPT ENTRYPOINT
# ======================================================================


def main() -> None:
    test_selection_contract_has_no_financial_fields()
    test_selection_contract_is_frozen()
    test_selection_contract_rejects_bad_construction()

    test_selection_prompt_lists_every_tool()
    test_selection_prompt_forbids_answering()
    test_selection_prompt_includes_the_question()

    test_parses_clean_json()
    test_unwraps_a_fenced_block()
    test_rejects_prose()
    test_rejects_unexpected_keys()
    test_rejects_non_object_arguments()
    test_does_not_fuzzy_match_tool_names()

    test_valid_selections_pass()
    test_invalid_selections_fail()
    test_rejection_reason_is_human_readable()

    test_ask_returns_real_numbers()
    test_ask_passes_arguments_through()
    test_ask_evidence_for_a_real_transaction()
    test_data_is_always_attached_on_success()

    test_different_models_produce_identical_data()
    test_a_lying_model_cannot_corrupt_the_data()
    test_large_fabricated_financial_number_is_rejected()
    test_grounded_numeric_answer_is_accepted()
    test_repeated_questions_return_identical_data()

    test_selection_failure_is_honest()
    test_garbage_selection_is_rejected()
    test_hallucinated_tool_is_rejected()
    test_hallucinated_txn_id_is_honest()
    test_phrasing_failure_does_not_lose_the_answer()
    test_fallback_renders_every_tool()
    test_none_selection_is_a_clean_refusal()
    test_missing_context_is_reported_not_improvised()
    test_empty_question_is_rejected()

    test_explain_still_works_without_a_query_context()
    test_ask_answer_is_json_serialisable()

    print("Phase 6 Step 3 agent ask() tests passed.")


if __name__ == "__main__":
    main()