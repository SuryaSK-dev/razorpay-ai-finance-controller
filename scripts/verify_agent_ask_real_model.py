# scripts/verify_agent_ask_real_model.py
"""
Phase 6 Step 4 — Real-model verification of controller.ask().

WHAT THIS PROVES
----------------
That a REAL Gemini model can drive the agent loop -- choose a tool from
a natural-language question, and phrase the result -- without changing
a single number.

The central assertion, made for every question:

    answer.data == getattr(context, tool_used)(**tool_arguments)

The model's involvement must produce byte-identical data to calling the
tool directly. If a live model can shift a number, the whole
architecture is decorative.

WHAT THIS IS NOT
----------------
This is a SMOKE TEST, not an evaluation.

Five questions cannot measure tool-selection accuracy. Reporting "5/5
correct" as a benchmark would be exactly the mistake recorded in
FAILURE_LOG.md section 32 -- publishing a number that does not measure
what it claims to. A proper selection-accuracy evaluation needs a
held-out question set with pre-labelled expected tools, which is
Upgrade E work.

What five questions CAN establish is that the loop runs end to end
against a real provider, that the data invariant holds, and roughly
where the tool boundaries confuse a real model.

EXPECTED TOOLS ARE PREDICTIONS, NOT REQUIREMENTS
------------------------------------------------
Each question carries the tool a person would expect. A mismatch is
recorded as a finding, not an assertion failure, because:

  - Some questions are genuinely ambiguous. "Why is TXN_x unresolved?"
    could route to get_exceptions or get_evidence, and both return
    something true.
  - A wrong tool gives a true answer to the wrong question. That is
    visible and fixable by sharpening when_not_to_use in the registry.
  - A wrong NUMBER would be invisible, which is why that IS asserted.

The script fails only on the data invariant. Selection mismatches are
reported honestly and the run continues.

COST
----
Ten Gemini calls: one selection plus one phrasing per question.

Run:
    python scripts/verify_agent_ask_real_model.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.agent.config import load_agent_config
from src.agent.controller import FinanceControllerAgent
from src.agent.providers.gemini_provider import GeminiProvider
from src.agent.tools.query_tools import BatchQueryContext


ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
OUTPUT_PATH = ROOT / "data" / "eval" / "agent_ask_real_model.json"


# Each case: the question, and the tool a person would expect it to
# reach. `expected_tool=None` means the question is outside the tool
# set and should produce a clean refusal rather than a forced choice.
CASES = [
    {
        "id": "Q1",
        "question": "What is the match rate for this batch?",
        "expected_tool": "get_match_rate",
        "why": "Direct question about batch-level results.",
    },
    {
        "id": "Q2",
        "question": "Which transactions could you not resolve?",
        "expected_tool": "get_exceptions",
        "why": "Asks for the exception list, not the counts.",
    },
    {
        "id": "Q3",
        "question": "Show me everything that needs human review.",
        "expected_tool": "get_exceptions",
        "why": (
            "Should reach get_exceptions WITH a status filter. Tests "
            "whether the model supplies the optional argument rather "
            "than returning everything."
        ),
    },
    {
        "id": "Q4",
        "question": None,          # filled in at runtime with a real ID
        "expected_tool": "get_evidence",
        "why": (
            "Names a specific transaction. The genuinely ambiguous "
            "case -- get_exceptions would also return something true, "
            "so this tests whether when_not_to_use is doing its job."
        ),
    },
    {
        "id": "Q5",
        "question": "How fast did the pipeline process this batch?",
        "expected_tool": "get_throughput_report",
        "why": "Performance rather than correctness.",
    },
    {
        "id": "Q6",
        "question": "What is the weather in Bangalore today?",
        "expected_tool": None,
        "why": (
            "Outside the tool set entirely. Should refuse cleanly "
            "rather than force a wrong tool."
        ),
    },
]


def verify_data_invariant(
    context: BatchQueryContext,
    answer,
) -> tuple[bool, str]:
    """
    THE ASSERTION THAT MATTERS.

    Re-run the tool the model chose, with the arguments it supplied,
    directly against the context. The result must be identical to what
    ask() returned.

    If these ever differ, the model influenced the answer, and every
    claim this project makes about deterministic financial truth is
    false.
    """
    if answer.data is None:
        return True, "no data (refusal or error path)"

    direct = getattr(context, answer.tool_used)(**answer.tool_arguments)

    if direct == answer.data:
        return True, "identical to direct tool call"

    return False, (
        "DATA DIVERGED between agent path and direct call -- the model "
        "influenced the result"
    )


def main() -> None:
    print("=" * 72)
    print("PHASE 6 STEP 4 — REAL-MODEL VERIFICATION OF ask()")
    print("=" * 72)
    print()
    print("Smoke test, not an evaluation. Five questions cannot measure")
    print("selection accuracy; see Upgrade E for that.")
    print()

    config = load_agent_config()
    provider = GeminiProvider(config)
    context = BatchQueryContext(raw_dir=RAW_DIR)
    agent = FinanceControllerAgent(provider.as_callable(), context)

    print(f"Model: {config.gemini_model}")
    print(f"Batch: {len(context.decisions)} records")
    print()

    # Fill Q4 with a transaction that actually has an exception, so the
    # question is realistic rather than asking about a clean record.
    exceptions = context.get_exceptions()["exceptions"]
    sample_txn = (
        exceptions[0]["txn_id"] if exceptions
        else context.decisions[0].txn_id
    )

    for case in CASES:
        if case["question"] is None:
            case["question"] = (
                f"Why is {sample_txn} not fully matched?"
            )

    results = []
    invariant_failures = []
    selection_mismatches = []

    for case in CASES:
        print("-" * 72)
        print(f"{case['id']}: {case['question']}")

        answer = agent.ask(case["question"])

        # last_response holds only the MOST RECENT provider call, so
        # after ask() it reflects the phrasing call, not the selection
        # call. Recorded per question rather than summed, since the
        # selection call's usage is already overwritten by this point.
        last = provider.last_response

        ok, invariant_note = verify_data_invariant(context, answer)

        selected = answer.tool_used
        expected = case["expected_tool"]

        # A refusal is recorded as tool None so it compares cleanly
        # against expected_tool=None.
        if answer.answer_source == "no_tool":
            selected = None

        selection_matched = selected == expected

        if not ok:
            invariant_failures.append(case["id"])

        if not selection_matched:
            selection_mismatches.append({
                "id": case["id"],
                "expected": expected,
                "selected": selected,
            })

        print(f"  tool selected   : {selected}")
        print(f"  tool expected   : {expected}")
        print(f"  selection match : {'yes' if selection_matched else 'NO'}")
        print(f"  arguments       : {answer.tool_arguments}")
        print(f"  answer source   : {answer.answer_source}")
        print(f"  data invariant  : {'PASS' if ok else 'FAIL'} "
              f"({invariant_note})")
        print(f"  answer          : {answer.answer[:200]}")

        results.append({
            "id": case["id"],
            "question": case["question"],
            "expected_tool": expected,
            "selected_tool": selected,
            "selection_matched": selection_matched,
            "why_expected": case["why"],
            "tool_arguments": answer.tool_arguments,
            "answer_source": answer.answer_source,
            "answer": answer.answer,
            "data_invariant_held": ok,
            "data_invariant_note": invariant_note,
            "selection_latency_seconds": answer.agent_metadata.get(
                "selection_latency_seconds"
            ),
            "phrasing_latency_seconds": answer.agent_metadata.get(
                "phrasing_latency_seconds"
            ),
            "phrasing_error": answer.agent_metadata.get("phrasing_error"),
            "last_call_input_tokens": (
                last.input_tokens if last else None
            ),
            "last_call_output_tokens": (
                last.output_tokens if last else None
            ),
        })

        print()

    # ------------------------------------------------------------------
    # SUMMARY
    # ------------------------------------------------------------------

    matched = sum(1 for r in results if r["selection_matched"])
    llm_phrased = sum(
        1 for r in results if r["answer_source"] == "llm"
    )

    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"Questions asked           : {len(results)}")
    print(f"Data invariant held       : "
          f"{len(results) - len(invariant_failures)}/{len(results)}")
    print(f"Selection matched expected: {matched}/{len(results)}")
    print(f"Phrased by the model      : {llm_phrased}/{len(results)}")

    if selection_mismatches:
        print()
        print("SELECTION MISMATCHES (findings, not failures):")
        for mismatch in selection_mismatches:
            print(f"  {mismatch['id']}: expected "
                  f"{mismatch['expected']}, selected "
                  f"{mismatch['selected']}")
        print()
        print("  A wrong tool gives a TRUE answer to the WRONG")
        print("  question. Fixable by sharpening when_not_to_use in")
        print("  registry.py. Recorded rather than asserted, because")
        print("  some of these questions are genuinely ambiguous.")

    report = {
        "stage": "phase-6-step-4",
        "kind": "smoke_test",
        "not_an_evaluation": (
            "Five questions cannot measure selection accuracy. This "
            "verifies the loop runs against a real provider and that "
            "the data invariant holds. Selection accuracy as a "
            "measured number requires a held-out labelled question "
            "set."
        ),
        "model": config.gemini_model,
        "batch_records": len(context.decisions),
        "questions": len(results),
        "data_invariant_held": len(results) - len(invariant_failures),
        "selection_matched_expected": matched,
        "phrased_by_model": llm_phrased,
        "selection_mismatches": selection_mismatches,
        "cases": results,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)

    print()
    print(f"Artifact: {OUTPUT_PATH}")

    # ------------------------------------------------------------------
    # The ONLY hard failure condition.
    # ------------------------------------------------------------------

    if invariant_failures:
        print()
        print("=" * 72)
        print("FAIL — THE DATA INVARIANT WAS VIOLATED")
        print("=" * 72)
        print(f"Cases: {invariant_failures}")
        print()
        print("A real model changed a number between the agent path and")
        print("a direct tool call. This is the failure mode the entire")
        print("architecture exists to prevent. Do not proceed until it")
        print("is understood.")
        raise SystemExit(1)

    print()
    print("Data invariant held on every question.")
    print("The real model chose tools and wrote prose. It did not")
    print("change a single number.")
    print()
    print("Phase 6 Step 4: PASS")


if __name__ == "__main__":
    main()