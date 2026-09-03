# ROADMAP

Post-submission plan for the AI Finance Controller.
Frozen at `submission-final` → `426d4a0`.

**Status update.** V1.0.5, V1.1, V1.3 and V2 have since shipped — recorded
as `FAILURE_LOG.md` §66, §67 and §68. Suite 424 → **455**; decision
snapshot `1392ddf1a3c2ea1c` unchanged throughout. Both findings below are
now closed, and the sections that describe them are kept as written
because they are the evidence for why the ordering was what it was.

Every claim about current behaviour in this document was checked against
the code at that commit. The command or `file:line` is given inline.
Anything not verifiable is marked **UNVERIFIED**.

---

## 1. Where this stands

The system reconciles a 61-record batch across three sources, verifies
GST and TDS, classifies every unresolved record into a typed exception
with the decision rule that produced it, and reports the batch in rupees.
It measures itself: 24/61 matched (39.34%), 55/61 agreeing with
independent ground truth (90.16%), 37 exceptions itemised, throughput
recorded with its O(n²) ceiling disclosed. 455 tests pass from a cold
clone with no API key. The AI layer selects among five read-only tools
and phrases results; it holds no financial authority.

Two findings from the final review round shape everything below, and
neither is cosmetic.

**A — Reconciliation is PG-anchored, so unclaimed bank rows are
invisible.** `run_matching()` produces one `MatchResult` per PG record
(`src/matching/engine.py:924` says so in the docstring), and
`decide_batch()` iterates those results
(`src/exceptions/manager.py:561`). Bank rows are visited only as
candidates. Nothing scans the bank pool for rows that were never
claimed. On the shipped batch: **64 bank rows, 61 PG records, 59 bank
rows selected — 5 never selected, appearing in no decision and no
exception.** §65 closed the negative-amount case at ingestion. The
general case — a well-formed bank credit that no PG record claims — is
open.

**B — The exception payload carries no money.** `get_exceptions()`
returns exactly eight fields (`src/agent/tools/query_tools.py:264-272`):
`txn_id`, `status`, `exception_code`, `reason_codes`,
`confidence_score`, `confidence_tier`, `matched_sources`,
`tax_verified`. No amount, no variance, no date, no age. Exactly one
`"amount"` payload key exists anywhere in that file —
`query_tools.py:412`, a bucket total inside `get_cash_position()`. The
list is sorted by `txn_id` (`query_tools.py:275`).

Finding B is why every multi-step agent proposal was rejected before
submission, and the ordering below follows from it.

---

## 2. The dependency graph

```
                        ┌──────────────────────────────┐
                        │  V1.0.5  completeness        │
                        │  assertion (Finding A)       │
                        └──────────────┬───────────────┘
                                       │ unclaimed rows must be
                                       │ nameable before they can
                                       │ be ranked or aged
                                       ▼
  ┌────────────┐   ┌────────────┐   ┌──────────────────────────────┐
  │ V1.2       │   │ V1.3       │   │  V2   THE CASE DOSSIER       │
  │ saturation │   │ doc sweep  │   │  amount · expected · variance│
  │ metric     │   │ hardening  │   │  dates · identifiers ·       │
  └────────────┘   └────────────┘   │  provenance · evidence refs  │
       independent      independent └───────┬──────────────────────┘
                                            │
                    ┌───────────────────────┼───────────────────────┐
                    ▼                       ▼                       ▼
          ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
          │ V2.1  value-     │   │ V2.2  ageing in  │   │ V3  multi-step   │
          │ weighted         │   │ the cash view    │   │ investigation    │
          │ prioritization   │   │                  │   │                  │
          └────────┬─────────┘   └──────────────────┘   └──────────────────┘
                   │ needs a rank                              needs evidence
                   │ that exists                               to reason over
                   ▼
          ┌──────────────────┐
          │ V1.1  severity   │  ← partially independent: the POLICY rank
          │ rank from        │    ships without V2; the VALUE weight in
          │ DECISION_TABLE   │    V2.1 cannot
          └──────────────────┘

  ┌──────────────────────────────────────────────────────────────────┐
  │ V1.4  evaluation depth — independent of all of the above.        │
  │ Measures what already exists; adds no field and no capability.   │
  └──────────────────────────────────────────────────────────────────┘

  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
  │ V4  N:1 +    │ ───▶ │ V5  audit    │ ───▶ │ V6  prod     │
  │ refunds      │      │ log + state  │      │ adapters     │
  └──────────────┘      └──────────────┘      └──────────────┘
     needs V2 for the      needs V4: a state      needs V5: idempotency
     variance model        machine over an        keys before per-bank
                           incomplete txn         adapters can retry
                           model encodes the
                           wrong states
```

**The graph is the answer to "why isn't your agent more autonomous?"**
V3 is blocked by V2, and the block is checkable rather than asserted:

```bash
python -c "import sys;sys.path.insert(0,'.');\
from src.agent.tools.query_tools import BatchQueryContext;\
print(list(BatchQueryContext().get_exceptions()['exceptions'][0]))"
```

An agent asked *"what should I work first?"* has eight fields to reason
over, none of which is money, a date, or a variance. Multi-step
orchestration over that produces something that sounds analytical and is
guessing.

---

## 3. The items

## V1.0.5 — Reconciliation completeness assertion  ✅ SHIPPED (§66)

**PROBLEM**
Reconciliation is PG-anchored. A well-formed bank credit that no PG
record claims is silently absent from the output. This is Finding A, and
it is the general case that §65 narrowed to negatives only.

**EVIDENCE**
`src/matching/engine.py:924` — *"One MatchResult is produced per
PG-anchored transaction."*
`src/exceptions/manager.py:561` — `for result in match_results:`
Executed against the shipped batch: 64 bank rows, 61 PG records, 59 bank
rows selected as `MatchResult.bank_record`, **5 never selected**. No
decision or exception references them.

**BLOCKED BY**
Nothing. This is first because every later item that ranks, ages or
investigates an exception is incomplete while a class of exception
cannot be named.

**CHANGES**
`src/matching/engine.py` — return the unclaimed-bank-row set alongside
`MatchResult`s, or expose it from `CandidateIndex`.
`src/exceptions/manager.py` — a batch-level completeness result, not a
per-record decision; an unclaimed bank row has no `txn_id` to anchor a
`MatchDecision` to.
`src/agent/tools/query_tools.py` — surface the count and the rows.
`scripts/run_pipeline.py` — print it beside the ingestion rejections.

**INVARIANTS**
None weakened. This is additive. `answer.data ==
getattr(context, tool)(**args)` continues to hold because the new
surface is another deterministic read. No `ground_truth` reference. No
core→agent import. No mutation surface.
**Decision snapshot `1392ddf1a3c2ea1c` must not move** — unclaimed rows
produce no `MatchDecision`, so if the hash changes, the implementation
has leaked into the per-record path and is wrong.

**NEW TESTS**
- Every ingested bank row is either selected by exactly one
  `MatchResult` or present in the unclaimed set. Partition, not overlap.
- On the shipped batch the unclaimed count is exactly 5, and those rows
  are named.
- **The control:** inject a bank row that matches nothing and assert the
  unclaimed count rises by one. A completeness assertion that cannot
  observe an added orphan is the §63 defect again.
- The decision snapshot is unchanged.

**ARTIFACTS**
None regenerate. `accuracy_report.json` measures PG-anchored decisions
and its denominator is unaffected. If any accuracy number moves, the
change is wrong.

**DONE WHEN**
`run_pipeline.py` reports a non-zero unclaimed-bank-row count on the
current batch, the partition test passes, and the decision snapshot is
still `1392ddf1a3c2ea1c`.

**RISK**
The tempting shortcut is to synthesise a `MatchDecision` for an
unclaimed bank row so it appears in `get_exceptions()`. That would
change the 61-record denominator every published percentage rests on.
Unclaimed rows are a **separate report**, not a 62nd decision.

**EFFORT** 6–8h

---

## V1.1 — Severity-ranked exceptions  ✅ SHIPPED (§67)

**PROBLEM**
`get_exceptions()` sorts by `txn_id` (`query_tools.py:275`). An operator
reading the list top-down works in transaction-ID order, which carries
no information. Meanwhile `DECISION_TABLE` already encodes severity as
`priority=0..11` (12 rules, verified by
`grep -c "priority=" src/exceptions/decision_table.py`), and that
ordering is never exposed.

**EVIDENCE**
`src/exceptions/decision_table.py` — priorities 0 through 11, one per
rule.
`src/agent/tools/query_tools.py:275` — `items.sort(key=lambda item: item["txn_id"])`.

**BLOCKED BY**
Nothing for the policy rank. The **value** weight is V2.1 and needs V2.

**CHANGES**
`src/agent/tools/query_tools.py` — derive rank from `DECISION_TABLE` by
importing it, never by restating the order. Add `policy_rank` to the
payload and sort by it, `txn_id` as tiebreak for determinism.

**INVARIANTS**
Unaffected. Reordering a read-only list changes no decision.
The data invariant still holds — `answer.data` is still exactly what a
direct call returns.

**NEW TESTS**
- `policy_rank` for every exception equals the `priority` of the rule
  named in `matched_rule`. Derived, not restated.
- **The control:** a structural test that the ranking is not a literal
  list in `query_tools.py` — the §52 shape. If someone hardcodes the
  order, both copies agree and no behavioural test can see it.
- Sort is stable and total: equal ranks fall back to `txn_id`.

**ARTIFACTS**
None. Ordering is not a published metric.

**DONE WHEN**
`get_exceptions()["exceptions"][0]` is the highest-severity exception in
the batch, `policy_rank` is present on every item, and adding a
13th rule to `DECISION_TABLE` changes the ranking with no edit to
`query_tools.py`.

**RISK**
Low. The one real risk is implying that policy rank is *operational*
priority — it is not, until V2.1 adds exposure and ageing. The field
name `policy_rank` says which one it is.

**EFFORT** 3–4h

---

## V1.2 — Saturation observability

**PROBLEM**
§64 established what happens under pool saturation: four concurrent
hangs each fail at the timeout, the fifth returns without starting, and
the pool recovers. What the system cannot do is **say** it is saturated.
It fails every call honestly and reports nothing.

**EVIDENCE**
`FAILURE_LOG.md` §64. `src/agent/guardrails.py:29` —
`ThreadPoolExecutor(max_workers=4)`, module-level.
`tests/test_agent_concurrency.py::test_the_worker_count_is_deliberate`
pins 4 because it is the saturation threshold.

**BLOCKED BY**
Nothing. Independent of the information model.

**CHANGES**
`src/agent/guardrails.py` — a counter of in-flight calls and consecutive
timeouts, read-only from outside. Optionally a circuit breaker that
short-circuits to the deterministic fallback after N consecutive
timeouts, rather than paying the timeout each call.

**INVARIANTS**
All model calls remain inside `call_llm_bounded` — the breaker lives
*in* that function, which is the correct place and keeps
`test_every_model_call_goes_through_the_guardrail` green.
No mutation surface: the counter is not exposed as a tool.

**NEW TESTS**
- Saturate the pool; assert the in-flight gauge reads `max_workers`.
- After the breaker opens, a call returns the fallback **without**
  waiting the full timeout — assert wall-clock, not an exception type.
- **The control:** with the breaker closed, a healthy call still reaches
  the provider. A breaker stuck open is an outage that every
  fallback-path test would pass through silently.

**ARTIFACTS**
None.

**DONE WHEN**
Under four concurrent hangs, a fifth caller returns in materially less
than `AGENT_CALL_TIMEOUT_SECONDS`, and the saturation state is
inspectable without an LLM call.

**RISK**
A breaker that opens on transient failure degrades a working system.
Threshold and reset must be tested, and the closed-path control above is
what stops that shipping silently.

**EFFORT** 6h

---

## V1.3 — Documentation-sweep hardening  ✅ SHIPPED (§67)

**PROBLEM**
`scripts/report_accuracy.py:305` prints *"exhaustively tested over all
512 boolean"*. The sweep is 2048 = 2¹¹ — `DecisionContext` has 11
fields. The number-consistency sweep run before submission covered
`README.md`, `ARCHITECTURE.md` and `FAILURE_LOG.md`, and did not include
`scripts/`. A judge running `report_accuracy.py` sees the superseded
figure.

**EVIDENCE**
`grep -rn "512" scripts/` → one hit, `report_accuracy.py:305`.
`FAILURE_LOG.md` §9 records the 512 sweep as historical; the coverage
claim is 2048/2048.

**Fixed.** `report_accuracy.py:305` now prints 2048, and the
number-consistency sweep was widened to cover `scripts/` — the directory
it had never included, which is how the stale string survived.

**BLOCKED BY**
Nothing.

**CHANGES**
`scripts/report_accuracy.py` — correct the string.
A new test that greps `scripts/` for published figures against their
artifacts, extending the existing discipline to the directory it missed.

**INVARIANTS**
None affected. String change plus a test.

**NEW TESTS**
- No file under `scripts/` contains a coverage figure other than the one
  `DecisionContext` implies — derived from `len(fields(DecisionContext))`,
  not a literal.
- **The control:** feed the checker a fixture containing `512` and
  assert it fails. A drift sweep that cannot see drift is §63.

**ARTIFACTS**
None. `accuracy_report.json` does not carry the string.

**DONE WHEN**
`grep -rn "512" scripts/` returns nothing, and the new test fails when
`512` is reintroduced anywhere under `scripts/`.

**RISK**
Very low. The only trap is hardcoding `2048` in the test, which
recreates the same drift one level down — compute it from the dataclass.

**EFFORT** 2h

---

## V1.4 — Evaluation depth

**PROBLEM**
Accuracy is reported as a single 55/61. Three things it does not say:
how often the engine **auto-approved wrongly** (the expensive direction),
how the match/exception split moves with the confidence threshold, and
what the fail-open/fail-closed asymmetry the project asserts is actually
worth.

**EVIDENCE**
Computed at `426d4a0`: of 24 records the engine marked `MATCHED`,
ground truth disagreed with **0**. Auto-approval precision **24/24 =
100%**. All 6 divergences are the engine declining a match ground truth
allowed — verified: every divergent record has
`expected_status == "MATCHED"`. The asymmetry is real and currently
unquantified.

**BLOCKED BY**
Nothing. Measures what already exists.

**CHANGES**
`scripts/report_accuracy.py` or a sibling — auto-approval precision as a
first-class figure; a replay across `CONFIDENCE_HIGH_THRESHOLD` values
producing a risk-coverage curve; a cost-weighted score with an explicit,
documented cost ratio for a false auto-match versus an unnecessary
review.

**INVARIANTS**
Ground truth stays out of `src/` — this is evaluation code and must
import nothing into the pipeline.
The threshold replay must not mutate `config.py` at runtime; it
parameterises a call, or it is measuring a global side effect.

**NEW TESTS**
- Auto-approval precision reconciles: `matched_correct + matched_wrong ==
  matched_total`.
- The curve is monotonic where theory says it must be — raising the
  threshold cannot increase the match count.
- **The control:** a synthetic case with a known false auto-match scores
  as one. A precision metric that has never seen a false positive is
  §4's vacuous invariant.
- The cost ratio is a named constant with its rationale, not a literal.

**ARTIFACTS**
New: `data/eval/risk_coverage_curve.json`,
`data/eval/cost_weighted_accuracy.json`. `accuracy_report.json`
regenerates with the precision field added; its existing figures must
not move.

**DONE WHEN**
The report states auto-approval precision with its numerator and
denominator, the curve has ≥4 threshold points, and the cost-weighted
score changes when the cost ratio changes — proving it is weighted
rather than decorative.

**RISK**
A cost ratio chosen to flatter. Pick it from stated finance-ops
reasoning, publish the ratio next to the score, and show the score at
two ratios so the reader can see the sensitivity.

**EFFORT** 10–12h

---

## V2 — The case dossier  ✅ SHIPPED (§68)

**PROBLEM**
Finding B. The exception payload carries no money, no dates, no
variance. An operator cannot triage from it and an agent cannot reason
over it. This is the single blocking item for everything after it.

**EVIDENCE**
`src/agent/tools/query_tools.py:264-272` — the eight fields.
One `"amount"` payload key in the whole file, `query_tools.py:412`,
inside `get_cash_position()`.
The data exists: `MatchDecision.evidence["match_signals"]["amount_bank"]`
carries `pg_expected_net`, `bank_amount` and `delta`. It is computed and
then not surfaced on the exception path.

**BLOCKED BY**
V1.0.5 — an unclaimed bank row has no `txn_id`, so the dossier schema
must accommodate a case that is not PG-anchored. Designing the schema
before that class exists means redesigning it after.

**CHANGES**
`src/agent/tools/query_tools.py` — a `get_case(txn_id)` returning a
typed dossier, and amount/variance/date fields added to
`get_exceptions()` rows.
A new frozen dossier contract, probably `src/agent/tools/case.py`.
`src/exceptions/manager.py` — only if a field is computed and discarded;
prefer surfacing what `evidence` already holds.

**INVARIANTS**
The data invariant holds and gets **stronger** — `get_case` is another
deterministic read, re-derivable by a direct call.
No mutation surface: the dossier is read-only, and
`test_tools_expose_no_mutation_surface` must still pass unchanged.
**Decision snapshot must not move.** If surfacing a field requires
changing what `decide()` writes into `evidence`, the hash changes and
the change needs its own justification — prefer reading from what is
already there.

**NEW TESTS**
- Every field in the dossier is re-derivable from `decide_batch()`
  output; no dossier field is computed in the tool layer. This is the
  §52 guard applied to a new surface.
- Amounts are `Decimal`-derived strings, never floats.
- **The control:** a dossier for a record with no bank match has explicit
  `None`s, not zeros. §63.2 is exactly this mistake, and the
  `financial.py` comment about conflating absent with zero applies
  verbatim.
- The decision snapshot is unchanged.

**ARTIFACTS**
None regenerate. If any published number moves, a field was recomputed
rather than surfaced.

**DONE WHEN**
`get_exceptions()["exceptions"][0]` contains a rupee amount and a date,
and `get_case("TXN_00025")` returns expected vs actual with the variance,
every value traceable to `decide_batch()` output.

**RISK**
Recomputing a value in the tool layer instead of surfacing the one the
engine already produced. That creates a second definition of a financial
quantity — §52, the defect this project's `src/financial.py` exists to
prevent. Every dossier field must be a read.

**EFFORT** 16–20h

---

## V2.1 — Value-weighted deterministic prioritization

**PROBLEM**
Policy severity alone does not order a work queue. A ₹200 timing
difference and a ₹90,000 amount mismatch can carry the same rank. The
cash view already knows 60.2% of value is blocked; the exception list
cannot say which records hold it.

**EVIDENCE**
V1.1 supplies policy rank. V2 supplies exposure. Neither alone orders
the queue.

**BLOCKED BY**
V2 (exposure) and V1.1 (policy rank).

**CHANGES**
`src/agent/tools/query_tools.py` — a deterministic priority score
combining policy rank, rupee exposure and age, with published weights.
`src/config.py` — the weights, because every constant in this system
lives there.

**INVARIANTS**
Unaffected. Ranking is a deterministic read.
**The model must never compute the rank.** It explains an ordering the
engine produced. If the priority ever depends on model output, the
architecture claim is broken.

**NEW TESTS**
- The score is a pure function of dossier fields — same input, same
  score, asserted across a rerun.
- Changing a weight in `config.py` changes the ordering, proving the
  weights are live rather than decorative.
- **The control:** a record with high exposure and low policy severity
  outranks the reverse case at documented weights, and the test names
  the expected order. A scorer never checked against a case where the
  factors disagree is untested.
- No priority field appears in any model-facing contract.

**ARTIFACTS**
New: `data/eval/prioritization_report.json` — the ranked queue with
each component of the score, so the ordering is auditable.

**DONE WHEN**
The top-ranked exception is the highest-exposure record among the
highest-severity class, and swapping the exposure weight to zero
reproduces V1.1's pure policy ordering exactly.

**RISK**
Weights chosen to produce a pleasing demo ordering. Publish them, and
publish the ordering at two weightings.

**EFFORT** 8h

---

## V2.2 — Ageing in the cash view

**PROBLEM**
*"₹601,761.49 blocked"* does not tell an operator whether that is
yesterday's backlog or a month of accumulation. Settlement is a T+1/T+2
business and age is the difference between a queue and a problem.

**EVIDENCE**
`get_cash_position()` buckets by decision status only
(`query_tools.py:412` region). No date appears in the exception payload
(Finding B). `DATE_TOLERANCE_DAYS = 3` exists in `config.py` as the
settlement-lag window, so the threshold concept is already defined.

**BLOCKED BY**
V2 — ageing needs a date on the record, which the payload does not
currently carry.

**CHANGES**
`src/agent/tools/query_tools.py` — age buckets within
`blocked_in_exceptions`, keyed off the PG transaction date against the
batch date.

**INVARIANTS**
Unaffected — additive read.
Bucket totals must still sum to `total_expected_settlement` exactly.
The ageing split is a *subdivision* of the blocked bucket, not a new
top-level bucket.

**NEW TESTS**
- Age buckets sum to the blocked total to the paise.
- A record exactly at the T+2 boundary lands in a named bucket —
  boundary defined, not implied.
- **The control:** an ingestion-rejected record with no parseable date
  is excluded and reported as unknown, never bucketed as age zero. This
  is the same unknown-is-not-zero rule the cash view already applies to
  amount.

**ARTIFACTS**
`get_cash_position()` output changes shape; any doc quoting it
regenerates. Existing totals must not move.

**DONE WHEN**
The cash view reports blocked value split by age against T+2, the splits
sum to `601,761.49` on the current batch, and the total is unchanged.

**RISK**
"Age" is ambiguous — transaction date, settlement date, or first-flagged
date. Pick one, name it in the payload key, and document why.
UNVERIFIED: whether a per-record settlement date distinct from the
transaction timestamp exists in the current contracts; check before
choosing.

**EFFORT** 6h

---

## V3 — Multi-step investigation

**PROBLEM**
The agent answers one question with one tool call
(`src/agent/controller.py` — a single `dispatch()` call site in `ask()`).
It cannot answer *"why is this exception here and what should I do?"*,
which requires assembling evidence across several reads.

**EVIDENCE**
`grep -c "dispatch(" src/agent/controller.py` → 2, of which one is a
docstring mention and one is the call. Level 1 by the definition in
`INTERVIEW_PREP.md` §12.

**BLOCKED BY**
V2, absolutely. This is the item the whole ordering exists to defer.

**CHANGES**
`src/agent/controller.py` — an `investigate(txn_id)` loop with a bounded
step budget.
A per-step verification record.

**INVARIANTS**
**This is the one item that changes an invariant, and the replacement
must be designed, not assumed.**

Today: `answer.data == getattr(context, tool)(**tool_arguments)` — one
answer, one tool, one re-derivable payload.

That does not survive multiple steps, because `answer.data` would have
to be an aggregate. The replacement is a **per-step equivalent**:

```
AgentInvestigation:
    question: str
    steps: tuple[InvestigationStep, ...]
    conclusion: str          # model-authored prose
    conclusion_source: str   # "llm" | "deterministic_fallback"

InvestigationStep:
    tool: str
    arguments: dict
    data: dict               # the real tool output
```

and the invariant becomes, for every step:

```python
all(s.data == getattr(context, s.tool)(**s.arguments) for s in steps)
```

Three properties this must preserve, each needing its own test:

1. **Every step is individually re-derivable.** Aggregation must not
   become a place where a number is synthesised.
2. **The conclusion carries no number absent from the steps.** This is
   §62's faithfulness check applied to an aggregate — the fact set is
   the union of step payloads, which is closed and enumerable, so
   containment works here as it does for `explain()`.
3. **The step budget is bounded and enforced deterministically**, not by
   asking the model to stop.

**NEW TESTS**
- The per-step invariant over a real investigation.
- A lying model in the conclusion step cannot alter any `step.data` —
  the `test_a_lying_model_cannot_corrupt_the_data` shape, one level up.
- **The control:** an investigation whose conclusion cites a figure not
  present in any step is rejected and falls back. Without it the
  faithfulness check is decorative.
- Step budget: a model that keeps requesting tools is stopped by the
  loop.

**ARTIFACTS**
New: a held-out investigation eval with labelled expected step sets, and
a report scoring step selection separately from conclusion quality.

**DONE WHEN**
`investigate("TXN_00025")` returns ≥2 steps, each independently
re-derivable, with a conclusion containing no figure absent from the
steps — and the held-out eval scores step selection against a
deterministic baseline, as `agent_tool_selection_report.json` does today.

**RISK**
The highest-risk item in this roadmap. It replaces the invariant that is
the project's central safety claim. If the per-step property is not
enforced with the same rigour as the current one, the architecture is
weaker than before and the demo is stronger — which is the exact trade
this project has refused throughout.

**EFFORT** 30–40h

---

## V4 — N:1 settlement, refunds, chargebacks

**PROBLEM**
Settlement is modelled 1:1. Real settlement is N:1 — many captures net
into one bank credit, minus refunds, chargebacks and adjustments. §65
refuses negative transaction values at ingestion; it does not implement
them.

**EVIDENCE**
`ARCHITECTURE.md` — the N:1 design note, including the tolerance trap
(₹0.01 per line × 500 lines = ₹5 of "legitimate" drift).
`FAILURE_LOG.md` §65 — the refusal, and why it is a refusal.
`src/models.py` — `SettlementValue` rejects negatives on
`gross_amount`, `credited_amount`, `invoice_amount`.

**BLOCKED BY**
V2 — the variance model. Batch-level tolerance needs a per-line variance
representation that the dossier defines.

**CHANGES**
Candidate generation anchors on `settlement_id` rather than `txn_id`;
`settlement_expected_net()` becomes a sum over line items minus
adjustments; the decision context gains batch-relational states.
Extend the ARCHITECTURE note with the refund netting model before
writing code.

**INVARIANTS**
`SettlementValue` must be **relaxed deliberately**, not deleted — a
negative becomes valid only as an adjustment line inside a settlement
batch, never as a standalone transaction value. The §65 tests must be
rewritten to assert the new boundary, and the rewrite must be visible
in the log.
The decision snapshot **will** move. That is expected here and nowhere
else in this roadmap; it needs a new baseline and a recorded reason.

**NEW TESTS**
- Tolerance is applied once at batch level, never per line. The trap is
  named in ARCHITECTURE; the test must encode it.
- A refund nets against its settlement batch and does not appear as a
  standalone exception.
- **The control:** a refund with no matching settlement batch is still
  refused, as it is today. Relaxing the guard must not reopen §65.

**ARTIFACTS**
Everything regenerates. New ground truth, new accuracy report, new gold
baseline. This is a dataset-level change.

**DONE WHEN**
A settlement batch of N captures reconciles against one bank credit with
tolerance applied once, a refund line nets correctly, and accuracy is
re-measured against regenerated ground truth with the divergence
arithmetic reconciling as it does today.

**RISK**
The largest-scope item here. It touches matching, tax, decision policy
and the dataset simultaneously. Attempting it without V2 means designing
the variance representation twice.

**EFFORT** 60–80h

---

## V5 — Persistence, audit log, exception state machine

**PROBLEM**
The system is stateless. Running it twice produces two independent
results with no record that the first happened, no way to mark an
exception as being worked, and no idempotency guarantee.

**EVIDENCE**
`grep -rn "idempot" src/` → nothing.
`ARCHITECTURE.md` has an Idempotency section stating no state persists
between runs.

**BLOCKED BY**
V4. A state machine over an incomplete transaction model encodes the
wrong states; adding refunds afterwards means migrating them.

**CHANGES**
An append-only event log; replay to reconstruct state; an exception
state machine `OPEN → IN_REVIEW → RESOLVED | REJECTED | ESCALATED`;
idempotency keys on ingestion; an override changelog recording who
overrode what, when, and why.

**INVARIANTS**
**This item conflicts with an existing test and must not resolve it by
deletion.**

`tests/test_query_tools.py:403` —
`test_tools_expose_no_mutation_surface` asserts `BatchQueryContext` has
no write path. A state machine introduces one: something must move an
exception from `OPEN` to `IN_REVIEW`.

The replacement is an **authorization boundary**, not an absence:

- State transitions live on a separate authenticated surface, never on
  `BatchQueryContext`.
- **No transition is reachable from the tool registry**, so the model
  cannot invoke one — the registry gate is what makes this safe, and
  `test_a_mutating_tool_is_not_registrable` replaces the current test.
- Every transition is an append-only event with an actor, so the audit
  log answers *who*, which the current design has no need to ask.

The current test is replaced by a **stronger** pair: the model cannot
reach a mutation, and every mutation is attributable. Deleting it and
claiming the boundary still holds would be §63 verbatim.

**NEW TESTS**
- Replaying the event log reproduces state exactly.
- Re-ingesting an identical batch is a no-op under the idempotency key.
- Invalid transitions are refused (`RESOLVED → OPEN`).
- **The control:** attempting a state transition through `dispatch()`
  fails, and a test asserts no registered tool name maps to a mutating
  method. This is the replacement for the deleted test and must be
  mutation-verified — inject a mutating tool into the registry and prove
  the guard fires.

**ARTIFACTS**
New: an event-log schema fixture and a replay-determinism report.

**DONE WHEN**
An exception can be moved `OPEN → IN_REVIEW → RESOLVED` with each
transition recorded and attributed, replay reproduces final state
exactly, re-ingestion is a no-op, and no transition is reachable from
the model.

**RISK**
The mutation surface is the single most dangerous change in this
document. The registry gate is what keeps the AI boundary intact, and it
must be tested by injection rather than by inspection.

**EFFORT** 40–50h

---

## V6 — Production adapters

**PROBLEM**
Three assumptions hold only for the generated dataset: narration parsing
is one format, `merchant_ytd_gross_opening` is a generated field, and
ambiguity detection is O(n²).

**EVIDENCE**
`data/raw/bank_statement.json` — one narration convention; 0 of 64
narrations contain an internal ID, 58 of 64 carry `bank_ref`.
`ARCHITECTURE.md` "Ledger-backed YTD" — the field is generated;
`src/tax/seller_ledger.py` already reads it per record, so the code
shape does not change.
`data/throughput_benchmark.json` — 1,348.5 rec/s at 60, 179.2 at 5,000;
the collapse is the pairwise ambiguity scan.

**BLOCKED BY**
V5 — idempotency keys must exist before a per-bank adapter can retry
safely.

**CHANGES**
A narration adapter per bank behind one interface; `seller_ledger.py`
reads a ledger port instead of `raw_ref`; candidate blocking on
amount+date before the pairwise scan.

**INVARIANTS**
Blocking must not change which records are flagged ambiguous — it is an
index optimisation, not a policy change. The decision snapshot on the
current dataset **must not move**, and that is the test.

**NEW TESTS**
- Blocked and unblocked ambiguity detection produce identical results on
  the current batch. Same output, different complexity.
- Throughput at n=5,000 improves measurably and is re-recorded.
- **The control:** a deliberately colliding pair in different blocks is
  still detected, or the blocking key is wrong. An optimisation that
  silently narrows detection is worse than the O(n²) it replaces.
- A missing ledger value returns `None` and fails closed — §63.2 must
  not regress when the source changes.

**ARTIFACTS**
`data/throughput_benchmark.json` regenerates. §54 requires re-recording
after any change to the matching path.

**DONE WHEN**
Ambiguity detection is sub-quadratic with byte-identical results on the
current batch, throughput at 5,000 records improves against the recorded
figure, and the decision snapshot is unchanged.

**RISK**
A blocking key that is too narrow drops true ambiguities silently — the
same class as §4, where six records fail-opened while 162 tests passed.
The identical-results test is what makes this safe.

**EFFORT** 30–40h

---

## 4. Explicitly not planned

- **LLM-assisted candidate matching** — accidental net collisions are
  zero, so the shortlist has length one and there is nothing for a model
  to choose between (`FAILURE_LOG.md` §50).
- **Degrading the dataset to give a component more to do** — changing
  data to make a measurement honest is legitimate; changing it to make a
  component look necessary is not.
- **Autonomous financial mutation** — the boundary's credibility is that
  the model cannot reach one. V5 adds mutation for *humans*, gated by
  the registry.
- **RAG, memory, multi-agent orchestration** — none solves a problem
  this system has. The bottleneck is the information model, not context
  or coordination.
- **Forward cash forecasting** — plausible for Track 04, but only with a
  measurement strategy. A forecast without a backtest is a number nobody
  can check, which is the one thing this project has consistently
  refused to publish.

---

## 5. The principle

**The information model precedes the agent.**

Every proposal to make the agent more autonomous failed on the same
missing field. Not on model capability, not on prompt design, not on
orchestration — on the fact that `get_exceptions()` returns eight fields
and none of them is money.

That is why V3 sits behind V2, and the ordering is checkable rather than
preferred:

```bash
python -c "import sys;sys.path.insert(0,'.');\
from src.agent.tools.query_tools import BatchQueryContext;\
print(list(BatchQueryContext().get_exceptions()['exceptions'][0]))"
```

```
['txn_id', 'status', 'exception_code', 'reason_codes',
 'confidence_score', 'confidence_tier', 'matched_sources',
 'tax_verified']
```

An agent given multi-step orchestration over that payload would produce
prioritisation with no exposure, ageing with no dates, and
recommendations with no variance to reason about. It would look more
autonomous and be less correct — and it would be indistinguishable, from
the outside, from one that worked.

The same rule explains V1.0.5's position. Ranking, ageing and
investigation all operate on a set of exceptions. While a class of
exception cannot be named — the unclaimed bank row — every one of those
features is complete over an incomplete set, which is a subtler version
of the same error.

Build the fields. Then build the agent that reads them.
