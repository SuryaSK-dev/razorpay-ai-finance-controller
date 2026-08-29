# src/agent/tools/query_tools.py
"""
Phase 6 — Read-only query tools over deterministic decisions.

WHAT THIS IS
------------
Four functions that answer the questions Track 04 requires a finance
operator to be able to ask:

    get_match_rate()             -- what matched, and how confidently
    get_exceptions(status=None)  -- what did NOT resolve, itemised
    get_evidence(txn_id)         -- why one specific decision was made
    get_throughput_report()      -- how fast the batch processed

WHAT THIS IS NOT
----------------
None of these functions compute a financial outcome. Every number
returned is READ from `decide_batch()` output that already exists.
There is no code path here that can change a status, an amount, an
exception code, or a tax verdict.

This matters because Step 3 puts a language model in front of these
tools. The model will choose WHICH tool to call and phrase the result
in English. It must not be able to influence the result itself. The
cleanest way to guarantee that is for the tools to have no capability
to produce a new financial fact -- only to look one up.

For the same reason there is deliberately no `re_evaluate(txn_id)` or
`rematch(txn_id)` tool, and there must never be one. Adding a tool that
recomputes would reopen exactly the failure mode this architecture
exists to prevent: the model becoming the source of financial truth.

CACHING
-------
`BatchQueryContext` runs the pipeline ONCE at construction and holds
the resulting decisions. Every tool then reads that snapshot.

Two reasons:

  1. A live demo cannot re-run the pipeline per question.
  2. Every question in one session must see the SAME batch. If the
     tools re-ran the pipeline each call, two questions could in
     principle disagree, and an operator would have no way to tell
     which answer was current.

Call `refresh()` explicitly to re-read from disk. Nothing does that
implicitly.

RETURN SHAPE
------------
Every tool returns a plain dict of JSON-serialisable primitives, not
domain objects. The layer above this is a prompt, and a prompt cannot
carry a `MatchDecision`. Enums are converted to their string values at
this boundary so the serialisation is explicit and testable here rather
than incidental somewhere upstream.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from src.models import DecisionStatus, ExceptionCode, MatchDecision
from src.ingestion.loader import load_batch
from src.normalization.engine import normalize_batch
from src.matching.engine import run_matching
from src.exceptions.manager import decide_batch


ROOT = Path(__file__).resolve().parent.parent.parent.parent

DEFAULT_RAW_DIR = ROOT / "data" / "raw"
DEFAULT_THROUGHPUT_PATH = ROOT / "data" / "throughput_benchmark.json"


# Statuses that represent a resolved, no-action-needed outcome.
# Everything else is an exception a human may need to look at.
RESOLVED_STATUSES = frozenset({DecisionStatus.MATCHED})


class TxnNotFoundError(LookupError):
    """
    Raised when a requested transaction ID is not in the batch.

    This exists as a distinct exception type because Step 3 must be
    able to tell "the model hallucinated an ID" apart from "the tool
    broke". A hallucinated ID must produce an honest 'I have no record
    of that' -- never a fabricated answer, and never a crash.
    """


class BatchQueryContext:
    """
    Runs the deterministic pipeline once and answers questions about
    the result.

    The pipeline runs at construction, not lazily, so that a failure to
    load or process the batch surfaces immediately rather than in the
    middle of a conversation.
    """

    def __init__(
        self,
        raw_dir: Path | None = None,
        throughput_path: Path | None = None,
    ) -> None:
        self.raw_dir = Path(raw_dir) if raw_dir else DEFAULT_RAW_DIR
        self.throughput_path = (
            Path(throughput_path) if throughput_path
            else DEFAULT_THROUGHPUT_PATH
        )

        self.decisions: list[MatchDecision] = []
        self.match_results: list[Any] = []
        self._by_txn: dict[str, MatchDecision] = {}

        self.refresh()

    # ------------------------------------------------------------------
    # PIPELINE
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """
        Re-run the deterministic pipeline from disk.

        Explicit only. No tool calls this implicitly, because a batch
        that changes mid-conversation would make two answers in the
        same session inconsistent with no signal to the operator.
        """
        batch = load_batch(self.raw_dir)
        normalized = normalize_batch(batch)

        self.match_results = run_matching(normalized.records)
        self.decisions = decide_batch(self.match_results)

        self._by_txn = {d.txn_id: d for d in self.decisions}

        # Confidence tier per transaction, taken from the matching
        # layer rather than re-derived from confidence_score. Deriving
        # it here would be a second implementation of a threshold that
        # already exists in scoring.py, and the two could drift.
        self._tier_by_txn = {
            result.txn_id: result.confidence.value
            for result in self.match_results
        }

    # ------------------------------------------------------------------
    # TOOL 1 -- MATCH RATE
    # ------------------------------------------------------------------

    def get_match_rate(self) -> dict[str, Any]:
        """
        Match rate across the full batch.

        Track 04 asks the agent to report "its match rate". This is
        that number, computed over EVERY record processed -- not a
        filtered subset, and not a cherry-picked example.

        `matched` counts only DecisionStatus.MATCHED. PARTIAL_MATCH is
        reported separately rather than folded in, because a partial
        match is a record whose tax could not be verified. Counting it
        as matched would inflate the headline number by describing an
        unverified record as reconciled.
        """
        total = len(self.decisions)

        status_counts = Counter(d.status.value for d in self.decisions)
        tier_counts = Counter(self._tier_by_txn.values())

        matched = status_counts.get(DecisionStatus.MATCHED.value, 0)

        return {
            "total_records": total,
            "matched": matched,
            "match_rate_pct": (
                round(100.0 * matched / total, 2) if total else 0.0
            ),
            "unresolved": total - matched,
            "by_status": dict(sorted(status_counts.items())),
            "by_confidence_tier": dict(sorted(tier_counts.items())),
        }

    # ------------------------------------------------------------------
    # TOOL 2 -- EXCEPTIONS
    # ------------------------------------------------------------------

    def get_exceptions(
        self,
        status: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Every record that did not cleanly resolve, itemised.

        Track 04 asks for "the exceptions it could not resolve", and the
        brief explicitly warns that one cherry-picked example proves
        nothing. This returns the COMPLETE list. There is no truncation
        and no sampling.

        `status` optionally filters to one DecisionStatus. An unknown
        status raises rather than silently returning an empty list --
        an empty list would read as "no exceptions of that kind", which
        is a materially different and false claim.
        """
        if status is not None:
            valid = {s.value for s in DecisionStatus}
            if status not in valid:
                raise ValueError(
                    f"Unknown status {status!r}. Valid: {sorted(valid)}"
                )

        items = []
        for decision in self.decisions:
            if decision.status in RESOLVED_STATUSES:
                continue
            if status is not None and decision.status.value != status:
                continue

            items.append({
                "txn_id": decision.txn_id,
                "status": decision.status.value,
                "exception_code": decision.exception_code.value,
                "reason_codes": [c.value for c in decision.reason_codes],
                "confidence_score": decision.confidence_score,
                "confidence_tier": self._tier_by_txn.get(decision.txn_id),
                "matched_sources": list(decision.matched_sources),
                "tax_verified": decision.tax_verified,
            })

        items.sort(key=lambda item: item["txn_id"])

        return {
            "filter_status": status,
            "count": len(items),
            "total_records": len(self.decisions),
            "exceptions": items,
        }

    # ------------------------------------------------------------------
    # TOOL 3 -- EVIDENCE
    # ------------------------------------------------------------------

    def get_evidence(self, txn_id: str) -> dict[str, Any]:
        """
        The full audit trail behind ONE decision.

        Raises TxnNotFoundError for an unknown ID. That is deliberate:
        Step 3 lets a language model supply this argument, and a model
        can propose an ID that does not exist. The only safe responses
        are the real record or an explicit "no such transaction".
        Returning an empty dict would let a plausible-sounding but
        empty answer reach an operator.
        """
        decision = self._by_txn.get(txn_id)

        if decision is None:
            raise TxnNotFoundError(
                f"No transaction {txn_id!r} in the current batch "
                f"({len(self.decisions)} records)."
            )

        evidence = decision.evidence or {}

        return {
            "txn_id": decision.txn_id,
            "status": decision.status.value,
            "exception_code": decision.exception_code.value,
            "reason_codes": [c.value for c in decision.reason_codes],
            "confidence_score": decision.confidence_score,
            "confidence_tier": self._tier_by_txn.get(decision.txn_id),
            "matched_sources": list(decision.matched_sources),
            "tax_verified": decision.tax_verified,
            "tax_evaluated": evidence.get("tax_evaluated"),
            "matched_rule": evidence.get("matched_rule"),
            "selection_reason": evidence.get("selection_reason"),
            "decision_context": _jsonable(evidence.get("context")),
            "match_signals": _jsonable(evidence.get("match_signals")),
        }

    # ------------------------------------------------------------------
    # TOOL 4 -- THROUGHPUT
    # ------------------------------------------------------------------

    def get_throughput_report(self) -> dict[str, Any]:
        """
        Measured throughput from `data/throughput_benchmark.json`.

        Track 04 lists throughput as one of three bar requirements, so
        the agent needs to be able to answer it directly.

        This READS a recorded benchmark; it does not time anything now.
        A number produced during a demo on a loaded laptop would be
        worse evidence than the recorded sweep, and presenting a live
        figure as though it were the benchmark would be misleading.

        Missing file returns available=False rather than raising. An
        absent benchmark is a gap in evidence, not a broken tool, and
        the agent should be able to say so plainly.
        """
        if not self.throughput_path.exists():
            return {
                "available": False,
                "reason": (
                    f"No benchmark at {self.throughput_path.name}. "
                    "Run scripts/benchmark_throughput.py."
                ),
                "runs": [],
            }

        with self.throughput_path.open("r", encoding="utf-8") as handle:
            runs = json.load(handle)

        if not isinstance(runs, list) or not runs:
            return {
                "available": False,
                "reason": "Benchmark file present but contains no runs.",
                "runs": [],
            }

        peak = max(runs, key=lambda r: r.get("records_per_second", 0))

        # FIX (Q5): the run closest in size to the batch actually
        # loaded, surfaced as the headline figure.
        #
        # Found by real-model verification: asked "how fast did the
        # pipeline process THIS batch?", the model correctly answered
        # that the data contained benchmark sweeps rather than a figure
        # for the current batch. The tool did not answer the question
        # it was described as answering.
        #
        # The sweep is still returned as scaling context, but the
        # closest run now leads, so a question about the current batch
        # gets a figure about a comparable batch.
        batch_size = len(self.decisions)

        closest = min(
            runs,
            key=lambda r: abs(r.get("n_records", 0) - batch_size),
        )

        return {
            "available": True,
            "source": str(self.throughput_path.name),
            "current_batch_records": batch_size,
            "closest_benchmark_batch_size": closest.get("n_records"),
            "closest_benchmark_records_per_second": closest.get(
                "records_per_second"
            ),
            "closest_benchmark_total_seconds": closest.get("total_time_s"),
            "closest_benchmark_stage_seconds": {
                "load": closest.get("load_time_s"),
                "normalize": closest.get("normalize_time_s"),
                "match": closest.get("match_time_s"),
                "decide": closest.get("decide_time_s"),
            },
            "batch_sizes": [r.get("n_records") for r in runs],
            "peak_records_per_second": peak.get("records_per_second"),
            "peak_at_batch_size": peak.get("n_records"),
            "runs": runs,
            "caveat": (
                "Recorded benchmark on generated data on one machine, "
                "not a live timing of the current run and not a "
                "production capacity guarantee."
            ),
        }


# ======================================================================
# HELPERS
# ======================================================================

def _jsonable(value: Any) -> Any:
    """
    Convert evidence values into JSON-safe primitives.

    Evidence dicts carry enums, Decimals and dataclass-derived objects.
    The layer above this builds prompts and JSON payloads, so the
    conversion has to happen somewhere. Doing it here makes it explicit
    and testable rather than an incidental str() somewhere upstream.
    """
    if value is None:
        return None

    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]

    if isinstance(value, (DecisionStatus, ExceptionCode)):
        return value.value

    if isinstance(value, (str, int, float, bool)):
        return value

    if hasattr(value, "value"):          # other enums
        return _jsonable(value.value)

    return str(value)