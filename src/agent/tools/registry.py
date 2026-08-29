# src/agent/tools/registry.py
"""
Phase 6 Step 2 — Tool registry and dispatcher.

WHAT THIS IS
------------
The layer that describes the read-only query tools to a language model,
and routes a chosen tool name plus arguments to the real function.

    ToolSpec        -- name, description, parameter schema
    TOOL_REGISTRY   -- the four tools the model may choose from
    dispatch()      -- name + args -> real BatchQueryContext method

WHY A REGISTRY RATHER THAN LETTING THE MODEL CALL METHODS
---------------------------------------------------------
The model never touches `BatchQueryContext`. It emits a tool NAME and
an argument dict, and this module decides whether that is a thing the
model is allowed to ask for. That indirection is the whole point: it
turns "what the model said" into "what the system permits" at one
inspectable place.

Three failure modes this has to handle, all of which a real model does
produce:

  1. An invented tool name -- `get_refunds`, `list_all`, a typo.
  2. An invented argument -- `{"limit": 5}` on a tool with no limit.
  3. A plausible-but-wrong argument value -- `{"status": "PENDING"}`.

Every one of these RAISES. None of them falls back to a default tool,
coerces the value, or drops the unknown key.

The reason is not tidiness. If `dispatch()` quietly ignored an unknown
argument, a model asking for "the first 5 exceptions" would receive all
of them and the phrasing layer would then describe the full list as
though it were five. If it fell back to a default tool, an operator
would get a confident answer to a question they did not ask. Both
produce output that looks correct and is not, which is worse than an
error.

WHAT THE REGISTRY CANNOT DO
---------------------------
There is no tool here that computes a financial outcome, and there must
never be one. `dispatch()` can only reach methods that exist on
`BatchQueryContext`, and Step 1 asserts structurally that no such
method exists there. `test_tool_registry.py` asserts the same property
at this layer, so adding a mutating tool would have to defeat two
independent guards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from src.agent.tools.query_tools import (
    BatchQueryContext,
    TxnNotFoundError,
)
from src.models import DecisionStatus


class UnknownToolError(LookupError):
    """
    The model named a tool that does not exist.

    A distinct type because the layer above must be able to tell this
    apart from a tool that ran and failed. An invented tool name means
    the model misunderstood what is available; a tool error means
    something broke. Those deserve different responses to the operator.
    """


class InvalidToolArgumentsError(ValueError):
    """
    The model supplied arguments the tool cannot accept.

    Covers unknown keys, missing required keys, and values outside the
    permitted set. All three are rejected rather than repaired.
    """


@dataclass(frozen=True)
class ToolSpec:
    """
    One tool the model may choose.

    `description` and `when_not_to_use` are written FOR THE MODEL, not
    for a developer reading the source. They go into the prompt
    verbatim.

    `when_not_to_use` exists because these four tools overlap in ways
    that are obvious to a person and not to a model. "Why is TXN_00042
    unresolved?" could route to `get_exceptions` or `get_evidence`;
    both would return something true, but only one answers the
    question. Stating the boundary costs nothing at runtime and
    removes the ambiguity.
    """

    name: str
    description: str
    when_not_to_use: str
    parameters: dict[str, "ParamSpec"] = field(default_factory=dict)

    def required_params(self) -> set[str]:
        return {
            name for name, spec in self.parameters.items()
            if spec.required
        }

    def to_prompt_block(self) -> str:
        """Render this tool for inclusion in a model prompt."""
        lines = [f"- {self.name}", f"    {self.description}"]

        if self.parameters:
            lines.append("    Arguments:")
            for param_name, spec in self.parameters.items():
                requirement = "required" if spec.required else "optional"
                lines.append(
                    f"      {param_name} ({spec.type}, {requirement}): "
                    f"{spec.description}"
                )
                if spec.allowed_values:
                    allowed = ", ".join(sorted(spec.allowed_values))
                    lines.append(f"        one of: {allowed}")
        else:
            lines.append("    Arguments: none")

        lines.append(f"    Do NOT use when: {self.when_not_to_use}")
        return "\n".join(lines)


@dataclass(frozen=True)
class ParamSpec:
    """One argument a tool accepts."""

    type: str
    description: str
    required: bool = False
    allowed_values: frozenset[str] | None = None


# ======================================================================
# THE REGISTRY
# ======================================================================
#
# Descriptions are deliberately concrete about WHAT IS RETURNED rather
# than what the tool is "for". A model choosing between four options
# does better with "returns counts by status" than with "reports on
# reconciliation health".

TOOL_REGISTRY: dict[str, ToolSpec] = {

    "get_match_rate": ToolSpec(
        name="get_match_rate",
        description=(
            "Returns the batch-level match rate: total records, how "
            "many fully matched, the percentage, and breakdowns by "
            "decision status and by matching confidence tier. Use for "
            "any question about overall results, how the batch did, "
            "or how many matched."
        ),
        when_not_to_use=(
            "the operator asks about a specific transaction, or wants "
            "the list of failures rather than the counts"
        ),
    ),

    "get_exceptions": ToolSpec(
        name="get_exceptions",
        description=(
            "Returns every record that did NOT fully match, itemised "
            "with its status, exception code, all reason codes, and "
            "confidence. Optionally filtered to one status. This is "
            "the complete list, never a sample. Use for any question "
            "about what failed, what needs review, or what could not "
            "be resolved."
        ),
        when_not_to_use=(
            "the operator names one transaction and wants the reason "
            "for it specifically -- use get_evidence for that"
        ),
        parameters={
            "status": ParamSpec(
                type="string",
                description=(
                    "Restrict to one decision status. Omit for all "
                    "exceptions."
                ),
                required=False,
                allowed_values=frozenset(
                    s.value for s in DecisionStatus
                ),
            ),
        },
    ),

    "get_evidence": ToolSpec(
        name="get_evidence",
        description=(
            "Returns the full audit trail for ONE transaction: its "
            "status, exception code, reason codes, which decision rule "
            "fired, which sources matched, the tax verification state, "
            "and the underlying match signals. Use when the operator "
            "names a specific transaction ID."
        ),
        when_not_to_use=(
            "no specific transaction ID was given, or the operator "
            "wants a summary across the batch"
        ),
        parameters={
            "txn_id": ParamSpec(
                type="string",
                description=(
                    "The transaction ID, e.g. TXN_00042. Must be a "
                    "real ID from this batch."
                ),
                required=True,
            ),
        },
    ),

    "get_throughput_report": ToolSpec(
        name="get_throughput_report",
        description=(
            "Returns measured processing throughput from the recorded "
            "benchmark: batch sizes tested, records per second, and "
            "per-stage timings. Use for questions about speed, "
            "performance, or how fast the batch ran."
        ),
        when_not_to_use=(
            "the question is about correctness or results rather than "
            "speed"
        ),
    ),
}


# ======================================================================
# DISPATCH
# ======================================================================

def _bind(context: BatchQueryContext, name: str) -> Callable[..., Any]:
    """
    Resolve a registered tool name to the real bound method.

    getattr is used rather than a hand-written mapping so the registry
    cannot drift from the implementation: a tool registered under a
    name that does not exist on BatchQueryContext fails here, and
    `test_every_registered_tool_exists` catches it at test time.
    """
    method = getattr(context, name, None)

    if method is None or not callable(method):
        raise UnknownToolError(
            f"Tool {name!r} is registered but not implemented on "
            "BatchQueryContext."
        )

    return method


def validate_arguments(
    spec: ToolSpec,
    arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Check the model's arguments against the tool's schema.

    Rejects, in order:
      - unknown keys
      - missing required keys
      - values outside an allowed set

    Nothing is coerced and nothing is dropped. A model that asks for
    `{"limit": 5}` on a tool with no limit must be told the argument
    does not exist, not silently handed the full list which the
    phrasing layer then describes as five.
    """
    arguments = dict(arguments or {})

    unknown = set(arguments) - set(spec.parameters)
    if unknown:
        raise InvalidToolArgumentsError(
            f"{spec.name}: unknown argument(s) {sorted(unknown)}. "
            f"Accepts: {sorted(spec.parameters) or 'none'}."
        )

    missing = spec.required_params() - set(arguments)
    if missing:
        raise InvalidToolArgumentsError(
            f"{spec.name}: missing required argument(s) "
            f"{sorted(missing)}."
        )

    for key, value in arguments.items():
        param = spec.parameters[key]

        if value is None and not param.required:
            continue

        if param.allowed_values and value not in param.allowed_values:
            raise InvalidToolArgumentsError(
                f"{spec.name}: {key}={value!r} is not permitted. "
                f"One of: {sorted(param.allowed_values)}."
            )

        if param.type == "string" and not isinstance(value, str):
            raise InvalidToolArgumentsError(
                f"{spec.name}: {key} must be a string, got "
                f"{type(value).__name__}."
            )

    return arguments


def dispatch(
    context: BatchQueryContext,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Route a model-chosen tool name and arguments to the real function.

    Returns a result envelope rather than the bare tool output:

        {"ok": True,  "tool": ..., "arguments": ..., "result": {...}}
        {"ok": False, "tool": ..., "arguments": ..., "error": "...",
         "error_type": "..."}

    The envelope exists so the phrasing layer above can tell success
    from failure WITHOUT inspecting the shape of the payload. A model
    handed a bare dict on success and a bare dict on failure would have
    to guess, and guessing is how a failure gets phrased as a result.

    Expected, model-caused failures -- unknown tool, bad arguments,
    hallucinated transaction ID -- are returned as ok=False envelopes,
    because they are normal conversational outcomes the agent should
    explain rather than crash on.

    Unexpected failures are NOT caught. A bug in the pipeline should
    surface as a stack trace during development, not be smoothed into
    a polite sentence for an operator.
    """
    spec = TOOL_REGISTRY.get(tool_name)

    if spec is None:
        return {
            "ok": False,
            "tool": tool_name,
            "arguments": arguments or {},
            "error": (
                f"No tool named {tool_name!r}. Available: "
                f"{sorted(TOOL_REGISTRY)}."
            ),
            "error_type": "UnknownToolError",
        }

    try:
        validated = validate_arguments(spec, arguments)
    except InvalidToolArgumentsError as exc:
        return {
            "ok": False,
            "tool": tool_name,
            "arguments": arguments or {},
            "error": str(exc),
            "error_type": "InvalidToolArgumentsError",
        }

    method = _bind(context, tool_name)

    try:
        result = method(**validated)
    except TxnNotFoundError as exc:
        # The model proposed an ID that is not in the batch. This is an
        # expected outcome, not a defect -- the agent should say so
        # plainly rather than inventing a record.
        return {
            "ok": False,
            "tool": tool_name,
            "arguments": validated,
            "error": str(exc),
            "error_type": "TxnNotFoundError",
        }
    except ValueError as exc:
        # query_tools raises ValueError for an unknown status. Caught
        # here so the same class of model mistake produces the same
        # envelope regardless of which layer noticed it.
        return {
            "ok": False,
            "tool": tool_name,
            "arguments": validated,
            "error": str(exc),
            "error_type": "InvalidToolArgumentsError",
        }

    return {
        "ok": True,
        "tool": tool_name,
        "arguments": validated,
        "result": result,
    }


# ======================================================================
# PROMPT RENDERING
# ======================================================================

def render_tool_catalogue() -> str:
    """
    Render every tool as a block for the model prompt.

    Kept here rather than in the controller so the prompt text and the
    registry cannot drift: adding a tool automatically adds it to the
    catalogue the model sees.
    """
    blocks = [
        TOOL_REGISTRY[name].to_prompt_block()
        for name in sorted(TOOL_REGISTRY)
    ]
    return "\n\n".join(blocks)


def tool_names() -> list[str]:
    """Sorted list of valid tool names, for validation and prompts."""
    return sorted(TOOL_REGISTRY)