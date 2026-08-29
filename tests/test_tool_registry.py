# tests/test_tool_registry.py
"""
Phase 6 Step 2 — tool registry and dispatcher tests.

The registry is where "what the model said" becomes "what the system
permits". The properties that matter are therefore mostly about what
the dispatcher REFUSES to do:

    1. Registry cannot drift from the implementation -- every
       registered tool must exist on BatchQueryContext.
    2. An invented tool name is rejected, never defaulted.
    3. An invented argument is rejected, never dropped.
    4. A bad argument value is rejected, never coerced.
    5. A hallucinated transaction ID produces an honest failure
       envelope, not a fabricated record.
    6. Success and failure are distinguishable without inspecting the
       payload shape.
    7. No tool exists whose name suggests it mutates anything.

No LLM, no network, no API key.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.models import DecisionStatus
from src.agent.tools.query_tools import BatchQueryContext
from src.agent.tools.registry import (
    TOOL_REGISTRY,
    ToolSpec,
    ParamSpec,
    UnknownToolError,
    InvalidToolArgumentsError,
    dispatch,
    validate_arguments,
    render_tool_catalogue,
    tool_names,
)


ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"


def build_context() -> BatchQueryContext:
    return BatchQueryContext(raw_dir=RAW_DIR)


# ======================================================================
# REGISTRY INTEGRITY
# ======================================================================

def test_registry_is_not_empty():
    assert TOOL_REGISTRY
    assert len(TOOL_REGISTRY) == 4


def test_every_registered_tool_exists_on_the_context():
    """
    The registry must not describe a tool the implementation does not
    have. If it did, the model would be told it can ask for something
    that fails at dispatch time.
    """
    context = build_context()

    for name in TOOL_REGISTRY:
        method = getattr(context, name, None)
        assert method is not None, f"{name} not on BatchQueryContext"
        assert callable(method), f"{name} is not callable"


def test_registry_keys_match_spec_names():
    for key, spec in TOOL_REGISTRY.items():
        assert key == spec.name, (
            f"registered under {key!r} but spec.name is {spec.name!r}"
        )


def test_every_tool_has_a_description_and_a_boundary():
    """
    `when_not_to_use` is not optional. These four tools overlap, and a
    tool described without its boundary is a tool the model will
    misroute to.
    """
    for spec in TOOL_REGISTRY.values():
        assert len(spec.description) > 40, f"{spec.name}: thin description"
        assert len(spec.when_not_to_use) > 10, (
            f"{spec.name}: missing when_not_to_use"
        )


def test_status_parameter_allows_exactly_the_real_statuses():
    """
    If the allowed set drifts from DecisionStatus, the model would be
    told about a status the engine cannot produce.
    """
    spec = TOOL_REGISTRY["get_exceptions"]
    allowed = spec.parameters["status"].allowed_values

    assert allowed == frozenset(s.value for s in DecisionStatus)


def test_get_evidence_requires_a_txn_id():
    spec = TOOL_REGISTRY["get_evidence"]
    assert spec.required_params() == {"txn_id"}


def test_tools_with_no_parameters_declare_none():
    for name in ("get_match_rate", "get_throughput_report"):
        assert TOOL_REGISTRY[name].parameters == {}


def test_registry_exposes_no_mutation_tool():
    """
    Second structural guard on the same property Step 1 asserts. A
    mutating tool would have to defeat both.
    """
    forbidden = (
        "re_evaluate", "reevaluate", "rematch", "recompute",
        "set_", "update_", "override", "approve", "resolve_",
        "delete", "write", "modify",
    )

    for name in TOOL_REGISTRY:
        for pattern in forbidden:
            assert pattern not in name.lower(), (
                f"{name} looks like a mutation tool. The registry is "
                "read-only by design."
            )


# ======================================================================
# ARGUMENT VALIDATION
# ======================================================================

def test_unknown_argument_is_rejected_not_dropped():
    """
    THE IMPORTANT ONE. If an unknown key were silently dropped, a model
    asking for "the first 5 exceptions" would receive all of them, and
    the phrasing layer would then describe the full list as five.
    """
    spec = TOOL_REGISTRY["get_exceptions"]

    try:
        validate_arguments(spec, {"limit": 5})
        assert False, "expected InvalidToolArgumentsError"
    except InvalidToolArgumentsError as exc:
        assert "limit" in str(exc)


def test_missing_required_argument_is_rejected():
    spec = TOOL_REGISTRY["get_evidence"]

    try:
        validate_arguments(spec, {})
        assert False, "expected InvalidToolArgumentsError"
    except InvalidToolArgumentsError as exc:
        assert "txn_id" in str(exc)


def test_value_outside_allowed_set_is_rejected():
    spec = TOOL_REGISTRY["get_exceptions"]

    try:
        validate_arguments(spec, {"status": "PENDING"})
        assert False, "expected InvalidToolArgumentsError"
    except InvalidToolArgumentsError as exc:
        assert "PENDING" in str(exc)


def test_wrong_type_is_rejected_not_coerced():
    spec = TOOL_REGISTRY["get_evidence"]

    try:
        validate_arguments(spec, {"txn_id": 42})
        assert False, "expected InvalidToolArgumentsError"
    except InvalidToolArgumentsError as exc:
        assert "string" in str(exc).lower()


def test_valid_arguments_pass_through_unchanged():
    spec = TOOL_REGISTRY["get_exceptions"]
    validated = validate_arguments(spec, {"status": "HUMAN_REVIEW"})
    assert validated == {"status": "HUMAN_REVIEW"}


def test_omitting_an_optional_argument_is_fine():
    spec = TOOL_REGISTRY["get_exceptions"]
    assert validate_arguments(spec, None) == {}
    assert validate_arguments(spec, {}) == {}


# ======================================================================
# DISPATCH -- SUCCESS
# ======================================================================

def test_dispatch_match_rate():
    context = build_context()
    envelope = dispatch(context, "get_match_rate")

    assert envelope["ok"] is True
    assert envelope["tool"] == "get_match_rate"
    assert envelope["result"]["total_records"] > 0


def test_dispatch_exceptions_unfiltered():
    context = build_context()
    envelope = dispatch(context, "get_exceptions")

    assert envelope["ok"] is True
    assert envelope["result"]["count"] == len(
        envelope["result"]["exceptions"]
    )


def test_dispatch_exceptions_filtered():
    context = build_context()
    envelope = dispatch(
        context, "get_exceptions", {"status": "HUMAN_REVIEW"}
    )

    assert envelope["ok"] is True
    for item in envelope["result"]["exceptions"]:
        assert item["status"] == "HUMAN_REVIEW"


def test_dispatch_evidence():
    context = build_context()
    txn_id = context.decisions[0].txn_id

    envelope = dispatch(context, "get_evidence", {"txn_id": txn_id})

    assert envelope["ok"] is True
    assert envelope["result"]["txn_id"] == txn_id


def test_dispatch_throughput():
    context = build_context()
    envelope = dispatch(context, "get_throughput_report")

    assert envelope["ok"] is True
    assert "available" in envelope["result"]


def test_dispatch_result_matches_direct_call():
    """
    Dispatch must be a pass-through. If it transformed the result, the
    model would see something different from what the tool produced.
    """
    context = build_context()

    assert (
        dispatch(context, "get_match_rate")["result"]
        == context.get_match_rate()
    )


# ======================================================================
# DISPATCH -- FAILURE ENVELOPES
# ======================================================================

def test_unknown_tool_name_is_rejected_not_defaulted():
    """
    A model that invents `get_refunds` must be told no such tool
    exists. Falling back to a default would hand the operator a
    confident answer to a question nobody asked.
    """
    context = build_context()

    for fake in ("get_refunds", "list_all", "get_match_rat", ""):
        envelope = dispatch(context, fake)

        assert envelope["ok"] is False
        assert envelope["error_type"] == "UnknownToolError"
        assert "result" not in envelope


def test_hallucinated_txn_id_returns_honest_failure():
    """
    Not a crash, and emphatically not a fabricated record.
    """
    context = build_context()

    envelope = dispatch(
        context, "get_evidence", {"txn_id": "TXN_99999"}
    )

    assert envelope["ok"] is False
    assert envelope["error_type"] == "TxnNotFoundError"
    assert "result" not in envelope
    assert "TXN_99999" in envelope["error"]


def test_bad_status_from_the_tool_layer_becomes_an_envelope():
    """
    query_tools raises ValueError for an unknown status; the registry
    also validates it. Either way the model-facing outcome is the same
    envelope shape.
    """
    context = build_context()

    envelope = dispatch(
        context, "get_exceptions", {"status": "NOT_A_STATUS"}
    )

    assert envelope["ok"] is False
    assert envelope["error_type"] == "InvalidToolArgumentsError"


def test_unknown_argument_produces_a_failure_envelope():
    context = build_context()

    envelope = dispatch(
        context, "get_exceptions", {"limit": 5}
    )

    assert envelope["ok"] is False
    assert "limit" in envelope["error"]


def test_success_and_failure_are_distinguishable_without_the_payload():
    """
    The phrasing layer must not have to guess from the shape. `ok` is
    always present and always a bool.
    """
    context = build_context()

    good = dispatch(context, "get_match_rate")
    bad = dispatch(context, "get_nothing")

    assert good["ok"] is True and "result" in good
    assert bad["ok"] is False and "error" in bad
    assert isinstance(good["ok"], bool)
    assert isinstance(bad["ok"], bool)


def test_every_envelope_is_json_serialisable():
    """
    Envelopes go into prompts. A leaked enum or Decimal would fail
    there rather than here.
    """
    context = build_context()
    txn_id = context.decisions[0].txn_id

    envelopes = [
        dispatch(context, "get_match_rate"),
        dispatch(context, "get_exceptions"),
        dispatch(context, "get_evidence", {"txn_id": txn_id}),
        dispatch(context, "get_throughput_report"),
        dispatch(context, "get_nothing"),
        dispatch(context, "get_evidence", {"txn_id": "TXN_99999"}),
    ]

    for envelope in envelopes:
        json.dumps(envelope)


# ======================================================================
# PROMPT RENDERING
# ======================================================================

def test_catalogue_lists_every_tool():
    catalogue = render_tool_catalogue()

    for name in TOOL_REGISTRY:
        assert name in catalogue


def test_catalogue_includes_boundaries_and_allowed_values():
    catalogue = render_tool_catalogue()

    assert "Do NOT use when:" in catalogue
    assert "HUMAN_REVIEW" in catalogue      # allowed status values
    assert "txn_id" in catalogue            # required argument


def test_tool_names_are_sorted_and_complete():
    assert tool_names() == sorted(TOOL_REGISTRY)


def test_catalogue_cannot_drift_from_the_registry():
    """
    Rendering is derived from TOOL_REGISTRY, so a new tool appears in
    the prompt automatically rather than needing a second edit.
    """
    extra = ToolSpec(
        name="get_example_only",
        description="A" * 50,
        when_not_to_use="B" * 20,
        parameters={"x": ParamSpec(type="string", description="x")},
    )

    TOOL_REGISTRY[extra.name] = extra
    try:
        assert extra.name in render_tool_catalogue()
        assert extra.name in tool_names()
    finally:
        del TOOL_REGISTRY[extra.name]

    assert "get_example_only" not in render_tool_catalogue()


def main() -> None:
    test_registry_is_not_empty()
    test_every_registered_tool_exists_on_the_context()
    test_registry_keys_match_spec_names()
    test_every_tool_has_a_description_and_a_boundary()
    test_status_parameter_allows_exactly_the_real_statuses()
    test_get_evidence_requires_a_txn_id()
    test_tools_with_no_parameters_declare_none()
    test_registry_exposes_no_mutation_tool()
    test_unknown_argument_is_rejected_not_dropped()
    test_missing_required_argument_is_rejected()
    test_value_outside_allowed_set_is_rejected()
    test_wrong_type_is_rejected_not_coerced()
    test_valid_arguments_pass_through_unchanged()
    test_omitting_an_optional_argument_is_fine()
    test_dispatch_match_rate()
    test_dispatch_exceptions_unfiltered()
    test_dispatch_exceptions_filtered()
    test_dispatch_evidence()
    test_dispatch_throughput()
    test_dispatch_result_matches_direct_call()
    test_unknown_tool_name_is_rejected_not_defaulted()
    test_hallucinated_txn_id_returns_honest_failure()
    test_bad_status_from_the_tool_layer_becomes_an_envelope()
    test_unknown_argument_produces_a_failure_envelope()
    test_success_and_failure_are_distinguishable_without_the_payload()
    test_every_envelope_is_json_serialisable()
    test_catalogue_lists_every_tool()
    test_catalogue_includes_boundaries_and_allowed_values()
    test_tool_names_are_sorted_and_complete()
    test_catalogue_cannot_drift_from_the_registry()

    print("Phase 6 Step 2 tool registry tests passed.")


if __name__ == "__main__":
    main()