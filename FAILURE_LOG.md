# Failure Log

## AI Finance Controller — Phases 0 through 6

This is a record of things that went wrong while building this system, how
they were found, and what was done about them.

I am keeping it because the bugs turned out to be the most useful part of
the project. Several of them only showed up because of tests or harnesses
written earlier, and a few of them changed how I think about the whole
design.

A note on tone: I have tried to write this plainly rather than making
each bug sound like a major architectural insight. Some of them were just
mistakes. A few were genuinely interesting. I have tried to be clear about
which is which.

---

## How to read this

Each entry has:

- **What happened**
- **How it was found**
- **Fix**
- **Still open** (where anything remains)

Entries are grouped by phase. Sections 36–40 correct things recorded
earlier in this same log that later turned out to be wrong.

---

# Phase 0–2 — Contracts, data, ingestion

## 1. Raw source formats were leaking into business logic

**What happened.** The first version let matching code read
payment-gateway, bank, and invoice records directly. Every downstream
component then had to know three different schemas.

**Fix.** Added an ingestion layer as a schema boundary and a normalization
layer producing one canonical `NormalizedRecord`. Downstream code never
sees a raw source format.

This was a design decision made early rather than a bug found late, but
it is the reason most later bugs stayed contained to one layer.

---

## 2. The PYT narration pattern silently dropped its prefix

**What happened.** `_NARRATION_PATTERNS` had three regexes. Two wrapped the
whole token in the capture group; the third did not:

```python
r"(TXN_\d{5,8})"        # correct
r"(TXN-\d{4}-\d{4,8})"  # correct
r"PYT_(\d{7,8})"        # bug -- group covers digits only
```

Since `_extract_txn_from_narration` returns `match.group(1)`, the PYT
pattern returned `1234567` instead of `PYT_1234567`.

**How it was found.** A held-out narration test. It never showed up in
normal runs because `PYT_` appears nowhere in the generated data. The
pattern has never fired in production.

**Fix.** `r"(PYT_\d{7,8})"`.

**Worth noting.** No result was ever affected, because the code path is
dead against our own dataset. It was found only because a held-out test
exercised something the main data does not. That is the clearest
argument I have for keeping held-out tests around.

---

# Phase 3 — Matching

## 3. Fuzzy candidate guard compared the wrong amount

**What happened.** The fuzzy tier reported `TP = 0` — it never recovered
the corrupted-reference cases it was built for.

**Root cause.** The amount guard compared PG *gross* against bank *net*.
These differ by fee, GST, and TDS, so the correct candidate was rejected
before fuzzy similarity was even computed.

**Fix.** Compare expected net (`gross − fee − GST − TDS`) against the bank
credited amount.

**After.** `TP = 6`, `FN = 0`, recall 1.00 on the six synthetic cases.

The precision number reported alongside this was later found to be
meaningless, and the tier turned out to be unreachable in practice —
see sections 36–39 and 41. The gross/net fix itself was real and correct.

---

## 4. Ambiguity was flagged but the data never contained any

This is the worst bug in the project and the one I learned the most from.

**What happened.** The `ambiguous` category is supposed to produce two
transactions that genuinely compete — same amount, same date — so the
matcher cannot safely pick one. Ground truth said `AMBIGUOUS` for six
records.

The generator created the sibling by copying only the PG fields:

```python
s_pg["gross_amount"] = pg["gross_amount"]
s_pg["timestamp"]    = pg["timestamp"]
```

Ambiguity detection compares the anchor PG's expected net against
candidate **bank** amounts. The sibling's bank row was never synced, and
it belonged to a different merchant with a different TDS position, so its
net did not match. **No competing bank record existed.**

Result: six transactions were being auto-matched `MATCHED` with no
exception code, when ground truth said they should have gone to a human.
Six fail-open cases.

**How it was found.** Not by the unit tests. The full-batch gold baseline
harness flagged them as divergences. At the time, **162 unit tests were
passing**, including `test_ambiguous_result_never_auto_matchable`.

That test was not wrong. It guards *"if a result is flagged ambiguous, it
can never be auto-matched."* Nothing was ever flagged, so the invariant
was never violated. It had no way to check whether ambiguity was being
**detected** in the first place.

**Fix.** `build_ambiguous_sibling()` now derives everything from the
counterpart's gross and mirrors its bank `credited_amount` and
`value_date`. Both members are drawn from merchants that withhold zero
TDS, so identical gross gives identical net. Added a generation-time
check that fails loudly if any ambiguous record has no colliding sibling.

**What I took from this.** A passing test suite told me nothing about
whether a condition my ground truth asserted actually existed in my data.
Only running the whole batch and comparing against ground truth caught
it.

**Still open.** Forcing both pair members into the zero-TDS merchant
cohort is the generator being shaped to fit the measurement. It is a
normal way to isolate a variable, but it does mean the ambiguous category
can never exercise TDS. Recorded here rather than left implicit.

---

# Phase 4 — Tax and decisions

## 5. GST and TDS were suppressing each other

**What happened.** The context logic was effectively:

```python
tds_mismatch = not tax.tds_verified and tax.gst_verified
```

So if GST was already wrong, a TDS error was silently dropped.

**Why it matters.** These are independent statutory controls. A
transaction can genuinely have both. Reporting one hides the other from
whoever has to fix it.

**Fix.** Evaluated independently. A record with both now produces
`TAX_MISMATCH` while `reason_codes` keeps `ERR_GST_MISMATCH` and
`ERR_TDS_VARIANCE`.

---

## 6. Only one violation was surviving into the output

**What happened.** The decision output kept only the winning exception
code. That is fine as a status, but useless as audit evidence.

**Fix.** Split the two ideas apart. `status` and `exception_code` are the
classification; `reason_codes` is the complete set of everything that was
wrong. Added `_all_violated_codes()`.

---

## 7. Decision logic was an if/elif chain

**What happened.** The exception manager grew ad-hoc branching. Policy
was hidden in the order someone happened to write conditions.

**Fix.** Replaced with a priority-ordered `DECISION_TABLE` — a list of
rules with explicit priorities, evaluated in order. Policy became
inspectable data instead of control flow.

---

## 8. `fully_clean` was missing from the context

A rule read `context.fully_clean`, but the construction path did not
always set it. `AttributeError` when that rule was hit.

**Fix.** Added it as an explicit field and set it at construction.

Small bug, but it showed the policy definition and the context builder had
drifted apart. They now share one typed contract.

---

## 9. The 512-combination sweep found a state with no rule

Testing all 2⁹ boolean combinations found one with no matching rule — all
flags false including `fully_clean`.

**Fix.** Added a lowest-priority catch-all producing `HUMAN_REVIEW`, so an
unexpected state cannot crash or silently produce an undefined outcome.

---

## 10. The catch-all then fired on real data

**What happened.** The catch-all was supposed to be unreachable. It fired
on the real generated batch.

**Root cause.** `missing_bank` and `missing_invoice` were derived from
`match_result.sources_present`, while tax verification gated on
`match_result.bank_record` / `invoice_record`. Two sources of truth for
the same fact. When they disagreed, the context became internally
inconsistent.

**Fix.** Both now derive from the record objects directly. Added a
real-batch regression asserting the catch-all never fires.

This is the one where a safety net intended for theoretical states caught
a real defect. Worth having.

---

## 11. Throughput was claimed but never measured

**Fix.** `scripts/benchmark_throughput.py`, measuring load / normalize /
match / decide across 60, 300, 1000, 5000 records, writing
`data/throughput_benchmark.json`.

**Still open.** This is a benchmark on generated data on one machine. It
is not a production capacity claim.

---

## 12. C3 — the amount check was gated on confidence derived from the amount

**What happened.** `amount_control_evaluable` and `tax_evaluable` both
required `not low_confidence`. But `low_confidence` comes from
`normalized_score`, which includes `SCORE_AMOUNT_BANK`.

So an amount discrepancy pulled confidence down, and the low confidence
was then used to skip the check that would have reported the discrepancy.
A genuine `AMOUNT_MISMATCH` quietly became a generic
`REFERENCE_MISMATCH`.

**Fix.** Removed the confidence gate from both. Identity ("is this the
right candidate") and financial correctness ("does the amount reconcile")
are different questions. A candidate needs unambiguous, non-duplicate,
present-source identity for the amount comparison to mean something —
confidence tier is not a precondition.

**Effect.** Gold baseline divergences dropped from 19 to 11. Eight cases —
the whole `amount_fee_discrepancy` block — had been reporting the wrong
exception code.

---

## 13. C4 — `tax_verified` was True on records where tax never ran

**What happened.** `gst_mismatch`, `tds_mismatch`, and `tax_unverifiable`
all default to `False`. When tax verification is skipped — missing
invoice, ambiguous identity, duplicate identity — they stay `False`, and
`tax_verified` was computed straight from them:

```python
tax_verified = (not gst_mismatch and not tds_mismatch and not tax_unverifiable)
```

So a `PARTIAL_MATCH` with **no invoice at all** reported
`tax_verified=True`.

**Why it matters.** Status was always right, so no decision was wrong.
But `tax_verified` feeds the explanation contracts — the agent could tell
someone that tax is verified on a transaction that has no invoice to
verify against.

**Fix.** Made it three-state:

```
True  -- verify_tax() ran, everything passed
False -- verify_tax() ran, something failed
None  -- verify_tax() never ran; nothing is claimed
```

Added `_tax_was_evaluated()` mirroring the gate exactly, and
`tax_evaluated` to the evidence dict. Ten tests in
`tests/test_tax_verified_states.py`, including a cross-check that the
evidence flag and the reported value can never disagree.

---

# Phase 1 (revisited) — ground truth was wrong twice

Both of these made a **correct engine look broken**, which cost more
review time than an actual bug would have.

## 14. L1 — duplicates were labelled AMBIGUOUS

Ground truth said `AMBIGUOUS` for the duplicate category. The decision
table maps `duplicate_detected` to `HUMAN_REVIEW / DUPLICATE_DETECTED` at
priority 1, and always has.

The exception code matched; only the status differed. That is about as
clear a signal as you get that the label is wrong, not the code.

**Fix.** Label changed to `HUMAN_REVIEW`.

A duplicate is not an ambiguity. Ambiguity is two different transactions
competing; duplication is one transaction appearing twice. Different
fixes: disambiguate versus reverse a row.

---

## 15. L2 — "unresolvable" was labelled UNMATCHED

`build_unresolvable` emits all three sources and deliberately keeps
`bank_ref` intact. `UNMATCHED` requires `no_candidates_found`, which
requires bank **and** invoice both absent. The label described something
the builder never produces.

The engine found the counterpart and reported the ₹50 shortfall as
`AMOUNT_MISMATCH / HUMAN_REVIEW`, which is both correct and more useful.

**Fix.** Label corrected.

**Prevention.** Added `_verify_label_reachability()` to the generator and
`tests/test_ground_truth_labels.py`, which introspects `DECISION_TABLE`
and asserts no ground-truth entry claims a status no rule produces. That
would have caught both of these at generation time instead of three
harness runs later.

**Still open.** The category is called `unresolvable` but is resolvable by
identity — only irreconcilable without a human. `degraded_signals` would
be a more honest name. Not renaming mid-submission to avoid churning case
IDs.

---

## 16. A1 — six fixed amounts were manufacturing collisions

**What happened.** Gross was drawn from six fixed values. With 63 records
that put roughly ten transactions on each value, and because fee and GST
are fixed percentages, identical gross gave identical expected net.

Consequences: nine bank rows landed on exactly ₹12,205.00; the date
window was doing nearly all the discriminating work in ambiguity
detection; unrelated categories supplied accidental ambiguity evidence to
each other.

**Fix.** Log-uniform draw at paise precision across three orders of
magnitude. Added a generation-time counter:

```
accidental net collisions (non-ambiguous): 0 record(s) across 0 amount(s)
```

Deliberate collisions are unaffected — the ambiguous sibling copies its
counterpart's gross explicitly, and the duplicate bank row is a dict copy.

---

# Phase 5A — the AI boundary

## 17. The first timeout did not actually time out

**What happened.** The guardrail measured elapsed time *after* the call
returned. A 15-second call blocked for 15 seconds and was then declared
too slow.

**How it was found.** A regression test using a function that sleeps 15
seconds, requiring return within 12.

```
Pipeline waited 15.0s for a call that should have timed out at 10s
```

**Fix.** `ThreadPoolExecutor` with `future.result(timeout=...)`. The
pipeline now returns at the configured timeout regardless of what the
underlying thread is doing.

**Honest limitation.** Python cannot forcibly kill a running thread. The
guarantee is *"the pipeline does not wait"*, not *"the provider call is
terminated"*.

---

## 18. AI outputs were bare strings

Narration extraction returned a string; explanation returned a string. A
string does not carry an authority boundary — future code could treat it
as a decision.

**Fix.** Frozen dataclasses `NarrationExtraction` and `Explanation`, with
no fields for amount, GST, TDS, status, or exception code. There is no
field through which the model could express a financial fact. A test
greps field names for financial vocabulary.

---

## 19. Confidence was fabricated

`NarrationExtraction` shipped with `confidence_hint = "medium"` while the
prompt never asked for or validated confidence. That is a made-up number
in a field that looks like a measurement.

**Fix.** `"unspecified"` until there is a real signal to put there.

---

## 20. The contract existed but the real path bypassed it

`candidate_lookup` still accepted a raw string while the contract was
tested separately. A tested boundary that production does not go through
is not a boundary.

**Fix.** Lookup now consumes `NarrationExtraction`. Tests migrated to
construct the typed object.

---

## 21. Smaller interface bugs after the contract migration

- Tests still asserted `result.value == "TXN_00042"` instead of
  `result.value.proposed_txn_id`
- An invariant test passed a raw string into the migrated lookup →
  `AttributeError: 'str' object has no attribute 'proposed_txn_id'`
- The demo printed the whole dataclass repr instead of `fallback.text`

All straightforward, all caused by changing an interface without changing
every call site.

---

# Phase 5B — real model integration

## 22. What is built

- `src/agent/providers/base.py` — `LLMProvider` ABC, `ProviderResponse`
  with no financial fields
- `src/agent/providers/gemini_provider.py` — real `google-genai` calls to
  Gemini 3.1 Flash-Lite, real token accounting from `usage_metadata`
- `src/agent/config.py` — credentials from environment, hard failure if
  `AGENT_FREE_ONLY` is disabled

Real recorded runs are in `data/eval/real_gemini_explanation_run_5C4.json`
with genuine wall-clock latencies.

**Measured.** On 8 held-out explanation cases: 8/8 status preserved, 8/8
amounts preserved, 8/8 tax preserved, 0 unsupported claims, 0
safety-critical failures.

**Honest limitation.** Eight cases is a small sample. These are real
measurements but closer to anecdotes than statistics. Expanding the
held-out set is outstanding work.

---

## 23. The provider test could not run without secrets

`test_gemini_provider_rejects_empty_prompt` called `load_agent_config()`
just to check that an empty prompt is rejected — pure input validation,
no network needed. Without `GEMINI_API_KEY` set, the whole test errored.

That meant the suite could not run in CI, and anyone cloning the repo
without a key would see a red test.

**Fix.** Construct `AgentConfig` directly in the test. Verified by running
the whole suite with `GEMINI_API_KEY=` unset — no test depends on it now.

---

## 24. LLM-assisted candidate matching is still not connected

`find_bank_candidates_with_llm_assist()` exists in `candidates.py` and is
**not on the live path**.

So the honest summary of what the model does today: it selects which
question to answer, phrases results, and explains decisions. It does not
participate in matching, so its contribution to any financial outcome is
zero by design.

Before connecting it, these need to hold:

- deterministic tiers keep priority; LLM only runs after they fail
- proposed ID must exist in the deterministic index
- amount and date guards still enforced
- LLM failure produces an identical decision to LLM-off
- false-positive rate measured on held-out narrations

Upgrade B gave it something concrete to do: one bank row now carries a
UPI reference and no UTR at all, which no regex can recover.

**Still open.**

---

# Phase 5C — evaluation

## 25. `CaseResult` gained a required field and the tests did not follow

Four tests failed with `TypeError: CaseResult.__init__() missing 1
required positional argument: 'outcome'`.

The field itself was a good change — it separates a provider failure
(timeout, quota) from a genuine model abstention, so an outage cannot
masquerade as the model being appropriately cautious.

**Fix.** Updated the four constructions. One test also needed its
*assertion* changed: `test_failed_provider_call_is_recorded` asserted
`abstentions == 1` for a provider failure, which encodes the old, wrong
semantics. It is now `0`.

---

## 26. The per-case E2E harness cannot see batch-relational properties

**What happened.** Six ambiguous cases showed as divergences even after
the generator was fixed and ambiguity detection was verified working.

**Root cause.** `execute_case()` writes one transaction's records into a
private directory and runs the pipeline over that directory alone. In a
batch of one, the bank pool contains a single row — its own. Ambiguity
means *"another plausible record exists elsewhere in the batch"*. It
cannot exist by construction.

`diagnose_ambiguity.py` confirmed it directly: run over the full batch,
all six report `is_ambiguous=True` with 2–3 pieces of bank ambiguity
evidence each. Same engine, same data, same code — only the pool size
differs.

**Fix.** Those categories are reported as `NOT_EVALUABLE_PER_CASE` rather
than counted as engine divergence, with `raw_divergent_cases` kept in the
report so the exclusion is arithmetically transparent. Tests assert that
`raw == divergent + not_evaluable + known_policy`, and that only declared
categories can be excluded.

**Still open.** The proper fix is giving each case the rest of the batch
as context. Deferred.

---

## 27. A test had unreachable assertions on keys that never existed

`test_no_baseline_divergence` asserted
`coverage["missing_actual_cases"]` and
`coverage["unexpected_actual_decisions"]`. The verifier has never emitted
either key — the real names are `missing_execution_cases` and
`unexpected_execution_cases`.

Those lines never ran, because `assert divergent_cases == 0` above them
always failed first. The moment divergences hit zero, the test would have
crashed with `KeyError`.

**Fix.** Corrected the key names.

A test that fails for the wrong reason still looks red, so this stayed
invisible for as long as something else was broken.

---

## 28. A `.pyc` file was tracked in git

`src/__pycache__/config.cpython-311.pyc` was committed before
`.gitignore` existed. `.gitignore` does not retroactively untrack.

**Fix.** `git rm --cached`.

---

## 29. This log fell a milestone behind the code

For a period, `FAILURE_LOG.md` stated that real-model integration was
future work while the repository contained a working Gemini provider and
completed evaluation runs. Anyone reading the log first and the code
second would have found a direct contradiction.

Related: version strings. Prose and code comments referred to `v0.6` and
`v0.8` milestones that do not exist. The real git tags have always been
`phase-3-final`, `phase-4-final`, `phase-5-boundary`, `phase-5-final`,
`phase-6-final`.

**Fix.** Rewritten, twice now. Tags referenced consistently.

An earlier version of this log recorded, as a lesson, *"documentation had
to catch up with the engineering reality."* Then the same thing happened
again to the document that recorded it.

---

# Phase 6 — the agent

## 30. What was actually missing

Everything up to Phase 5C was a reconciliation engine with a model
attached to one narrow task: rephrasing a decision that had already been
made. There was no orchestration, no tool selection, and no way for a
person to ask a question. A human ran six shell scripts and read JSON.

Track 04 asks for *"an agent that closes one finance-ops loop… reporting
its match rate and the exceptions it could not resolve."* What existed
was a model call inside a guardrail, which is not the same thing.

Recording this as a failure rather than a roadmap item because it went
unstated for a long time while the surrounding work got more and more
polished. The gap was not technical difficulty. It was that the
interesting problems were all in the deterministic core, so that is where
the effort went.

---

## 31. What Phase 6 built

Five steps:

```
query_tools.py     four read-only tools over decide_batch() output
registry.py        tool specs, strict argument validation, envelopes
tool_selection.py  frozen ToolSelection contract and strict parser
controller.ask()   select -> dispatch -> phrase
demo_agent.py      the real demo path
```

Two model calls per question, neither able to produce a number:

1. **Selection** — the model reads the question and the tool catalogue.
   It has no access to the data. It cannot answer; it can only choose
   which question to ask the deterministic layer.

2. **Phrasing** — the model receives the real tool output and writes it
   in English, instructed to use only the numbers given.

Between them sits `dispatch()`, which runs the real tool. Every number in
an answer comes from `decide_batch()` via `BatchQueryContext`. The raw
result is returned alongside the prose in `AgentAnswer.data`, so the
phrasing can always be checked against the numbers it describes.

Verified against a real model: six questions through Gemini 3.1
Flash-Lite, data invariant held 6/6, tool selection matched expectation
6/6. Re-verified live inside the demo after every answer.

---

## 32. The tool answered a question nobody asked

The best finding of Phase 6, and it came from the real model rather than
from a test.

Asked *"How fast did the pipeline process this batch?"*, the agent
selected `get_throughput_report` — correct — and then answered:

> The provided data does not contain a live timing for the current batch
> of 61 records. It only provides a benchmark for a batch size of 60
> records…

The model was right. `get_throughput_report()` returned a sweep across
60/300/1000/5000 records with a peak figure. The operator asked about
*this* batch of 61. Nothing in the payload addressed that.

The tool did not answer the question its own description claimed it
answered, and 287 tests did not notice, because every one of them checked
that the tool returned the data it was written to return. None checked
whether that data answered anything.

**Fix: the tool, not the prompt.** `get_throughput_report()` now leads
with the recorded run closest in size to the loaded batch, then the sweep
as scaling context. Prompting the model to be less honest about a gap in
its evidence would have been the wrong direction entirely.

The general shape is worth keeping: a model with no stake in the answer
looked at a payload and said it did not contain what was asked for. A
test suite cannot do that, because a test asserts what the author already
believed.

---

## 33. The demo script demonstrated nothing for a long time

`scripts/demo_agent.py` — the most obviously-named file in the
repository, the one anyone opens first — called a hardcoded `mock_llm()`
returning a canned string, under a docstring describing a "GPT call when
wiring in credentials."

That was accurate when written. It stayed in place through the entire
Gemini integration and the whole evaluation framework. For that period
the repository contained a working real-model provider, recorded live
runs, and a demo that used neither.

Nothing about it was hidden. It was simply never revisited, because it
was not blocking anything.

Now rewritten: real provider, real batch, five questions, and the data
invariant re-verified after every answer. An `--offline` mode runs the
same loop with a keyword stub for anyone without credentials, labelled as
a stub at every point the distinction could be misread.

---

## 34. The tests are hermetic; the scripts are not

Phase 5B deliberately made the test suite runnable without credentials
(section 23). All tests pass with `GEMINI_API_KEY` unset.

The scripts were never given the same treatment, and nothing said so.
Running `verify_agent_ask_real_model.py` without exporting `.env`
produces:

```
RuntimeError: GEMINI_API_KEY is not configured.
```

with no indication that `.env` exists and needs sourcing. We hit this
ourselves immediately after writing the script.

Partly addressed: `demo_agent.py` now falls back to offline mode and
prints the export command. The other real-model scripts still fail
bluntly, and the README documents the requirement rather than the code
handling it.

Recorded rather than fixed, because adding `load_dotenv()` would put a
dependency and a behaviour change into Phase 5 files that are otherwise
frozen and green.

---

## 35. Measured accuracy, and what it does not mean

`scripts/report_accuracy.py` compares full-batch `decide_batch()` output
against `ground_truth.json`, which the pipeline never reads.

Current result, after Upgrade B:

```
Ground-truth entries    : 63
Rejected at ingestion   : 2 (corrupted, never decisioned)
Decisions evaluated     : 61

STATUS accuracy         : 55/61 (90.16%)
EXCEPTION-CODE accuracy : 55/61 (90.16%)
```

The six divergences are all in `reference_mismatch_fuzzy` and are all
fail-closed. See section 44.

**Before Upgrade B this figure was 61/61 (100%).** That number was real
but it was measured on a dataset where the fuzzy tier never ran. Making
the tier reachable exposed a policy question that had never been tested,
and the accuracy fell as a result. A number that drops when you finally
exercise a code path is more informative than one that never had to.

**On the denominator.** The two corrupted records are counted as
`rejected_at_ingestion`, not dropped. Dropping them would inflate the
percentage against a smaller denominator, and a test asserts the
accounting adds up.

**On independence.** Ground truth and the engine both derive from the
same specification — the decision table. Two labels disagreed with it and
were corrected (sections 14–15); both corrections are printed with the
result and stored in the artifact rather than left to be discovered.

What this measures is that the implementation matches its own
specification across ten adversarial categories. That is a real result
and it is narrower than "the engine handles reconciliation."

**Deliberately absent:** no test asserts a minimum accuracy. A test that
failed when accuracy dropped would create pressure to adjust ground truth
until it passed — which is exactly the failure recorded twice already in
this log.

---

# Corrections to earlier entries

## 36. The fuzzy precision figure is withdrawn

Earlier versions of this log reported:

```
Threshold 60 → Precision 0.12, Recall 1.00
Threshold 70 → Precision 0.13, Recall 1.00
Threshold 90 → Precision 0.13, Recall 1.00
Threshold 95 → Precision 0.00, Recall 0.00
```

and attributed the poor precision to narrow amount diversity in the
synthetic dataset.

**Both the number and the explanation are withdrawn.**

---

## 37. The stated cause was tested and disproven

After fix A1 (section 16), accidental net collisions went to **zero**.
Precision did not move. It stayed at 0.12.

If narrow amount diversity had been the cause, removing it would have
changed the number. It did not.

---

## 38. The real cause — the metric counted correct matches as errors

The sweep computed, per record:

```python
fuzzy_fired = best_fuzzy_score >= threshold
FP          = (category != "reference_mismatch_fuzzy") and fuzzy_fired
```

That asks *"did fuzzy fire?"* — not *"did fuzzy pick the wrong record?"*

The generator wrote narration as `"NEFT CR UTR123456789 MERCH_001"`. The
narration **contained the UTR verbatim**, so for every clean transaction
`partial_ratio(pg.utr, narration)` returned 100. It cleared every
threshold, and because its category was not `reference_mismatch_fuzzy`,
it was counted as a false positive.

**Those 43 "false positives" were correct matches being scored as
errors.**

The tell was in the output the whole time: FP stayed at exactly 43 across
thresholds 60 through 95. A similarity-based false-positive count has to
fall as the threshold rises. A flat count means the metric was
independent of the variable it claimed to sweep. I looked at that table
several times without noticing.

---

## 39. The fuzzy tier was unreachable, and that was already documented

Rewriting the benchmark to report tier reachability first gave:

```
reference_mismatch_fuzzy    exact_utr=0  exact_txn=6  fuzzy=0
Records that genuinely reach the fuzzy tier: 0
```

**Zero of 61 records reached the fuzzy tier.**
`build_reference_mismatch()` corrupted only `bank["utr"]` and left
`bank_ref` as `BANKREF_<txn_id>`, so tier 2 resolved every one of those
records before fuzzy was consulted.

**This was not a discovery.** `tests/test_matching.py` already said:

> "In our data, this typically resolves via exact_txn since bank_ref
> still encodes the correct txn_id even when UTR is corrupted. The fuzzy
> tier exists for the realistic case where no such structured convention
> is available."

The design fact was written down in a test docstring and never reached
the numbers published elsewhere in this document. I kept reporting a
precision figure for a code path production does not take.

Fixed in Upgrade B — see section 41.

---

## 40. The pattern across all of this

Several things that looked like engine weaknesses were measurement
defects:

| Reported as | Actually was |
|---|---|
| Ground-truth divergence (L1, L2) | Labels asserting statuses the decision table cannot produce |
| Six fail-open auto-matches | A per-case harness structurally unable to observe batch-relational properties |
| Fuzzy precision 0.13 | A benchmark counting correct matches as false positives, on a tier that never ran |

Every time, the deterministic engine was right and the instrument was
wrong.

The lesson is narrower than "test your tests". It is:

> A number that looks like a weakness in your code should be checked
> against the data and the code path that produced it before you write it
> down as one.

I documented 0.13 twice in this file and defended it as an honest
limitation. Being honest about a number is worth nothing if the number
does not measure what it says it measures.

---

# Upgrade B — realistic narration, and a reachable fuzzy tier

## 41. The tier is now reachable

`build_reference_mismatch()` now emits a bank row with **no structured
reference at all**:

| Field | Was | Now |
|---|---|---|
| `bank_ref` | `BANKREF_TXN_00025` | `HDFC0004521N9921` (bank-native) |
| `utr` | corrupted digit | `None` — the feed exposes no UTR field |
| `narration` | one invented format | realistic format, UTR in free text |

Tier 1 misses (no UTR), tier 2 misses (bank-native ref, no `TXN_` token
in the narration), so tier 3 fires with amount and date agreement
enforced.

```
reference_mismatch_fuzzy    exact_utr=0  exact_txn=0  fuzzy=6
Records that genuinely reach the fuzzy tier: 6
```

Five narration formats replaced the single invented one, drawn from what
Indian banks actually emit. One — the UPI form — carries a UPI reference
and **no UTR at all**, deliberately unrecoverable by narration matching.
Without at least one such case, "our fuzzy tier recovers narration" would
be an untested claim about a dataset engineered to be easy.

---

## 42. `BANKREF_<txn_id>` was load-bearing in four files

A convention introduced for generator convenience had quietly become an
assumption everywhere downstream. Removing it broke, in order:

1. `verify_data.py` — `index_bank_by_pg_txn` only indexed rows starting
   with `BANKREF_`. CHECK 4 reported six rows as missing from the bank
   feed; CHECK 6 asserted a UTR discrepancy that no longer exists in that
   shape.
2. `build_e2e_benchmark.py` — `get_txn_id()` raised for any bank row it
   could not resolve. **Hard crash**, no benchmark built.
3. `tune_fuzzy_threshold.py` — see section 43.

Each needed a second linkage path, and each of those paths is documented
as a *verification affordance*: the verifier may use fuzzy matching to
answer "does a bank row for this transaction exist in the file?" The
pipeline still has to answer the harder question — "which row, if any,
may I safely link, given amount and date guards across the whole
candidate pool?" — and nothing in the harness does that for it.

Worth stating plainly: a shortcut taken once in a generator propagated
silently into every downstream evaluator, and nobody noticed until the
shortcut was removed.

---

## 43. The sweep's truth linkage broke for the same reason — twice

The first version of `tune_fuzzy_threshold.py` asked *"did fuzzy fire?"*
(section 38). The corrected version asked:

```python
best_record.txn_id == pg.txn_id
```

which is broken for exactly the records the sweep now evaluates. Upgrade
B strips both the `bank_ref` convention and any `TXN_` token from the
narration — that removal is what makes the tier reachable at all. So
`bank_record.txn_id` is `None` **by construction**, `None == "TXN_00025"`
is `False`, and every correct selection was counted as a false positive:

```
TP = 0, FP = 6, Recall = nan
```

The metric was identifying ground truth by the exact field the category
exists to remove. Same defect class as section 38, in the corrected
version of the same file.

**Fix.** A named `is_correct()` predicate that prefers `txn_id` when one
exists and falls back to exact net equality when it does not — valid only
because accidental net collisions are measured at zero, with that
dependency written into the docstring rather than left implicit.

Corrected result:

```
Threshold   TP   FP   FN   Precision  Recall
85          6    0    0    1.00       1.00
95          3    0    3    1.00       0.50
```

**Caveat, printed with the number.** With zero accidental net collisions,
any candidate surviving the amount and date guards is already the correct
one. The fuzzy score is doing no discriminating work — it is a formality
on top of a guard that has already decided. Precision 1.00 means "the
guards are selective on this dataset", not "narration matching works".
Making narration load-bearing would need amount collisions *inside* the
guard window, which is a deliberate dataset change rather than a fix.

---

## 44. Fail-closed behaviour became visible only once the tier ran

With the tier reachable, six records diverge from ground truth:

```
expected  MATCHED / NONE
actual    HUMAN_REVIEW / REFERENCE_MISMATCH
```

The engine recovers all six by narration similarity, then declines to
auto-match them. Scoring withholds `SCORE_UTR_EXACT` and
`SCORE_TXN_ID_BANK` — neither is available — so confidence lands LOW and
`low_confidence_requires_human_review` fires at priority 6.

**We kept the engine.** A settlement whose only link to a transaction is
a fuzzy string match, with no structured identifier agreeing anywhere, is
not something a finance system should auto-approve. Routing it to a human
is the fail-closed outcome this architecture claims to produce, and here
it produced it without being asked.

The ground-truth label was written while the fuzzy tier was unreachable,
so it encoded an assumption that had never been tested. It is
deliberately left as `MATCHED`.

**Why not correct the label.** It would have restored 100%. It would also
have been a third ground-truth edit, and a reviewer counting three starts
asking whether the answer key follows the engine. A number that needed
the target moved is worth less than one that did not.

**How it is recorded.** `verify_e2e_gold_baseline.py` gained a third
outcome, `KNOWN_POLICY_DIVERGENCE`, distinct from
`NOT_EVALUABLE_PER_CASE`:

- `NOT_EVALUABLE_PER_CASE` — the harness **cannot see** the property
- `KNOWN_POLICY_DIVERGENCE` — the harness sees it clearly, and we decided
  the engine is right

Collapsing the two would use an honesty mechanism to hide a result. Two
tests prevent that: every policy exclusion must carry a rationale over 60
characters in the artifact, and the two buckets must stay disjoint. The
arithmetic stays checkable:

```
raw (12) == divergent (0) + not_evaluable (6) + known_policy (6)
```

---

# 45. Current state

```
326 / 326 tests passing

Gold baseline:            stable
Baseline divergences:     0
Not evaluable:            6  (batch-relational)
Known policy divergences: 6  (fail-closed, documented)
Raw mismatches:           12
Measured accuracy:        55/61 (90.16%)
Decision policy coverage: 2048/2048
Fuzzy tier:               6 of 61 records reach it
Accidental collisions:    0
Settlement arithmetic:    1 definition (was 4)
Throughput:               1,348.5 rec/s @ 60; O(n^2) -- 179.2 @ 5000
MDR:                      method-aware (UPI zero-rated); 17 zero-fee records
```

**Deterministic core (0–4).** Decimal firewall, per-record fault
isolation, three-tier matching with deterministic tie-breaking,
independent GST/TDS validation, per-record seller ledger, priority
decision table with 2048/2048 combination coverage, full reason-code
preservation.

**AI boundary (5A).** Preemptive timeout, typed contracts with no
financial vocabulary, deterministic fallback, invariance tests proving
the decision is unchanged whether the model succeeds or fails.

**Real model (5B).** Gemini 3.1 Flash-Lite behind that boundary.

**Evaluation (5C).** Held-out narration and explanation sets, semantic
faithfulness scoring, E2E gold baseline harness, throughput benchmark,
tier reachability measurement, full-batch accuracy report.

**Agent (6).** Four read-only tools, a registry with strict argument
validation, a two-call `ask()` loop, and a demo that re-verifies the data
invariant after every answer.

---

# 46. What is not done

Listing these explicitly so nothing above is read as a completed claim.

- **LLM-assisted candidate matching** — built, not connected (section
  24). The model selects tools, phrases results, and explains decisions.
  It does not participate in matching, so its contribution to any
  financial outcome is zero by design.
- **Agent tool-selection accuracy is six questions.** A smoke test, not
  an evaluation. A real measurement needs a held-out labelled question
  set.
- **The fuzzy tier is reachable but not stress-tested.** Six records
  reach it and it recovers all six, but the amount guard is doing the
  discriminating work (section 43).
- **Held-out sets** — 8 cases each; too small to generalise from.
- **Real bank narration** — five formats now, but still invented. Only
  the `reference_mismatch_fuzzy` category has a bank-native reference;
  every other category still uses `BANKREF_<txn_id>`, a convention no
  real bank provides.
- **Settlement model** — one PG transaction to one bank credit. Real
  settlements are batched: many transactions net into one transfer, minus
  refunds and chargebacks. The hard part of real reconciliation is
  decomposing that, and this system never has to.
- **Scale** — throughput is a benchmark on one machine, not a capacity
  claim.

---

# 47. Milestones

```
phase-3-final
    ↓
phase-4-final
    ↓
phase-5-boundary
    ↓
phase-5-final     (real Gemini integration + evaluation)
    ↓
phase-6-final     (agent tool layer, ask() loop, real-model demo)
```

These are architectural checkpoints, not version numbers.

---

# 48. Closing

The most useful thing I built was not the reconciliation engine. It was
the set of harnesses that kept disagreeing with it — and the discipline
of checking, each time, which one was actually wrong.

Usually it was the harness. That is not a comfortable result to write
down, but it is the honest one, and it is the reason I trust the engine
more than I would have if everything had passed the first time.

The last one was different. Upgrade B made a dead code path run for the
first time, and the engine turned out to be more conservative than the
ground truth expected. Accuracy fell from 100% to 90.16% as a direct
result. That is the trade this project has been making all along: a
number you can explain is worth more than a number that never had to be
tested.

# 49. A verification script asserted a property the data did not have

Small, but it belongs here because of where it happened.

`verify_data.py` CHECK 8 printed:

    Narrations carrying no UTR at all: 1
    (UPI form -- unrecoverable by narration matching, on purpose)

Both halves were wrong.

The row it found was:

    UPI/3TR694524394/MERCH_004/NET STLMNT

That is not the UPI form. It is the ordinary
`{method}/{utr}/{merchant}/NET STLMNT` shape, and the single-character
corruption in `build_reference_mismatch()` happened to land on index 0
— turning `UTR694524394` into `3TR694524394`. Eleven of twelve
characters still match. `partial_ratio` scores it 92, comfortably above
the production threshold of 85. It is entirely recoverable.

`rng.randint(0, len(original) - 1)` includes indices 0, 1 and 2, which
are the letters `U`, `T`, `R`. Nothing about that is a defect — OCR and
manual entry both corrupt prefixes — but the check was reporting it as
something else.

**The label described a code path that does not execute.** The UPI
branch of `_make_narration()` only fires when `utr is None`, and every
caller passes a real UTR. I wrote a comment asserting a property of
data that is never generated, in the file whose entire job is verifying
properties the data actually has.

**Fix.** CHECK 8 no longer counts or labels missing-UTR narrations. A
new CHECK 9 measures the property that matters instead: every
`reference_mismatch_fuzzy` row must score above `FUZZY_MIN_SIMILARITY`
against its original UTR, and it fails if any does not. Prefix
corruptions are reported separately with their scores, so the case is
visible rather than mislabelled:

    CHECK 9: Corrupted UTRs remain recoverable
      Checked 6 reference-mismatch rows against the production
      threshold (85).
      1 row(s) had the corruption land on the 'UTR' prefix rather than
      the digits:
        TXN_00027  similarity 92  UPI/3TR694524394/MERCH_004/NET STLMNT
        (still recoverable -- 11 of 12 characters match)
      Every corrupted UTR remains above the threshold.

The UPI branch is retained but marked unreachable. It is the shape a
bank row takes when there is no recoverable reference at all, and it is
the case that would be needed if an LLM extraction path were ever
justified — which brings us to the next section.

---

# 50. We looked for a job for the LLM in matching and could not honestly find one

`find_bank_candidates_with_llm_assist()` has existed in
`candidates.py` since Phase 5, deliberately off the live path. Section
24 lists the preconditions for connecting it. Upgrade B was supposed to
supply the missing one: a bank row the deterministic tiers genuinely
cannot resolve.

It did not. Three designs were considered and all three failed for
different reasons, and the pattern is the finding.

## Design 1 — extract a TXN_ token from the narration

This is what the extractor already does. It is also now impossible,
because **Upgrade B removed every `TXN_` token from bank narration on
purpose.** Real banks do not echo your internal transaction ID; that
convention was exactly what made the fuzzy tier dead code (section 39).

The extractor is looking for something the data no longer contains, and
correctly so.

Even if it found one, `index.bank_by_txn` does not hold the
`reference_mismatch_fuzzy` rows — they have no resolvable txn_id by
construction. That absence is what makes them reach tier 3 at all.

## Design 2 — recover rows that carry no UTR

Make the UPI branch reachable, and some bank rows would look like:

    UPI/P2M/412345678901/RAZORPAY/MERCH_004

A UPI reference that appears nowhere else in the dataset, and a
merchant ID. **There is no recoverable signal in that string.** An LLM
cannot extract information that is not present; it can only invent
something, which the deterministic index lookup would then reject.

A guard correctly rejecting a hallucination is a good property, but it
is not recall. Building the case in order to demonstrate the guard
would be building a problem to demonstrate a solution.

## Design 3 — let the model choose from a candidate shortlist

Accidental net collisions in this dataset are **zero** (section 16).
Any candidate surviving the amount and date guards is already the
correct one. The shortlist has length one. There is nothing to choose
between.

## And the one place ambiguity does exist is where the model must not go

The `ambiguous` category contains genuine competing candidates — that
is its whole purpose. It is also precisely where an LLM must not
intervene. Breaking an ambiguity tie with no independent verifying
signal **is** the model deciding financial truth, which is the exact
failure this architecture exists to prevent. Routing those to
`HUMAN_REVIEW` is the correct outcome, and a model that resolved them
would be doing harm confidently.

## What would be required, and why we did not do it

Making narration load-bearing needs amount collisions *inside* the
guard window, so that the amount guard stops uniquely identifying the
right row and narration has to break the tie. That is a deliberate
change to the dataset in order to create work for a tool we already
have.

Everywhere else in this project, the dataset was changed to make a
measurement honest. This would be changing it to make a component look
necessary. Those are not the same thing and the difference is the point.

## The deferred function also has four real bugs

Worth recording, because it has never run and its state is not
obvious from reading it:

1. `index.bank_by_txn.get(result.value, [])` — `result.value` is a
   `NarrationExtraction` object, not a string. It passes the whole
   dataclass as a dict key and returns `[]` forever.
2. `pg_record.raw_ref.get("narration")` — narration lives on the BANK
   record. PG records do not have one, so this is always empty and the
   function returns early every time.
3. It returns candidates without applying the amount and date guards,
   which section 24 lists as a precondition for connecting it.
4. It labels the result `"fuzzy"`, which would misreport the tier in
   every downstream measurement.

All four are fixable. None of them is the reason it stays disconnected.

## Current position

The extractor exists, is guarded, is tested in isolation, and is not
connected.

**The model's contribution to financial outcomes is zero by design and
by measurement, not by omission.** It selects which question to answer,
phrases results from real numbers, and explains decisions it cannot
alter. On this dataset the deterministic tier recovers everything an
LLM extraction path would have, at precision 1.00.

Connecting it would mean adding a model to a path that does not need
one. This log has recorded several cases of a number measuring
something other than what it claimed. Shipping a component that adds
nothing measurable, and describing it as AI-assisted matching, would be
the same mistake in a different shape.

## 51. The README's decision table was not updated for Upgrade B

The accuracy section and the fuzzy-tier row were corrected. The
decision-status table three paragraphs above them was not, so the
README reported 30 `MATCHED` and a 49.18% match rate while the
pipeline produced 24 and 39.34%.

The arithmetic is exactly what Upgrade B predicts: six
`reference_mismatch_fuzzy` records moved `MATCHED` -> `HUMAN_REVIEW`
once the fuzzy tier became reachable. 30 − 6 = 24, 13 + 6 = 19.
`PARTIAL_MATCH` and `UNMATCHED` also shifted by one, from RNG drift
in the narration change.

I even edited the `HUMAN_REVIEW` row's description to mention
"fuzzy-only linkage" without changing the count sitting next to it.

**How it was found.** Rehearsing the demo out loud and noticing the
number I was saying did not match the number I had written.

Third documentation-drift incident in this project. Section 29 is
about the previous two, and includes the line "documentation had to
catch up with the engineering reality." Apparently that is not a
lesson you learn once.

---

# 52. The most important formula in the system existed four times

`expected_net = gross - fee - GST - TDS` is the expression every
layer that touches a bank amount depends on. It existed as four
independent inline copies:

```
matching/candidates.py   _pg_expected_net()   -- the only named one
matching/engine.py       inline, candidate ranking
matching/scoring.py      inline, SCORE_AMOUNT_BANK
exceptions/manager.py    inline, AMOUNT_MISMATCH
```

`fee + GST` -- the expected invoice amount -- existed twice more,
in `engine.py` and `scoring.py`.

**How it was found.** By reading the files, during a full-repository
audit. Not by a test, and not by a harness.

**Nothing was ever wrong.** All four copies agreed, and every number
this project has published is unaffected. That is exactly why it
survived: a divergence that has not happened yet is invisible to
every assertion you can write about current behaviour. The 289 tests
passing at the time could not have caught it, because there was
nothing yet to catch.

**Why it is a defect anyway.** The copies were not independent
implementations kept in sync deliberately. They were the same
expression typed four times, and nothing connected them. The moment
a settlement term is added -- a refund, a chargeback, an adjustment,
the negative line items that make real settlement reconciliation
hard -- a partial edit leaves candidate ranking, confidence scoring
and the AMOUNT_MISMATCH control each reconciling against a different
definition of the same settlement. Nothing raises. The batch is
simply wrong, quietly, in the layer with the least test visibility.

That is not hypothetical. N:1 batched settlement is the single
largest gap named in section 46, and adding it *starts* by changing
this expression.

**Fix.** `src/financial.py` -- `settlement_expected_net()` and
`expected_invoice_amount()`. All six call sites now import.

The refactor was verified behaviour-identical before anything else
changed: a full-batch decision snapshot (status, exception code,
confidence, tax state, reason codes for all 61 records) hashed to
`cefdc56f22c13dff` before and after.

**Regression protection.** `tests/test_financial_invariants.py`,
nine tests in two layers:

- *Behavioural* -- over the real batch, the scoring signal, the
  amount control, the invoice signal and the generator's own
  independently-written `net_payout` field must all agree with
  `src/financial.py` to the paise.
- *Structural* -- a regex sweep of `src/` fails if any module
  outside `financial.py` re-derives either expression inline, plus a
  positive check that the four consumers still import it.

The structural half is the one that matters. The behavioural half
only fires once someone has already written a divergence *and* the
batch happens to exercise it. The structural half rejects the copy
at the moment it appears. It was tested by reintroducing a fifth
copy into `scoring.py` and confirming the failure names the exact
file and line.

**What I took from this.** Every defect in this log so far was found
by an instrument disagreeing with the engine. This one could not be,
in principle. Some defects are only visible by reading, and "all
tests pass" is silent about them by construction.

---

# 53. A HUMAN_REVIEW decision with nothing in its reason codes

`_all_violated_codes()` had a branch for every `DecisionContext`
flag except `low_confidence`.

The decision table's low-confidence rule reports
`REFERENCE_MISMATCH` as its primary exception code. But
`low_confidence` is mutually exclusive with every identity and
source-presence flag -- the guard in `_build_context()` makes it so
-- which means those records have no *other* violation to populate
the list. The one code that should have been there was the only one
missing.

Result, on the real batch:

```
TXN_00025  HUMAN_REVIEW  REFERENCE_MISMATCH  reason_codes=[NONE]
```

Six records, all `reference_mismatch_fuzzy`, routed to a human with
no machine-readable statement of what was wrong with them.

**How it was found.** A full-file audit, then confirmed directly
against `data/eval/accuracy_report.json`, where all six divergences
had been printing `"reason_codes": ["NONE"]` in a published artifact
the whole time. I had read that file more than once.

**Why it matters.** No decision was wrong. Status, exception code
and the complete `evidence.context` were all correct, and the record
went to review as it should have. But this README says every
unresolved record carries "the complete set of violated conditions",
and section 6 of this log records splitting `status` from
`reason_codes` specifically so that a single status would not be the
only audit evidence. For the second-largest exception bucket in the
batch, `reason_codes` explained nothing.

**Fix.** The missing branch. One `if`.

**The fix was broader than expected.** It changed 16 records, not 6.
The `amount_fee_discrepancy` and `unresolvable` categories are
genuinely *both* amount-mismatched and low-confidence: the bank
credits less than expected, so `SCORE_AMOUNT_BANK` is withheld, so
the normalized score falls below the auto-match floor. The table
picks `AMOUNT_MISMATCH` at priority 3, and `reason_codes` now
preserves both facts:

```
TXN_00031  HUMAN_REVIEW  AMOUNT_MISMATCH
           reason_codes=[AMOUNT_MISMATCH, REFERENCE_MISMATCH]
```

That is the C3 fix (section 12) becoming visible in the output for
the first time. C3 established that identity and financial
correctness are different questions; these ten records are the ones
where both answers are bad, and now they say so.

**Regression protection.** Two tests. The specific one asserts a
pure low-confidence context yields `REFERENCE_MISMATCH`. The general
one is the better guard:

> whatever rule fires, its `exception_code` must also appear in
> `reason_codes`

swept across the complete 2^11 = 2048 context space, with
`fully_clean` and the catch-all exempt by construction. Reverting
the one-line fix makes it fail on 16 of 2048 combinations and names
the responsible rule.

The specific test only proves this bug is fixed. The general test
proves the *class* is closed -- had it existed, it would have caught
this the day `low_confidence` was added to the context.

**Related.** While writing the 2048-combination sweep it became
clear that the coverage figure published everywhere -- "512/512
combinations" -- described a 2^9 sweep that pinned
`duplicate_detected` and `amount_mismatch` at False throughout. Both
are real policy dimensions with their own rules. The figure
understated the space rather than covering it. The full space is now
swept and the published figure is 2048/2048;
`test_context_dimensions_match_the_swept_space()` fails if a twelfth
field is ever added without updating both.

---

# 54. The throughput figure described an engine that no longer existed

This is the fourth documentation-drift incident (§29, §49, §51), and the
worst of them, because the number was a headline claim rather than a
sentence in prose.

**What happened.** `README.md` reported:

    Throughput   1,113.9 records/sec at batch 60; swept across 60/300/1000/5000

with a per-stage breakdown showing `match_time` growing **linearly**:

    n=60    match 0.0016s
    n=300   match 0.0068s
    n=1000  match 0.0327s
    n=5000  match 0.1439s

Re-running the benchmark during Upgrade 2.2 produced something else
entirely:

    n=60    match 0.0041s     1,348.5 rec/s
    n=300   match 0.0786s     2,254.4 rec/s
    n=1000  match 0.8360s     1,052.1 rec/s
    n=5000  match 27.3259s      179.2 rec/s

Five times the records costs twenty to thirty times the matching time.
The engine is **O(n²)**, and had been reported as linear.

**How it was found.** Not by looking for it. Upgrade 2.2 made the payment
gateway's fee method-dependent, which meant regenerating the dataset and
rebuilding every evaluation artifact — including the throughput
benchmark, which had not been rebuilt in a long time.

**First conclusion, and it was wrong.** The obvious reading was that I had
just introduced a performance regression: UPI is zero-rated, so a third of
records now have `fee = 0` and therefore `expected_net == gross`, which
looked like it could cluster amounts and make the candidate scan do more
work.

That hypothesis was testable, so I tested it — the same discipline that
disproved the fuzzy-precision explanation in §37. Running the identical
batch through both fee models:

    OLD flat 2%          run_matching(1500) = 1.68s   1502 pairs pass the gate
    NEW method-aware     run_matching(1500) = 1.72s   1500 pairs pass the gate

**No difference.** The MDR change was not the cause, and the O(n²)
behaviour was already there.

**The actual cause.** `git log` on the two files:

    data/throughput_benchmark.json   last written at bea9957  (phase-4-final)
    find_bank_ambiguity_candidates   added at      520a489  (Phase 5B)

The ambiguity scan is **newer than the recorded benchmark**. It walks the
entire bank pool for every PG record, asking *"does a competing record
exist?"* — an O(n²) sweep, and the price of detecting ambiguity at all.

The benchmark was never wrong when it was recorded. It was recorded
against a matching engine that did not yet contain the scan, and then
never re-recorded. Every phase since has quoted it.

**The part that stings.** `benchmark_throughput.py` prints this at the end
of every run, and has since Phase 4:

> *"Note: match_time growth relative to n_records indicates the matching
> engine's actual complexity behavior on this hardware — if match_time
> grows faster than linearly, that's real evidence of an O(n^2)
> bottleneck worth investigating, not a claim to hide."*

The instrument was correct, the warning was already written, and the
stale artifact meant nobody ever saw the growth it was warning about. I
wrote a check for exactly this condition and then stopped running it.

**Fix.** Benchmark re-recorded. `README.md` now reports 1,348.5 rec/sec at
batch 60 **and** the quadratic growth, with the per-stage table, rather
than a single flattering figure. The cost is stated as a real ceiling for
production scale rather than left for a reviewer to discover.

**Not fixed: the O(n²) itself.** Making ambiguity detection sub-quadratic
means bucketing candidates by (rounded amount, date window) instead of
scanning the pool — a real change to `candidates.py`, which is core
matching logic, three days before a deadline. At the 61-record batch this
system targets, matching costs 4 milliseconds. Recorded as a scaling limit
in `ARCHITECTURE.md`, not papered over.

**What I took from this.** A generated artifact is a measurement with a
timestamp, and it silently stops being true the moment the code it
measured changes. Source code has tests to keep it honest; a JSON file
committed months ago has nothing.

The four drift incidents now share one shape: **something written down
once, and then not re-derived when the thing it described moved.** The
countermeasure that would have caught all four is the same — regenerate
every artifact and re-grep every published number before claiming
anything, rather than trusting the last recorded value.

---

# 55. Three traps in making the fee method-dependent

Upgrade 2.2 replaced a flat 2% MDR with `config.MDR_BY_METHOD`. The change
itself is four lines. Three separate things would have broken silently,
and all three were found by reading the code that consumed the fee rather
than by running the tests — which passed throughout.

**Trap 1 — the audit trail could not explain the fee.**

`payment_method` was written into the raw JSON by the generator and was
**not** a field on `PGSettlementRecord`. A field absent from the Pydantic
model never reaches `NormalizedRecord.raw_ref`, so it was silently dropped
at ingestion.

That was harmless while the fee was a flat percentage. The moment the fee
depends on the method, an audit trail recording a fee with no way to
explain it is a real gap. Added as an optional field.

**Trap 2 — a zero fee makes the injected GST error a no-op.**

`build_tax_mismatch()` injects a wrong GST as a percentage of the fee:

    wrong_gst = money(fee * 0.12)     instead of    money(fee * 0.18)

Under a flat 2% MDR that is always a real discrepancy. With UPI at zero
MDR it is not:

    money(0 * 0.12) == money(0 * 0.18) == 0.00

The record would carry a `TAX_MISMATCH` ground-truth label over an invoice
that is arithmetically correct — a false divergence that would appear in
the accuracy report as an **engine** failure. Exactly the shape of L1 and
L2 (§14, §15).

Fixed by drawing `tax_mismatch` from `FEE_BEARING_METHODS`, and by adding
`_verify_tax_mismatch_is_detectable()` — a generation-time check that
every record labelled `tax_mismatch` actually contains a discrepancy the
engine can find.

**Trap 3 — the ambiguous sibling would have stopped colliding.**

`build_ambiguous_sibling()` hardcoded `fee = money(gross * 0.02)`.

Ambiguity is detected by comparing expected nets, and
`net = gross − fee − gst − tds`. If the counterpart paid by UPI (fee 0)
and the sibling were charged 2%, their nets would differ by ~2% of gross
— orders of magnitude above `AMOUNT_TOLERANCE` — and **the collision that
creates the ambiguity would silently fail to exist.**

That is precisely the original fail-open bug from §4, where the sibling's
bank row was never synced and six records were auto-matched that should
have gone to a human. Six fail-opens, while 162 tests passed.

Fixed by mirroring the counterpart's payment method, so the pair is
economically identical by construction.
`_verify_ambiguous_pairs_collide()` — the guard added after §4 — still
checks it independently, and reported 3 pairs colliding after the change.

**What the three have in common.** None would have failed a test. The
suite stayed at 326 passing through the entire upgrade. Each one is a
consumer of the fee that assumed the fee was a constant, and the
assumption was invisible until the constant became a variable.

The generator now runs **six** generation-time checks rather than four.
Two were added here, and both exist because the same class of defect —
ground truth asserting a condition the data does not contain — has now
occurred four times in this project (§4, §14, §15, and Trap 2 above).

**One thing worth stating plainly.** Making UPI zero-rated added a genuine
new edge case the system did not previously have: a legitimate ₹0 fee
implying a legitimate ₹0 GST, which must **not** be flagged as a tax
error. 17 of 61 records now carry it, none are wrongly flagged, and 8 of
them reach `MATCHED` cleanly. That case did not exist under a flat MDR.
