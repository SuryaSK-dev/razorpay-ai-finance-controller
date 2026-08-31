# tests/test_run_pipeline.py
"""
Guards the model-free entry point.

WHAT THIS PROTECTS
------------------
`run_pipeline.py` is the first thing many reviewers will run, and the
only artefact that demonstrates the deterministic core standing on its
own. Two properties matter:

    1. It reports the SAME numbers the agent's tools report.
       Two views of one batch that disagree would be worse than one
       view, because a reader has no way to tell which is current.

    2. It stays model-free.
       If it imported a provider, the separation it exists to
       demonstrate would not be real.

The second is asserted structurally rather than by inspection -- the
same approach as test_tools_expose_no_mutation_surface.
"""

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.exceptions.manager import decide_batch
from src.ingestion.loader import load_batch
from src.matching.engine import run_matching
from src.models import DecisionStatus
from src.normalization.engine import normalize_batch
from src.agent.tools.query_tools import (
    CASH_BUCKET_BY_STATUS,
    BatchQueryContext,
)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_pipeline.py"
RAW_DIR = ROOT / "data" / "raw"

sys.path.append(str(ROOT / "scripts"))

import run_pipeline  # noqa: E402


def _pipeline():
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    results = run_matching(normalized.records)
    return batch, results, decide_batch(results)


# ======================================================================
# IT STAYS MODEL-FREE
# ======================================================================

def test_script_imports_no_provider():
    """
    Structural. A provider import here would contradict the script's own
    headline claim, and the claim is the reason the script exists.
    """
    source = SCRIPT.read_text(encoding="utf-8")

    forbidden = (
        "gemini_provider",
        "GeminiProvider",
        "load_agent_config",
        "call_llm_bounded",
        "FinanceControllerAgent",
        "google.genai",
    )

    for token in forbidden:
        assert token not in source, (
            f"run_pipeline.py references {token!r} -- it must run with no "
            "model involved"
        )


def test_runs_with_no_api_key():
    """
    End to end, in a subprocess, with the credential explicitly blanked.
    Importing the module is not enough -- this proves the whole run
    completes without one.
    """
    import os

    env = dict(os.environ)
    env["GEMINI_API_KEY"] = ""

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
        timeout=180,
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip().startswith("{")


# ======================================================================
# IT AGREES WITH THE AGENT'S VIEW OF THE SAME BATCH
# ======================================================================

def test_bucket_mapping_is_imported_not_restated():
    """
    THE SECTION 52 GUARD, applied to the cash buckets.

    run_pipeline.py must not define its own status -> bucket mapping.
    Two copies that agree today and nothing keeping them agreeing is
    exactly how expected_net came to exist in four places.
    """
    source = SCRIPT.read_text(encoding="utf-8")

    assert "CASH_BUCKET_BY_STATUS" in source, (
        "run_pipeline.py should import the shared bucket mapping"
    )

    # A restated mapping would need to name statuses next to bucket
    # strings. The imported form never does.
    assert 'DecisionStatus.MATCHED: "settled' not in source
    assert 'DecisionStatus.UNMATCHED: "not yet' not in source


def test_cash_totals_match_the_query_tool():
    """
    The script and get_cash_position() compute the same figures from the
    same batch. They must agree to the paise.
    """
    batch, results, decisions = _pipeline()

    script_summary = run_pipeline.stage_cash(
        results, decisions, batch.total_errors
    )
    tool = BatchQueryContext(raw_dir=RAW_DIR).get_cash_position()

    assert Decimal(script_summary["total_expected_settlement"]) == Decimal(
        tool["total_expected_settlement"]
    )
    assert Decimal(script_summary["total_bank_credited"]) == Decimal(
        tool["total_bank_credited"]
    )
    assert Decimal(script_summary["variance_vs_bank_credited"]) == Decimal(
        tool["variance_vs_bank_credited"]
    )


def test_cash_buckets_match_the_query_tool():
    batch, results, decisions = _pipeline()

    script_summary = run_pipeline.stage_cash(
        results, decisions, batch.total_errors
    )
    tool = BatchQueryContext(raw_dir=RAW_DIR).get_cash_position()

    pairs = [
        ("settled and verified", "settled_and_verified"),
        ("awaiting verification", "awaiting_verification"),
        ("blocked in exceptions", "blocked_in_exceptions"),
        ("not yet credited", "not_yet_credited"),
    ]

    for label, key in pairs:
        assert Decimal(script_summary["by_bucket"][label]) == Decimal(
            tool["by_bucket"][key]["amount"]
        ), label


def test_every_status_has_a_display_label():
    """
    STATUS_MEANING and STATUS_ORDER are checked against the enum, not a
    hand-written list -- a new status would otherwise be silently absent
    from the table while still being counted in the total.
    """
    for status in DecisionStatus:
        assert status in run_pipeline.STATUS_ORDER, status
        assert status in run_pipeline.STATUS_MEANING, status

    assert len(run_pipeline.STATUS_ORDER) == len(set(run_pipeline.STATUS_ORDER))


# ======================================================================
# THE JSON SUMMARY
# ======================================================================

def test_json_summary_is_complete_not_sampled():
    """
    Track 04: "one cherry-picked match proves nothing." The exception
    list must carry every unresolved record, with no truncation.
    """
    import json
    import os

    env = dict(os.environ)
    env["GEMINI_API_KEY"] = ""

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=180,
    )
    payload = json.loads(completed.stdout)

    _, _, decisions = _pipeline()
    unresolved = [d for d in decisions if d.status != DecisionStatus.MATCHED]

    assert payload["total_records"] == len(decisions)
    assert payload["matched"] == sum(
        1 for d in decisions if d.status == DecisionStatus.MATCHED
    )
    assert len(payload["exceptions"]) == len(unresolved)


def test_json_status_counts_reconcile():
    import json
    import os

    env = dict(os.environ)
    env["GEMINI_API_KEY"] = ""

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=180,
    )
    payload = json.loads(completed.stdout)

    assert sum(payload["by_status"].values()) == payload["total_records"]
    assert payload["matched"] + len(payload["exceptions"]) == \
        payload["total_records"]


def test_every_exception_carries_the_rule_that_fired():
    """
    A decision a reader cannot trace to a rule is not auditable -- the
    same standard the gold baseline applies to its exclusions.
    """
    import json
    import os

    env = dict(os.environ)
    env["GEMINI_API_KEY"] = ""

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=180,
    )
    payload = json.loads(completed.stdout)

    for item in payload["exceptions"]:
        assert item["matched_rule"], item["txn_id"]
        assert item["reason_codes"], item["txn_id"]
        assert item["exception_code"] != "NONE", item["txn_id"]
