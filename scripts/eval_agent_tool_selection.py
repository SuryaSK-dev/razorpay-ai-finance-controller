# scripts/eval_agent_tool_selection.py
"""
Stage 3 — Tool-selection accuracy, measured.

WHAT THIS REPLACES
------------------
Six questions through live Gemini, reported as "6/6 tool selection
matched expectation". That was a smoke test, and FAILURE_LOG.md section
46 says so. Six cases cannot distinguish a model that understands the
tool catalogue from one that got lucky on six direct phrasings.

WHAT IT MEASURES
----------------
32 held-out questions against two routers:

    BASELINE   a deterministic keyword router -- no model, no network.
               The same shape as the demo's --offline stub.

    MODEL      real Gemini through the ordinary bounded path.

Reporting both is the point. If the model does not beat a keyword
router, it is not earning its place in the selection step, and that is
a result worth publishing rather than hiding. This follows the pattern
already used for narration extraction: eval_narration_baseline.py
measures the deterministic path, eval_narration_extraction.py measures
the model, and the comparison is the finding.

WHAT IS SCORED
--------------
    tool     did the router choose the right tool?
    args     did it also supply the right arguments?

Argument correctness is scored SEPARATELY and never folded into the
tool score. Selecting get_exceptions for "show me everything needing
human review" but omitting status=HUMAN_REVIEW is a real partial
failure: the operator gets every exception rather than the subset they
asked for, and the phrasing layer will describe it as though it were
the subset. Collapsing that into a single pass/fail would hide it.

PROMPT INJECTION
----------------
Three cases carry `acceptable_tools` rather than a single expectation.
For an injection attempt, both answering with the real number and
declining are safe: ToolSelection has no field for a financial value,
so a compliant-but-malicious selection cannot carry one. The cases
exist to confirm the selection stays inside the catalogue, not to
assert one particular refusal.

RUNNING IT
----------
    python scripts/eval_agent_tool_selection.py               # baseline only
    python scripts/eval_agent_tool_selection.py --model       # + live Gemini

The baseline path is hermetic. The model path needs GEMINI_API_KEY and
costs one call per case.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))

from src.agent.tool_selection import (
    NO_TOOL,
    build_selection_prompt,
    parse_selection,
    validate_selection,
)
from src.agent.tools.registry import TOOL_REGISTRY

DATASET_PATH = ROOT / "data" / "eval" / "held_out_agent_questions.json"
OUTPUT_PATH = ROOT / "data" / "eval" / "agent_tool_selection_report.json"


# ======================================================================
# THE DETERMINISTIC BASELINE
# ======================================================================

def keyword_baseline_select(question: str) -> str:
    """
    Route on keywords alone. No model, no network, no reasoning.

    This is the honest floor: what you get without a language model. It
    is the same shape as the --offline stub in demo_agent.py, kept here
    as a standalone function so the evaluation does not depend on demo
    plumbing.

    Order matters and is doing real work. "How much money is blocked
    behind those exceptions?" contains the word 'exception', so the
    money check has to run first or a value question routes to the
    exception list. That ordering is exactly the kind of hand-tuning a
    keyword router needs and a model should not.
    """
    q = question.lower()

    if "txn_" in q:
        return "get_evidence"

    if any(w in q for w in (
        "money", "cash", "rupee", "amount", "value",
        "blocked", "stuck", "owed", "variance", "settled",
    )):
        return "get_cash_position"

    if any(w in q for w in ("fast", "speed", "throughput", "long", "performance")):
        return "get_throughput_report"

    if any(w in q for w in (
        "exception", "unresolved", "resolve", "failed", "review",
        "ambiguous", "mismatch",
    )):
        return "get_exceptions"

    if any(w in q for w in ("match rate", "matched", "overall", "reconcil", "tier")):
        return "get_match_rate"

    return NO_TOOL


def baseline_arguments(question: str, tool: str) -> dict[str, Any]:
    """
    Argument extraction for the baseline, deliberately crude.

    A keyword router can pull a TXN_ token out with a slice. It has no
    principled way to map "everything that needs human review" onto
    status=HUMAN_REVIEW without a second hand-written lookup table --
    which is precisely the work a model does for free, and precisely
    what this comparison is meant to expose.
    """
    if tool == "get_evidence" and "txn_" in question.lower():
        start = question.lower().index("txn_")
        return {"txn_id": question[start:start + 9].upper().strip(" ?.,-")}

    if tool == "get_exceptions":
        for status in ("HUMAN_REVIEW", "AMBIGUOUS", "TAX_MISMATCH",
                       "PARTIAL_MATCH", "UNMATCHED", "MATCHED"):
            if status.replace("_", " ").lower() in question.lower():
                return {"status": status}

    return {}


# ======================================================================
# CASE EXECUTION
# ======================================================================

@dataclass
class CaseResult:
    case_id: str
    category: str
    question: str
    expected: str
    selected: Optional[str]
    expected_arguments: dict
    selected_arguments: dict
    tool_correct: bool
    arguments_correct: bool
    selection_valid: bool
    error: Optional[str] = None
    raw: Optional[str] = None


def _acceptable(case: dict) -> list[str]:
    if "acceptable_tools" in case:
        return list(case["acceptable_tools"])
    return [case["expected_tool"]]


def _expected_label(case: dict) -> str:
    if "acceptable_tools" in case:
        return " | ".join(case["acceptable_tools"])
    return case["expected_tool"]


def run_case(case: dict, select_fn) -> CaseResult:
    """
    Run one case through a router.

    `select_fn` maps a question to (tool_name, arguments, raw, error).
    Both routers go through the same scoring so the comparison is
    like-for-like.
    """
    tool, arguments, raw, error = select_fn(case["question"])

    acceptable = _acceptable(case)
    tool_correct = tool in acceptable

    # Arguments are only meaningful when the tool was right. Scoring an
    # argument against the wrong tool would double-count one mistake.
    expected_args = case.get("expected_arguments", {})
    if "acceptable_tools" in case:
        # Injection cases: any argument set the registry accepts is fine.
        arguments_correct = tool_correct
    else:
        arguments_correct = tool_correct and arguments == expected_args

    valid = tool is not None and (
        tool == NO_TOOL or tool in TOOL_REGISTRY
    )

    return CaseResult(
        case_id=case["case_id"],
        category=case["category"],
        question=case["question"],
        expected=_expected_label(case),
        selected=tool,
        expected_arguments=expected_args,
        selected_arguments=arguments,
        tool_correct=tool_correct,
        arguments_correct=arguments_correct,
        selection_valid=valid,
        error=error,
        raw=raw,
    )


def baseline_router(question: str):
    tool = keyword_baseline_select(question)
    return tool, baseline_arguments(question, tool), None, None


def build_model_router(llm_call_fn):
    """
    Route through the real selection path -- the same prompt, parser and
    validator the agent uses in production. Not a reimplementation.
    """
    def route(question: str):
        prompt = build_selection_prompt(question)
        try:
            raw = llm_call_fn(prompt)
        except Exception as exc:            # provider failure, not model error
            return None, {}, None, f"provider_failure: {exc}"

        try:
            selection = parse_selection(raw)
        except Exception as exc:
            return None, {}, raw, f"parse_failure: {exc}"

        if not validate_selection(selection):
            return (
                selection.tool_name,
                selection.arguments,
                raw,
                "rejected_by_validator",
            )

        return selection.tool_name, selection.arguments, raw, None

    return route


# ======================================================================
# METRICS
# ======================================================================

def calculate_metrics(results: list[CaseResult]) -> dict:
    total = len(results)
    tool_ok = sum(1 for r in results if r.tool_correct)
    args_ok = sum(1 for r in results if r.arguments_correct)

    by_category: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "tool_ok": 0, "args_ok": 0}
    )
    for r in results:
        bucket = by_category[r.category]
        bucket["total"] += 1
        bucket["tool_ok"] += int(r.tool_correct)
        bucket["args_ok"] += int(r.arguments_correct)

    return {
        "total_cases": total,
        "tool_correct": tool_ok,
        "tool_accuracy_pct": round(100.0 * tool_ok / total, 2) if total else 0.0,
        "arguments_correct": args_ok,
        "arguments_accuracy_pct": round(100.0 * args_ok / total, 2) if total else 0.0,
        "invalid_selections": sum(1 for r in results if not r.selection_valid),
        "provider_failures": sum(
            1 for r in results if r.error and r.error.startswith("provider_failure")
        ),
        "parse_failures": sum(
            1 for r in results if r.error and r.error.startswith("parse_failure")
        ),
        "by_category": {k: dict(v) for k, v in sorted(by_category.items())},
    }


def confusion_matrix(results: list[CaseResult]) -> dict:
    """
    Expected tool (rows) against selected tool (columns).

    A single accuracy figure says how often the router was wrong. The
    matrix says WHERE -- and the interesting failures in this system are
    directional. get_cash_position mistaken for get_exceptions is a
    value question answered with a list; the reverse is a list question
    answered with a total. Both are wrong, and they are wrong in
    different ways.
    """
    matrix: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        matrix[r.expected][r.selected or "<none returned>"] += 1
    return {k: dict(v) for k, v in matrix.items()}


# ======================================================================
# REPORTING
# ======================================================================

RULE = "=" * 74


def print_metrics(label: str, metrics: dict) -> None:
    print(RULE)
    print(f"{label}")
    print(RULE)
    print()
    print(f"  Cases                : {metrics['total_cases']}")
    print(f"  Tool correct         : {metrics['tool_correct']}/"
          f"{metrics['total_cases']}  ({metrics['tool_accuracy_pct']}%)")
    print(f"  Arguments correct    : {metrics['arguments_correct']}/"
          f"{metrics['total_cases']}  ({metrics['arguments_accuracy_pct']}%)")
    print(f"  Invalid selections   : {metrics['invalid_selections']}")

    if metrics["provider_failures"] or metrics["parse_failures"]:
        print(f"  Provider failures    : {metrics['provider_failures']}"
              "   (infrastructure, NOT model quality)")
        print(f"  Parse failures       : {metrics['parse_failures']}"
              "   (model quality)")
    print()
    print("  By category:")
    for category, counts in metrics["by_category"].items():
        print(f"    {category:<20} tool {counts['tool_ok']}/{counts['total']}"
              f"   args {counts['args_ok']}/{counts['total']}")
    print()


def print_confusion(label: str, matrix: dict) -> None:
    print(f"  Confusion matrix -- {label}")
    print("  (expected -> selected; only rows with an error are listed)")
    print()
    clean = True
    for expected, selections in sorted(matrix.items()):
        wrong = {
            k: v for k, v in selections.items()
            if k not in [t.strip() for t in expected.split("|")]
        }
        if not wrong:
            continue
        clean = False
        print(f"    {expected}")
        for selected, count in sorted(wrong.items()):
            print(f"        -> {selected:<26} {count}")
    if clean:
        print("    no misroutes")
    print()


def print_failures(label: str, results: list[CaseResult]) -> None:
    failures = [r for r in results if not r.tool_correct]
    arg_only = [
        r for r in results if r.tool_correct and not r.arguments_correct
    ]

    if failures:
        print(f"  Misrouted -- {label}")
        for r in failures:
            print(f"    {r.case_id} [{r.category}] {r.question}")
            print(f"        expected {r.expected}, selected {r.selected}")
            if r.error:
                print(f"        error: {r.error}")
        print()

    if arg_only:
        print(f"  Right tool, wrong arguments -- {label}")
        for r in arg_only:
            print(f"    {r.case_id} {r.question}")
            print(f"        expected {r.expected_arguments}, "
                  f"got {r.selected_arguments}")
        print()


# ======================================================================
# MAIN
# ======================================================================

def load_dataset() -> dict:
    with DATASET_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure tool-selection accuracy"
    )
    parser.add_argument(
        "--model",
        action="store_true",
        help="Also evaluate live Gemini. Requires GEMINI_API_KEY.",
    )
    args = parser.parse_args()

    dataset = load_dataset()
    cases = dataset["cases"]

    print()
    print(RULE)
    print("TOOL-SELECTION EVALUATION")
    print(RULE)
    print()
    print(f"  Dataset  : {dataset['dataset_version']}  ({len(cases)} cases)")
    print(f"  Tools    : {len(TOOL_REGISTRY)} registered + 'none'")
    print()

    baseline_results = [run_case(c, baseline_router) for c in cases]
    baseline_metrics = calculate_metrics(baseline_results)
    baseline_matrix = confusion_matrix(baseline_results)

    print_metrics("BASELINE -- deterministic keyword router, no model",
                  baseline_metrics)
    print_confusion("baseline", baseline_matrix)
    print_failures("baseline", baseline_results)

    # A baseline-only run must NOT destroy a recorded live-model result.
    #
    # Found by the cold-clone freeze: running the documented hermetic
    # command overwrote the artifact with model=null, silently deleting
    # a measurement that costs 32 API calls to reproduce. A judge
    # following the README would have wiped it without any signal.
    #
    # Same failure shape as FAILURE_LOG.md section 54 -- an artifact
    # that stops describing reality, except here the mechanism is
    # destruction rather than staleness.
    previous_model = None
    if OUTPUT_PATH.exists():
        try:
            existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
            previous_model = existing.get("model")
        except (json.JSONDecodeError, OSError):
            previous_model = None

    report = {
        "report_version": "agent-selection-v1",
        "dataset_version": dataset["dataset_version"],
        "total_cases": len(cases),
        "registered_tools": sorted(TOOL_REGISTRY),
        "baseline": {
            "router": "deterministic keyword matching, no model",
            "metrics": baseline_metrics,
            "confusion": baseline_matrix,
        },
        "model": previous_model,
        "interpretation": (
            "The baseline is a keyword router with hand-tuned ordering. It "
            "is the honest floor for this task. A model that does not beat "
            "it is not earning its place in the selection step."
        ),
    }

    if args.model:
        from src.agent.config import load_agent_config
        from src.agent.providers.gemini_provider import GeminiProvider

        config = load_agent_config()
        provider = GeminiProvider(config)
        router = build_model_router(provider.as_callable())

        model_results = [run_case(c, router) for c in cases]
        model_metrics = calculate_metrics(model_results)
        model_matrix = confusion_matrix(model_results)

        print_metrics(f"MODEL -- {config.gemini_model}", model_metrics)
        print_confusion("model", model_matrix)
        print_failures("model", model_results)

        report["model"] = {
            "provider": config.gemini_model,
            "metrics": model_metrics,
            "confusion": model_matrix,
            "cases": [
                {
                    "case_id": r.case_id,
                    "category": r.category,
                    "question": r.question,
                    "expected": r.expected,
                    "selected": r.selected,
                    "tool_correct": r.tool_correct,
                    "arguments_correct": r.arguments_correct,
                    "error": r.error,
                }
                for r in model_results
            ],
        }

        delta = (model_metrics["tool_accuracy_pct"]
                 - baseline_metrics["tool_accuracy_pct"])
        report["model_minus_baseline_pct"] = round(delta, 2)
        report["model_is_from_a_previous_run"] = False

        print(RULE)
        print("COMPARISON")
        print(RULE)
        print()
        print(f"  Baseline tool accuracy : "
              f"{baseline_metrics['tool_accuracy_pct']}%")
        print(f"  Model tool accuracy    : "
              f"{model_metrics['tool_accuracy_pct']}%")
        print(f"  Delta                  : {delta:+.2f} points")
        print()
        if delta <= 0:
            print("  The model did NOT beat a keyword router on this set.")
            print("  That is a result, not a bug to tune away. Report it.")
            print()
    else:
        if previous_model:
            recorded = previous_model["metrics"]["tool_accuracy_pct"]
            report["model_is_from_a_previous_run"] = True
            print("  Model not evaluated in THIS run. The recorded result")
            print(f"  ({recorded}% on {previous_model['provider']}) is carried")
            print("  forward rather than discarded -- re-running the")
            print("  baseline must not delete a measurement that costs 32")
            print("  API calls to reproduce.")
        else:
            print("  Model not evaluated. Re-run with --model and "
                  "GEMINI_API_KEY set.")
        print()

    with OUTPUT_PATH.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)

    print(RULE)
    print(f"Artifact: {OUTPUT_PATH}")
    print(RULE)
    print()


if __name__ == "__main__":
    main()
