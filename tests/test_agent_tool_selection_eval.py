# tests/test_agent_tool_selection_eval.py
"""
Integrity tests for the tool-selection evaluation.

WHAT THESE GUARD
----------------
Not the accuracy figure -- the figure is a measurement and is allowed to
move. These guard the properties that make the figure MEAN something:

    the dataset covers every registered tool, plus refusal
    every expectation names a tool that actually exists
    scoring cannot silently pass a case it should fail
    arguments are scored separately from tool choice
    the artifact's arithmetic reconciles

DELIBERATELY ABSENT: no test asserts a minimum accuracy.

The same reasoning as the accuracy report. A test that failed when
selection accuracy dropped would create pressure to edit the question
set until it passed -- and the question set is the answer key. That
failure has already happened twice in this project with ground-truth
labels (FAILURE_LOG.md sections 14 and 15). The number is published, not
enforced.

These tests are hermetic. They exercise the deterministic baseline and
the scoring logic; the live-model path needs credentials and is run by
hand.
"""

import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.agent.tool_selection import NO_TOOL
from src.agent.tools.registry import TOOL_REGISTRY

sys.path.append(str(Path(__file__).resolve().parent.parent / "scripts"))

from eval_agent_tool_selection import (  # noqa: E402
    DATASET_PATH,
    baseline_arguments,
    baseline_router,
    calculate_metrics,
    confusion_matrix,
    keyword_baseline_select,
    load_dataset,
    run_case,
)

ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = ROOT / "data" / "eval" / "agent_tool_selection_report.json"


# ======================================================================
# DATASET INTEGRITY
# ======================================================================

def test_dataset_exists_and_parses():
    assert DATASET_PATH.exists(), (
        "held_out_agent_questions.json is missing -- the evaluation has "
        "no input"
    )
    assert load_dataset()["cases"]


def test_case_ids_are_unique():
    cases = load_dataset()["cases"]
    ids = [c["case_id"] for c in cases]
    assert len(ids) == len(set(ids))


def test_every_case_names_a_real_tool():
    """
    An expectation naming a tool that does not exist would make the case
    unpassable, and the accuracy figure would silently understate the
    router rather than measure it.
    """
    valid = set(TOOL_REGISTRY) | {NO_TOOL}

    for case in load_dataset()["cases"]:
        expected = case.get("acceptable_tools") or [case["expected_tool"]]
        for tool in expected:
            assert tool in valid, (
                f"{case['case_id']} expects {tool!r}, which is not a "
                f"registered tool. Valid: {sorted(valid)}"
            )


def test_every_registered_tool_is_covered():
    """
    A tool with no question in the set is a tool whose routing is
    unmeasured. Adding get_cash_position without adding questions for it
    would leave the headline accuracy unchanged while covering less.
    """
    covered = set()
    for case in load_dataset()["cases"]:
        covered.update(case.get("acceptable_tools") or [case["expected_tool"]])

    missing = set(TOOL_REGISTRY) - covered
    assert not missing, (
        f"no question routes to {sorted(missing)} -- their selection is "
        "unmeasured"
    )


def test_refusal_is_covered():
    """`none` must be reachable, or the set never tests declining."""
    cases = load_dataset()["cases"]
    assert any(c.get("expected_tool") == NO_TOOL for c in cases)


def test_prompt_injection_is_covered():
    cases = load_dataset()["cases"]
    injection = [c for c in cases if c["category"] == "prompt_injection"]

    assert len(injection) >= 3, "too few injection cases to be meaningful"

    for case in injection:
        assert "acceptable_tools" in case, (
            f"{case['case_id']}: an injection case needs acceptable_tools "
            "-- both answering with the real number and declining are safe"
        )


def test_every_case_carries_a_rationale():
    """
    A case a reader cannot evaluate is not a held-out set, it is a list
    of strings. Same standard as the gold baseline's policy exclusions.
    """
    for case in load_dataset()["cases"]:
        assert len(case.get("notes", "")) > 30, (
            f"{case['case_id']} has no substantive rationale"
        )


def test_expected_arguments_are_valid_for_their_tool():
    """
    An expectation demanding an argument the tool does not declare would
    be unpassable -- dispatch() rejects unknown arguments by design.
    """
    for case in load_dataset()["cases"]:
        tool = case.get("expected_tool")
        if tool is None or tool == NO_TOOL:
            continue

        spec = TOOL_REGISTRY[tool]
        for key in case.get("expected_arguments", {}):
            assert key in spec.parameters, (
                f"{case['case_id']} expects argument {key!r} on {tool}, "
                f"which declares {sorted(spec.parameters) or 'none'}"
            )


def test_dataset_is_not_trivially_easy():
    """
    A question set that only ever restates the tool name measures
    keyword overlap, not routing. At least a third of cases must avoid
    naming their own tool's distinguishing word.
    """
    cases = load_dataset()["cases"]
    hints = {
        "get_match_rate": "match rate",
        "get_exceptions": "exception",
        "get_evidence": "evidence",
        "get_cash_position": "cash position",
        "get_throughput_report": "throughput",
    }

    indirect = sum(
        1 for c in cases
        if c.get("expected_tool") in hints
        and hints[c["expected_tool"]] not in c["question"].lower()
    )

    assert indirect >= len(cases) // 3, (
        f"only {indirect} of {len(cases)} cases are phrased indirectly -- "
        "the set is measuring keyword overlap rather than routing"
    )


# ======================================================================
# SCORING LOGIC
# ======================================================================

def test_a_wrong_tool_is_scored_wrong():
    case = {
        "case_id": "T1", "category": "t", "question": "q",
        "expected_tool": "get_match_rate", "expected_arguments": {},
    }
    result = run_case(case, lambda q: ("get_exceptions", {}, None, None))

    assert result.tool_correct is False
    assert result.arguments_correct is False


def test_right_tool_wrong_arguments_is_a_separate_failure():
    """
    THE DISTINCTION THAT MATTERS.

    Selecting get_exceptions without status=HUMAN_REVIEW returns every
    exception instead of the subset asked for -- and the phrasing layer
    will describe the full list as though it were the subset. That is a
    real partial failure and must not be folded into the tool score.
    """
    case = {
        "case_id": "T2", "category": "t", "question": "q",
        "expected_tool": "get_exceptions",
        "expected_arguments": {"status": "HUMAN_REVIEW"},
    }
    result = run_case(case, lambda q: ("get_exceptions", {}, None, None))

    assert result.tool_correct is True
    assert result.arguments_correct is False


def test_injection_cases_accept_either_safe_outcome():
    case = {
        "case_id": "T3", "category": "prompt_injection", "question": "q",
        "acceptable_tools": ["get_match_rate", "none"],
        "expected_arguments": {},
    }

    for tool in ("get_match_rate", "none"):
        assert run_case(case, lambda q, t=tool: (t, {}, None, None)).tool_correct

    assert not run_case(
        case, lambda q: ("get_evidence", {}, None, None)
    ).tool_correct


def test_provider_failure_is_not_scored_as_a_wrong_answer():
    """
    An outage is infrastructure, not model quality -- the same
    distinction CaseResult.outcome draws in the narration evaluation
    (FAILURE_LOG.md section 25). It must be counted separately, never as
    a routing mistake the model made.
    """
    case = {
        "case_id": "T4", "category": "t", "question": "q",
        "expected_tool": "get_match_rate", "expected_arguments": {},
    }
    result = run_case(
        case, lambda q: (None, {}, None, "provider_failure: timeout")
    )

    metrics = calculate_metrics([result])

    assert metrics["provider_failures"] == 1
    assert metrics["parse_failures"] == 0


def test_metrics_arithmetic_reconciles():
    dataset = load_dataset()
    results = [run_case(c, baseline_router) for c in dataset["cases"]]
    metrics = calculate_metrics(results)

    assert metrics["total_cases"] == len(dataset["cases"])
    assert metrics["tool_correct"] == sum(1 for r in results if r.tool_correct)

    assert sum(v["total"] for v in metrics["by_category"].values()) == \
        metrics["total_cases"]
    assert sum(v["tool_ok"] for v in metrics["by_category"].values()) == \
        metrics["tool_correct"]


def test_arguments_correct_never_exceeds_tool_correct():
    """
    Arguments are only scored when the tool was right, so the argument
    count can never exceed the tool count. If it did, a case would be
    passing on arguments while failing on routing.
    """
    results = [run_case(c, baseline_router) for c in load_dataset()["cases"]]
    metrics = calculate_metrics(results)

    assert metrics["arguments_correct"] <= metrics["tool_correct"]


def test_confusion_matrix_accounts_for_every_case():
    results = [run_case(c, baseline_router) for c in load_dataset()["cases"]]
    matrix = confusion_matrix(results)

    counted = sum(sum(row.values()) for row in matrix.values())
    assert counted == len(results)


# ======================================================================
# THE BASELINE ITSELF
# ======================================================================

def test_baseline_is_deterministic():
    dataset = load_dataset()
    first = [run_case(c, baseline_router).selected for c in dataset["cases"]]
    second = [run_case(c, baseline_router).selected for c in dataset["cases"]]

    assert first == second


def test_baseline_needs_no_model_or_network():
    """
    The floor must be measurable with no credentials, or it is not a
    floor -- it is a second thing that can fail for the same reason.
    """
    assert keyword_baseline_select("what is the match rate?") in (
        set(TOOL_REGISTRY) | {NO_TOOL}
    )


def test_baseline_extracts_a_txn_id():
    args = baseline_arguments("why is TXN_00031 unresolved?", "get_evidence")
    assert args == {"txn_id": "TXN_00031"}


def test_baseline_cannot_tell_a_read_from_a_mutation():
    """
    THE FINDING THIS EVALUATION EXISTS TO SURFACE.

    The keyword router sees TXN_ and routes to get_evidence. It does the
    same for "why is TXN_00031 unresolved?" and for "change the status
    of TXN_00025 to MATCHED" -- because it matches tokens, not intent.

    Nothing unsafe follows: the tool layer is read-only, so a mutation
    request routed to get_evidence returns evidence rather than
    mutating anything. But the operator asked to change something and
    got a read, with no signal that the request was refused.

    Recognising that a request is out of scope is judgement, and it is
    the clearest thing a model buys in the selection step. Asserted here
    so the claim rests on a measured behaviour rather than an intuition.
    """
    assert keyword_baseline_select(
        "Change the status of TXN_00025 to MATCHED."
    ) == "get_evidence"

    assert keyword_baseline_select(
        "Can you re-run the matching for TXN_00031?"
    ) == "get_evidence"


# ======================================================================
# THE PUBLISHED ARTIFACT
# ======================================================================

def test_report_artifact_exists():
    assert REPORT_PATH.exists(), (
        "run scripts/eval_agent_tool_selection.py to produce the artifact"
    )


def test_report_matches_the_current_dataset():
    """
    A report built against an older question set would publish a number
    that no longer describes the set it names -- the shape of
    FAILURE_LOG.md section 54, where a stale artifact outlived the code
    it measured.
    """
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    dataset = load_dataset()

    assert report["dataset_version"] == dataset["dataset_version"]
    assert report["total_cases"] == len(dataset["cases"])


def test_report_covers_every_registered_tool():
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert set(report["registered_tools"]) == set(TOOL_REGISTRY)


def test_a_baseline_only_run_does_not_destroy_the_model_result():
    """
    REGRESSION (FAILURE_LOG.md section 57).

    Found by the cold-clone freeze. Running the documented hermetic
    command --

        python scripts/eval_agent_tool_selection.py

    -- overwrote the artifact with model=null, deleting 399 lines and a
    measurement that costs 32 API calls to reproduce. A judge following
    the README would have wiped it with no signal that anything was lost.

    The baseline-only path now carries a previously recorded model
    section forward and flags it as not-from-this-run.
    """
    import subprocess

    if not REPORT_PATH.exists():
        return

    before = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    if before.get("model") is None:
        return          # nothing recorded to protect

    import os
    env = dict(os.environ)
    env["GEMINI_API_KEY"] = ""

    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "eval_agent_tool_selection.py")],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-1500:]

    after = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    assert after["model"] is not None, (
        "a baseline-only run deleted the recorded live-model result"
    )
    assert (after["model"]["metrics"]["tool_accuracy_pct"]
            == before["model"]["metrics"]["tool_accuracy_pct"])
    assert after.get("model_is_from_a_previous_run") is True, (
        "a carried-forward model result must be flagged as such, or a "
        "reader cannot tell it was not measured in this run"
    )


def test_report_baseline_arithmetic_reconciles():
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    metrics = report["baseline"]["metrics"]

    assert metrics["tool_correct"] <= metrics["total_cases"]
    assert metrics["arguments_correct"] <= metrics["tool_correct"]

    expected_pct = round(
        100.0 * metrics["tool_correct"] / metrics["total_cases"], 2
    )
    assert metrics["tool_accuracy_pct"] == expected_pct
