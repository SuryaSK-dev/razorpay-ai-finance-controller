# src/agent/tools/query_tools.py
"""
Phase 6 — Read-only query tools over deterministic decisions.

WHAT THIS IS
------------
Five functions that answer the questions Track 04 requires a finance
operator to be able to ask:

    get_match_rate()             -- what matched, and how confidently
    get_exceptions(status=None)  -- what did NOT resolve, itemised
    get_evidence(txn_id)         -- why one specific decision was made
    get_cash_position()          -- where the MONEY is, in rupees
    get_throughput_report()      -- how fast the batch processed

Track 04 is "run the books AND THE CASH POSITION". The first three run
the books. get_cash_position() is the other half, and it is the only
tool that reports value rather than record counts -- twenty clean small
settlements and one blocked large one is a very different cash position
from the reverse, and both read as "20 matched, 1 blocked".

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

from decimal import Decimal

from src.models import DecisionStatus, ExceptionCode, MatchDecision
from src.config import money
from src.financial import settlement_expected_net
from src.ingestion.loader import load_batch
from src.normalization.engine import normalize_batch
from src.matching.engine import run_matching
from src.exceptions.manager import decide_batch
from src.exceptions.decision_table import DECISION_TABLE


ROOT = Path(__file__).resolve().parent.parent.parent.parent

DEFAULT_RAW_DIR = ROOT / "data" / "raw"
DEFAULT_THROUGHPUT_PATH = ROOT / "data" / "throughput_benchmark.json"


# Statuses that represent a resolved, no-action-needed outcome.
# Everything else is an exception a human may need to look at.
RESOLVED_STATUSES = frozenset({DecisionStatus.MATCHED})


# Where each decision status places a transaction's money.
#
# Every DecisionStatus must appear exactly once. A status with no bucket
# would silently drop real money out of the cash position, and a status
# in two buckets would double-count it -- both make the totals stop
# reconciling, which is the one property that makes this report
# checkable by hand. test_every_status_has_exactly_one_cash_bucket
# enforces exhaustiveness against the enum itself.
CASH_BUCKET_BY_STATUS: dict[DecisionStatus, str] = {
    DecisionStatus.MATCHED: "settled_and_verified",
    DecisionStatus.PARTIAL_MATCH: "awaiting_verification",
    DecisionStatus.HUMAN_REVIEW: "blocked_in_exceptions",
    DecisionStatus.AMBIGUOUS: "blocked_in_exceptions",
    DecisionStatus.TAX_MISMATCH: "blocked_in_exceptions",
    DecisionStatus.UNMATCHED: "not_yet_credited",
}

CASH_BUCKETS = (
    "settled_and_verified",
    "awaiting_verification",
    "blocked_in_exceptions",
    "not_yet_credited",
)


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
        self._match_by_txn: dict[str, Any] = {}

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

        # Records rejected at ingestion never reach decide_batch(), so
        # they are absent from every count below. get_cash_position()
        # discloses them explicitly rather than letting them vanish: a
        # corrupted record is money whose position is UNKNOWN, and an
        # unknown is a different fact from a zero.
        self.ingestion_rejections = batch.total_errors

        self.match_results = run_matching(normalized.records)
        self.decisions = decide_batch(self.match_results)

        self._by_txn = {d.txn_id: d for d in self.decisions}
        # Indexed for the case dossier: every financial value it
        # surfaces is read off the MatchResult that produced the
        # decision, never recomputed.
        self._match_by_txn = {m.txn_id: m for m in self.match_results}

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

    # ------------------------------------------------------------------
    # TRIAGE ORDERING
    # ------------------------------------------------------------------
    #
    # DECISION_TABLE already encodes severity as an explicit priority per
    # rule, authored deliberately and swept over all 2048 context
    # combinations. get_exceptions() used to sort by txn_id -- alphabetical,
    # and the least useful ordering an operator could be handed.
    #
    # The ranking is DERIVED from the table so it cannot drift. Adding a
    # rule reorders triage with no edit here.
    #
    # KEYED ON RULE NAME, NOT EXCEPTION CODE. The obvious mapping,
    #
    #     {rule.exception_code: rule.priority for rule in DECISION_TABLE}
    #
    # is lossy: HUMAN_REVIEW_REQUIRED is produced by three different rules
    # at priorities 0, 9 and 11, and a dict keyed on the code keeps only
    # the last one -- so "no candidates at all", the single most severe
    # state in the table, would inherit the catch-all's priority of 11 and
    # sort last. Rule names are unique, every decision records the rule
    # that fired in evidence["matched_rule"], and 61 of 61 carry it. That
    # key is exact.
    _PRIORITY_BY_RULE = {rule.name: rule.priority for rule in DECISION_TABLE}

    # Fallback only, for a decision with no recorded rule: the MOST severe
    # priority any rule emitting that code can carry. Minimum, not last --
    # under-stating severity is the direction that hides work.
    _MIN_PRIORITY_BY_CODE: dict = {}
    for _rule in DECISION_TABLE:
        _code = _rule.exception_code
        if _code not in _MIN_PRIORITY_BY_CODE:
            _MIN_PRIORITY_BY_CODE[_code] = _rule.priority
        else:
            _MIN_PRIORITY_BY_CODE[_code] = min(
                _MIN_PRIORITY_BY_CODE[_code], _rule.priority
            )

    def _policy_priority(self, decision: MatchDecision) -> int:
        rule = (decision.evidence or {}).get("matched_rule")
        if rule in self._PRIORITY_BY_RULE:
            return self._PRIORITY_BY_RULE[rule]
        return self._MIN_PRIORITY_BY_CODE.get(decision.exception_code, 99)

    # ------------------------------------------------------------------
    # THE CASE DOSSIER
    # ------------------------------------------------------------------
    #
    # Before this, an exception row carried eight fields and not one of
    # them was money. The system could say INR 601,761.49 was blocked
    # across 32 records and could not say which record held how much.
    #
    # That is why every multi-step agent proposal was rejected before
    # submission: an agent asked "what should I work first?" had nothing
    # to reason over. The information model precedes the agent, and this
    # is the field set it was waiting on (FAILURE_LOG.md section 68).
    #
    # Every value here ALREADY EXISTS on the decision or the match
    # result. Nothing is computed. `expected_net` comes from
    # settlement_expected_net() in financial.py -- the one definition
    # test_no_module_re_derives_expected_net_inline exists to protect.
    #
    # Absent is None, never zero. A settlement with no bank counterpart
    # has an UNKNOWN observed amount, and reporting that as 0.00 would
    # make a missing credit look like a zero credit -- section 63.2's
    # lesson, and the same rule the cash position already applies to the
    # two unparseable records.

    @staticmethod
    def _amount(value) -> Optional[str]:
        """Quantised string, or None. Matches get_cash_position()."""
        return None if value is None else str(money(value))

    @staticmethod
    def _day(record) -> Optional[str]:
        if record is None or record.date_utc is None:
            return None
        return record.date_utc.date().isoformat()

    def _dossier(self, decision: MatchDecision) -> dict[str, Any]:
        """
        Per-record financial evidence, read from what already exists.

        Returns None for every field whose source record is absent. The
        provenance block names which source each value came from, so a
        reader never has to guess whether a null means "missing record"
        or "missing field".
        """
        result = self._match_by_txn.get(decision.txn_id)
        if result is None:
            return {
                "expected_net": None,
                "observed_amount": None,
                "variance": None,
                "pg_date": None,
                "bank_date": None,
                "identifiers": {"txn_id": decision.txn_id},
                "provenance": {},
            }

        pg = result.pg_record
        bank = result.bank_record
        invoice = result.invoice_record

        expected = settlement_expected_net(pg) if pg is not None else None
        observed = bank.amount if bank is not None else None
        variance = (
            expected - observed
            if expected is not None and observed is not None
            else None
        )

        raw_bank = (bank.raw_ref or {}) if bank is not None else {}
        raw_invoice = (invoice.raw_ref or {}) if invoice is not None else {}

        return {
            "expected_net": self._amount(expected),
            "observed_amount": self._amount(observed),
            "variance": self._amount(variance),
            "pg_date": self._day(pg),
            "bank_date": self._day(bank),
            "identifiers": {
                "txn_id": decision.txn_id,
                "utr": pg.utr if pg is not None else None,
                "bank_ref": raw_bank.get("bank_ref"),
                "invoice_id": raw_invoice.get("invoice_id"),
            },
            "provenance": {
                "expected_net": "pg" if expected is not None else None,
                "observed_amount": "bank" if observed is not None else None,
                "variance": (
                    "derived: expected_net - observed_amount"
                    if variance is not None else None
                ),
                "pg_date": "pg" if pg is not None else None,
                "bank_date": "bank" if bank is not None else None,
            },
        }

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

            priority = self._policy_priority(decision)
            rule = (decision.evidence or {}).get("matched_rule")

            items.append({
                "txn_id": decision.txn_id,
                "status": decision.status.value,
                "exception_code": decision.exception_code.value,
                "reason_codes": [c.value for c in decision.reason_codes],
                "confidence_score": decision.confidence_score,
                "confidence_tier": self._tier_by_txn.get(decision.txn_id),
                "matched_sources": list(decision.matched_sources),
                "tax_verified": decision.tax_verified,
                "policy_priority": priority,
                "matched_rule": rule,
                "triage_basis": (
                    f"policy priority {priority} ({rule}), "
                    f"confidence {decision.confidence_score}"
                ),
                **self._dossier(decision),
            })

        # Policy severity first, then weakest evidence first within a
        # severity band, then txn_id so the order is total and stable.
        # Confidence ascending is deliberate: two records at the same
        # priority are not equally urgent, and the one the engine was
        # least sure about is the one a human should see first.
        items.sort(
            key=lambda item: (
                item["policy_priority"],
                item["confidence_score"],
                item["txn_id"],
            )
        )

        # Dense 1..N, assigned after the sort so the rank IS the position.
        for position, item in enumerate(items, start=1):
            item["triage_rank"] = position

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
    # TOOL 4 -- CASH POSITION
    # ------------------------------------------------------------------

    def get_cash_position(self) -> dict[str, Any]:
        """
        The batch expressed in rupees rather than record counts.

        Track 04 is "run the books AND THE CASH POSITION". The other
        tools run the books -- what matched, what did not, why. This one
        answers the question a finance controller actually opens a
        reconciliation report to ask: how much money is settled, how
        much is stuck, and does what we expected to move match what the
        bank actually moved.

        A record count cannot answer that. Twenty clean small
        settlements and one blocked large one is a very different cash
        position from the reverse, and both read as "20 matched, 1
        blocked".

        BUCKETS
        -------
        Every decisioned transaction lands in exactly one bucket, keyed
        off its DecisionStatus by CASH_BUCKET_BY_STATUS:

            settled_and_verified   reconciled across sources, tax verified
            awaiting_verification  PARTIAL_MATCH -- tax could not be checked
            blocked_in_exceptions  needs a human before it can be trusted
            not_yet_credited       expected, but no bank credit exists

        `not_yet_credited` is deliberately included at its expected net.
        A missing bank row is money the merchant is OWED and has not
        received -- the single most operationally urgent line in the
        report. Excluding it would understate exposure and would break
        the arithmetic below, which is what makes this number checkable
        by hand.

        WHAT IS AND IS NOT COUNTED
        --------------------------
        Amounts are expected net -- gross minus fee, GST and TDS -- from
        the same src/financial.py definition the matcher and the amount
        control use. There is no second settlement formula here.

        `total_bank_credited` sums the SELECTED bank record per
        transaction. Where a settlement was credited twice, the
        duplicate row is not added again: it is a credit pending
        reversal, not additional expected settlement, and the
        transaction itself appears in blocked_in_exceptions so an
        operator sees it.

        Records rejected at ingestion are reported as a count and NOT
        as an amount. Their gross is unparseable, so their value is
        genuinely unknown -- and an unknown is a different fact from a
        zero. Reporting them as zero would let corrupted money quietly
        balance the books.

        This tool READS. It computes no financial outcome that
        decide_batch() has not already established; it aggregates
        amounts already attached to records the deterministic pipeline
        produced.
        """
        totals = {bucket: Decimal("0") for bucket in CASH_BUCKETS}
        counts = {bucket: 0 for bucket in CASH_BUCKETS}

        total_bank_credited = Decimal("0")
        transactions_with_bank_credit = 0

        for result in self.match_results:
            decision = self._by_txn[result.txn_id]

            bucket = CASH_BUCKET_BY_STATUS[decision.status]

            totals[bucket] += settlement_expected_net(result.pg_record)
            counts[bucket] += 1

            if result.bank_record is not None:
                total_bank_credited += result.bank_record.amount
                transactions_with_bank_credit += 1

        total_expected = sum(totals.values(), Decimal("0"))
        variance = total_expected - total_bank_credited

        return {
            "currency": "INR",
            "total_records": len(self.decisions),
            "by_bucket": {
                bucket: {
                    "amount": str(money(totals[bucket])),
                    "records": counts[bucket],
                }
                for bucket in CASH_BUCKETS
            },
            "total_expected_settlement": str(money(total_expected)),
            "total_bank_credited": str(money(total_bank_credited)),
            "transactions_with_bank_credit": transactions_with_bank_credit,
            "variance_vs_bank_credited": str(money(variance)),
            "records_rejected_at_ingestion": self.ingestion_rejections,
            "rejected_value_note": (
                f"{self.ingestion_rejections} record(s) were rejected at "
                "ingestion and carry no parseable amount. Their value is "
                "unknown and is excluded from every figure above rather "
                "than being counted as zero."
            ),
            "caveat": (
                "Amounts are expected net (gross - fee - GST - TDS) from "
                "the deterministic pipeline. total_bank_credited counts "
                "the selected bank record per transaction; a duplicate "
                "credit is pending reversal, not additional settlement."
            ),
        }

    # ------------------------------------------------------------------
    # TOOL 5 -- THROUGHPUT
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