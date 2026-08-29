# src/agent/tool_selection.py
"""
Phase 6 Step 3a — Tool selection contract, prompt, and parser.

WHAT THIS IS
------------
The piece that turns a natural-language question into a validated
choice of tool. It is deliberately separate from `controller.py` so
that the selection step can be tested without running a conversation.

    ToolSelection   -- frozen contract: which tool, which arguments
    build_selection_prompt()  -- the prompt the model sees
    parse_selection()         -- model text -> ToolSelection
    validate_selection()      -- is this a tool that actually exists

WHY A FROZEN CONTRACT AGAIN
---------------------------
Same reason as `NarrationExtraction` in Phase 5: a bare dict returned
from a model is not a boundary. `ToolSelection` has exactly two fields
and no capacity to carry a financial fact. The model can say "call
get_evidence with txn_id TXN_00042". It cannot say "the status is
MATCHED" through this type, because there is nowhere to put it.

WHAT THE MODEL IS AND IS NOT DECIDING HERE
------------------------------------------
Deciding: which of four questions the operator is asking.
Not deciding: the answer.

If the model picks the wrong tool, the operator gets a true answer to
the wrong question -- annoying, and visible. If the model could
influence the answer itself, the operator could get a false answer to
the right question, which is not visible. The first failure mode is
acceptable; the second is what this whole architecture exists to
prevent.

PARSING IS STRICT ON PURPOSE
----------------------------
`parse_selection()` rejects anything it cannot read cleanly rather than
guessing. A model that returns prose around its JSON gets one lenient
affordance -- a fenced block is unwrapped -- and nothing else. No
fuzzy tool-name matching, no defaulting to the "most likely" tool.

A wrong guess here produces a confident answer to a question nobody
asked, and the operator has no way to detect it. A rejection produces
"I could not determine which tool to use", which is honest.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from src.agent.tools.registry import (
    TOOL_REGISTRY,
    InvalidToolArgumentsError,
    UnknownToolError,
    render_tool_catalogue,
    tool_names,
    validate_arguments,
)


@dataclass(frozen=True)
class ToolSelection:
    """
    One model-chosen tool call.

    Frozen, and structurally incapable of carrying a financial fact:
    there is no field for status, amount, tax, confidence, or any
    exception code. The only things expressible are a tool name and
    its arguments.
    """

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name:
            raise ValueError("tool_name must be a non-empty string")
        if not isinstance(self.arguments, dict):
            raise ValueError("arguments must be a dict")


# ======================================================================
# PROMPT
# ======================================================================

_SELECTION_INSTRUCTIONS = """\
You are the tool-selection step of a financial reconciliation
assistant. A finance operator has asked a question about a batch of
reconciled transactions. Your only job is to choose which ONE tool
answers it.

You are NOT answering the question. You are NOT computing anything.
You do not have access to the data. Another part of the system will
run the tool you choose and return real numbers.

AVAILABLE TOOLS
---------------
{catalogue}

RULES
-----
- Choose exactly one tool from the list above. Use its exact name.
- Supply only arguments that the chosen tool declares. Do not invent
  arguments such as limits, sorting, or date ranges -- they do not
  exist and the call will be rejected.
- If the operator names a specific transaction ID, pass it exactly as
  written. Do not correct, complete, or invent an ID.
- If no tool fits the question, choose "none".

RESPOND WITH JSON ONLY
----------------------
No prose, no explanation, no markdown fences. Exactly this shape:

{{"tool_name": "<tool name or none>", "arguments": {{}}}}

Examples:

{{"tool_name": "get_match_rate", "arguments": {{}}}}
{{"tool_name": "get_exceptions", "arguments": {{"status": "HUMAN_REVIEW"}}}}
{{"tool_name": "get_evidence", "arguments": {{"txn_id": "TXN_00042"}}}}
{{"tool_name": "none", "arguments": {{}}}}

OPERATOR QUESTION
-----------------
{question}
"""


def build_selection_prompt(question: str) -> str:
    """
    Build the tool-selection prompt.

    The tool catalogue is rendered from TOOL_REGISTRY rather than
    written out here, so a new tool appears in the prompt
    automatically. A hand-maintained copy would drift, and the model
    would be told about tools that no longer exist -- or not told about
    ones that do.
    """
    return _SELECTION_INSTRUCTIONS.format(
        catalogue=render_tool_catalogue(),
        question=question.strip(),
    )


# ======================================================================
# PARSING
# ======================================================================

_FENCE = re.compile(
    r"^\s*```(?:json)?\s*(.*?)\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)

NO_TOOL = "none"


def parse_selection(raw: str) -> ToolSelection:
    """
    Parse model output into a ToolSelection.

    Raises on anything that cannot be read cleanly. The single leniency
    is unwrapping a ```json fenced block, because models add those
    reflexively and the content inside is still exact.

    Everything else is strict. In particular there is no fuzzy matching
    of tool names: "get_match_rates" is not silently corrected to
    "get_match_rate". A near-miss usually means the model was
    improvising, and improvising is the state in which it is least
    likely to have chosen correctly.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Model returned an empty tool selection")

    text = raw.strip()

    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Tool selection was not valid JSON: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError(
            f"Tool selection must be a JSON object, got "
            f"{type(payload).__name__}"
        )

    unexpected = set(payload) - {"tool_name", "arguments"}
    if unexpected:
        raise ValueError(
            f"Tool selection contains unexpected key(s) "
            f"{sorted(unexpected)}. Expected only tool_name and "
            "arguments."
        )

    tool_name = payload.get("tool_name")
    arguments = payload.get("arguments", {})

    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("tool_name missing or not a string")

    if arguments is None:
        arguments = {}

    if not isinstance(arguments, dict):
        raise ValueError(
            f"arguments must be an object, got "
            f"{type(arguments).__name__}"
        )

    return ToolSelection(tool_name=tool_name, arguments=arguments)


# ======================================================================
# VALIDATION
# ======================================================================

def validate_selection(selection: ToolSelection) -> bool:
    """
    Is this a selection the system will actually honour?

    Used as the `validate_fn` passed to `call_llm_bounded`, so a
    selection that fails here is rejected by the guardrail before it
    reaches dispatch -- the model's output never becomes an action.

    `none` is valid. It is the model correctly reporting that no tool
    fits, which is a better outcome than forcing a wrong one.
    """
    if selection.tool_name == NO_TOOL:
        return not selection.arguments

    spec = TOOL_REGISTRY.get(selection.tool_name)
    if spec is None:
        return False

    try:
        validate_arguments(spec, selection.arguments)
    except InvalidToolArgumentsError:
        return False

    return True


def selection_rejection_reason(selection: ToolSelection) -> str:
    """
    Why a selection was rejected, phrased for a person.

    Separate from `validate_selection` because the guardrail needs a
    bool and the operator needs a sentence. Keeping them apart avoids
    a validator that returns a string and is truthy when it fails.
    """
    if selection.tool_name == NO_TOOL:
        if selection.arguments:
            return "'none' was selected but arguments were supplied."
        return ""

    if selection.tool_name not in TOOL_REGISTRY:
        return (
            f"No tool named {selection.tool_name!r}. "
            f"Available: {tool_names()}."
        )

    try:
        validate_arguments(
            TOOL_REGISTRY[selection.tool_name], selection.arguments
        )
    except InvalidToolArgumentsError as exc:
        return str(exc)

    return ""