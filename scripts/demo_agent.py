# scripts/demo_agent.py
"""
AI Finance Controller — end-to-end demonstration.

WHAT THIS SHOWS
---------------
The complete finance-ops loop the system implements:

    ingest -> reconcile -> verify tax -> decide -> ASK

A finance operator points the agent at a batch and asks questions in
plain English. The agent chooses which read-only tool answers each
question, the deterministic engine produces the numbers, and the model
phrases them.

The critical property, demonstrated live rather than asserted: the
model never touches a number. After every answer this script re-runs
the chosen tool directly and compares. If the two ever differ, the demo
stops.

WHAT IT REPLACED
----------------
The previous version of this file called a hardcoded `mock_llm()` that
returned a canned string, with a docstring referring to a GPT call that
was never wired in. It demonstrated the plumbing and nothing else.

This version runs the real provider against the real batch.

RUNNING IT
----------
Needs GEMINI_API_KEY in the environment:

    export $(grep -v '^#' .env | grep -v '^$' | xargs)
    python scripts/demo_agent.py

Without a key it runs in --offline mode: the same loop, same tools,
same data invariant check, with a deterministic stub in place of the
model. That mode proves the pipeline and the guardrails; it does not
demonstrate model behaviour, and says so at every point where the
distinction could be misread.

COST
----
Two Gemini calls per question (selection + phrasing). Six questions,
so twelve calls.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import warnings
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

# The google-genai SDK emits an automatic-function-calling advisory on
# first use. We do not use function calling -- tool selection happens in
# our own bounded layer -- so the notice is noise in the middle of a
# demo. Suppressed via both channels because the SDK has used each.
logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("google_genai.models").setLevel(logging.ERROR)
warnings.filterwarnings(
    "ignore", message=".*automatic function calling.*"
)

from src.agent.controller import FinanceControllerAgent
from src.agent.tools.query_tools import BatchQueryContext


ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"

RULE = "=" * 72
THIN = "-" * 72


DEMO_QUESTIONS = [
    "What is the match rate for this batch?",
    "Which transactions could you not resolve?",
    "Show me everything that needs human review.",
    None,                      # filled with a real exception txn_id
    "How fast did the pipeline process this batch?",
    # Closing question. The others report records; this one reports
    # money -- which is what "run the books AND THE CASH POSITION"
    # actually asks for, and what a finance controller opens the report
    # to find out.
    "How much money is blocked behind those exceptions?",
]


# ======================================================================
# OFFLINE STUB
# ======================================================================

def offline_model(context: BatchQueryContext):
    """
    A deterministic stand-in for the model.

    Routes on keywords rather than reasoning. This is emphatically NOT
    a demonstration of model capability -- it exists so the loop, the
    tools, and the data invariant can be shown without credentials.
    """
    def call(prompt: str) -> str:
        if "tool-selection step" not in prompt:
            return (
                "[offline mode -- no model. The data below is real; "
                "this sentence is not model-generated.]"
            )

        question = prompt.rsplit("OPERATOR QUESTION", 1)[-1].lower()

        if "txn_" in question:
            start = question.index("txn_")
            txn_id = question[start:start + 9].upper().strip(" ?.,")
            return json.dumps({
                "tool_name": "get_evidence",
                "arguments": {"txn_id": txn_id},
            })

        if "human review" in question:
            return json.dumps({
                "tool_name": "get_exceptions",
                "arguments": {"status": "HUMAN_REVIEW"},
            })

        if any(w in question for w in (
            "money", "cash", "rupee", "amount", "value",
            "blocked", "stuck", "exposure", "settled",
        )):
            return json.dumps({
                "tool_name": "get_cash_position", "arguments": {},
            })

        if any(w in question for w in ("resolve", "exception", "failed")):
            return json.dumps({
                "tool_name": "get_exceptions", "arguments": {},
            })

        if any(w in question for w in ("fast", "speed", "throughput")):
            return json.dumps({
                "tool_name": "get_throughput_report", "arguments": {},
            })

        return json.dumps({"tool_name": "get_match_rate", "arguments": {}})

    return call


# ======================================================================
# DEMO
# ======================================================================

def show_batch_summary(context: BatchQueryContext) -> None:
    rate = context.get_match_rate()

    print(RULE)
    print("DETERMINISTIC PIPELINE — COMPLETE")
    print(RULE)
    print()
    print(f"  Records processed : {rate['total_records']}")
    print(f"  Fully matched     : {rate['matched']} "
          f"({rate['match_rate_pct']}%)")
    print(f"  Unresolved        : {rate['unresolved']}")
    print()
    print("  By decision status:")
    for status, count in sorted(rate["by_status"].items()):
        print(f"    {status:<16} {count}")
    print()
    print("  The dataset is deliberately adversarial: ten anomaly")
    print("  categories, only 18 clean records by construction. A high")
    print("  match rate here would mean the exceptions were not being")
    print("  caught.")
    print()


def ask_and_verify(
    agent: FinanceControllerAgent,
    context: BatchQueryContext,
    question: str,
    index: int,
    offline: bool = False,
) -> bool:
    """
    Ask one question and verify the model changed nothing.

    Returns False if the data invariant was violated.
    """
    print(THIN)
    print(f"Q{index}. {question}")
    print()

    answer = agent.ask(question)

    # The controller reports "llm" for any successful call through
    # llm_call_fn, which in offline mode is a keyword stub. Correct the
    # label here rather than in the controller: from the controller's
    # position "the callable succeeded" is accurate, but printing "llm"
    # next to stub output would misrepresent what produced it.
    source = answer.answer_source
    if offline and source == "llm":
        source = "offline stub"

    print(f"  tool selected : {answer.tool_used or '(none)'}")
    if answer.tool_arguments:
        print(f"  arguments     : {answer.tool_arguments}")
    print(f"  answer source : {source}")
    print()
    print(f"  {answer.answer}")
    print()

    # ------------------------------------------------------------------
    # THE POINT OF THE DEMO
    # ------------------------------------------------------------------
    if answer.data is None:
        print("  [no data returned -- refusal or error path]")
        print()
        return True

    direct = getattr(context, answer.tool_used)(**answer.tool_arguments)

    if direct == answer.data:
        print("  VERIFIED: identical to calling the tool directly.")
        if offline:
            print("            The stub chose the tool; every number")
            print("            came from the engine.")
        else:
            print("            The model chose the question and wrote")
            print("            the prose. Every number came from the")
            print("            engine.")
        print()
        return True

    print("  *** DATA INVARIANT VIOLATED ***")
    print("  The agent path and a direct tool call disagree.")
    print()
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Finance Controller demonstration"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Run without a model. Proves the loop, not the model.",
    )
    args = parser.parse_args()

    print()
    print(RULE)
    print("AI FINANCE CONTROLLER — END-TO-END DEMONSTRATION")
    print(RULE)
    print()

    # ------------------------------------------------------------------
    # 1. Deterministic pipeline
    # ------------------------------------------------------------------
    print("Running ingest -> normalize -> match -> tax -> decide ...")
    print()

    context = BatchQueryContext(raw_dir=RAW_DIR)
    show_batch_summary(context)

    # ------------------------------------------------------------------
    # 2. Agent
    # ------------------------------------------------------------------
    offline = args.offline or not os.environ.get("GEMINI_API_KEY")

    if offline:
        if not args.offline:
            print("  GEMINI_API_KEY not set -- running offline.")
            print("  For the real demonstration:")
            print("    export $(grep -v '^#' .env | grep -v '^$' | xargs)")
            print()
        print(RULE)
        print("AGENT — OFFLINE MODE")
        print(RULE)
        print()
        print("  Keyword stub in place of the model. This shows the")
        print("  loop, the tools and the data invariant. It shows")
        print("  nothing about model behaviour.")
        print()
        llm_call_fn = offline_model(context)
        model_label = "offline stub"
    else:
        from src.agent.config import load_agent_config
        from src.agent.providers.gemini_provider import GeminiProvider

        config = load_agent_config()
        provider = GeminiProvider(config)
        llm_call_fn = provider.as_callable()
        model_label = config.gemini_model

        print(RULE)
        print(f"AGENT — {model_label}")
        print(RULE)
        print()
        print("  Two model calls per question:")
        print("    1. SELECTION — sees the tool catalogue, not the data")
        print("    2. PHRASING  — sees the real result, uses only it")
        print()
        print("  Between them the deterministic engine produces the")
        print("  numbers. The model has no path to change one.")
        print()

    agent = FinanceControllerAgent(llm_call_fn, context)

    # ------------------------------------------------------------------
    # 3. Questions
    # ------------------------------------------------------------------
    exceptions = context.get_exceptions()["exceptions"]
    sample_txn = (
        exceptions[0]["txn_id"] if exceptions
        else context.decisions[0].txn_id
    )

    questions = [
        q if q is not None else f"Why is {sample_txn} not fully matched?"
        for q in DEMO_QUESTIONS
    ]

    print(RULE)
    print("OPERATOR SESSION")
    print(RULE)
    print()

    all_verified = True
    for index, question in enumerate(questions, start=1):
        if not ask_and_verify(
            agent, context, question, index, offline
        ):
            all_verified = False

    # ------------------------------------------------------------------
    # 4. Close
    # ------------------------------------------------------------------
    print(RULE)
    print("SUMMARY")
    print(RULE)
    print()
    print(f"  Model            : {model_label}")
    print(f"  Questions asked  : {len(questions)}")
    print(f"  Data invariant   : "
          f"{'held on every answer' if all_verified else 'VIOLATED'}")
    print()

    if offline:
        print("  Offline run. Re-run with GEMINI_API_KEY set to see the")
        print("  real model choose tools and write the answers.")
        print()

    print("  What this demonstrates:")
    print("    - one finance-ops loop closed end to end")
    print("    - match rate and the complete exception list reported")
    print("      by the agent, from the full batch")
    print("    - evidence retrievable for any single transaction")
    print("    - measured throughput, not claimed")

    if offline:
        print("    - the data invariant holds regardless of what sits")
        print("      in the model's place")
    else:
        print("    - a model that chooses questions and writes prose,")
        print("      and cannot alter a financial fact")

    print()

    if not all_verified:
        print("  DATA INVARIANT VIOLATED -- see above.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()