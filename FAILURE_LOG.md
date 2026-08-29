# Failure Log

## AI Finance Controller — Phases 0 through 5C

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

Entries are grouped by phase. Section 30 onward covers corrections to
things recorded earlier in this same log that later turned out to be
wrong.

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
normal runs because `PYT_` appears nowhere in the generated data — zero
occurrences in `bank_statement.json`, nothing in `generate_data.py`. The
pattern has never fired in production.

**Fix.** `r"(PYT_\d{7,8})"`.

**Worth noting.** No result was ever affected by this, because the code
path is dead against our own dataset. It was found only because a
held-out test exercised something the main data does not. That is the
clearest argument I have for keeping held-out tests around.

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

Note: see section 31. The precision number reported alongside this was
later found to be meaningless, and the tier turned out to be unreachable
in practice. The gross/net fix itself was real and correct.

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

**This section supersedes what sections 31–33 of the earlier version of
this log said.** Those sections stated that real-model integration was
deliberately deferred and not yet done. That was true when written and is
no longer true. The log fell a full milestone behind the code, which is
worth recording as its own failure — see section 29.

## 22. What is now built

- `src/agent/providers/base.py` — `LLMProvider` ABC, `ProviderResponse`
  with no financial fields
- `src/agent/providers/gemini_provider.py` — real `google-genai` calls to
  Gemini 3.1 Flash-Lite, real token accounting from `usage_metadata`
- `src/agent/config.py` — credentials from environment, hard failure if
  `AGENT_FREE_ONLY` is disabled

Real recorded runs are in `data/eval/real_gemini_explanation_run_5C4.json`
with genuine wall-clock latencies.

**Measured.** On 8 held-out explanation cases: 75% full semantic
faithfulness, 87.5% reason-code preservation, 0 unsupported claims, 0
safety-critical failures.

**Honest limitation.** Eight cases is a small sample. 75% of 8 is 6. These
are real measurements but they are closer to anecdotes than statistics.
Expanding the held-out set is outstanding work.

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
**not on the live path**. `controller.py` only implements `.explain()` on
an already-computed decision.

So the honest summary of what the model does today: it rephrases a
decision the deterministic engine already made. It does not affect any
financial outcome. That is architecturally correct, and it is also a
narrow use of the model.

Before connecting it, these need to hold:

- deterministic tiers keep priority; LLM only runs after they fail
- proposed ID must exist in the deterministic index
- amount and date guards still enforced
- LLM failure produces an identical decision to LLM-off
- false-positive rate measured on held-out narrations

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

**Fix.** Those categories are now reported as `NOT_EVALUABLE_PER_CASE`
rather than counted as engine divergence, with `raw_divergent_cases` kept
in the report so the exclusion is arithmetically transparent. Two tests
guard it: one asserts `raw == divergent + not_evaluable`, the other
asserts only declared categories can be excluded.

Currently `raw = 6` and `not_evaluable = 6`, so the exclusion is carrying
no weight at all — nothing is hidden behind it.

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
`phase-3-final`, `phase-4-final`, `phase-5-boundary`, `phase-5-final`.

**Fix.** This rewrite. Tags now referenced consistently.

Section 18 of the earlier version of this log recorded, as a lesson,
*"documentation had to catch up with the engineering reality."* Then the
same thing happened again to the document that recorded it.

---

# FAILURE_LOG.md — Phase 6 section

Insert these sections **after section 29** ("This log fell a milestone
behind the code") and **before section 30** ("Corrections to earlier
entries in this log").

Then renumber the existing sections 30–38 to 36–44, or leave them and
accept a numbering gap — the content matters more than the sequence.

---

```markdown
# Phase 6 — the agent

## 30. What was actually missing

Everything up to Phase 5C was a reconciliation engine with a model
attached to one narrow task: rephrasing a decision that had already
been made. There was no orchestration, no tool selection, and no way
for a person to ask a question. A human ran six shell scripts and read
JSON.

Track 04 asks for "an agent that closes one finance-ops loop... reporting
its match rate and the exceptions it could not resolve." What existed
was a model call inside a guardrail, which is not the same thing.

Recording this as a failure rather than a roadmap item because it went
unstated for a long time while the surrounding work got more and more
polished. The gap was not technical difficulty. It was that the
interesting problems were all in the deterministic core, so that is
where the effort went.

## 31. What Phase 6 built

Five steps:

    query_tools.py    four read-only tools over decide_batch() output
    registry.py       tool specs, strict argument validation, envelopes
    tool_selection.py frozen ToolSelection contract and strict parser
    controller.ask()  select -> dispatch -> phrase
    demo_agent.py     the real demo path

Two model calls per question, neither able to produce a number:

    1. SELECTION -- the model reads the question and the tool
       catalogue. It has no access to the data. It cannot answer; it
       can only choose which question to ask the deterministic layer.

    2. PHRASING -- the model receives the real tool output and writes
       it in English, instructed to use only the numbers given.

Between them sits dispatch(), which runs the real tool. Every number in
an answer comes from decide_batch() via BatchQueryContext.

The raw tool result is returned alongside the prose in
`AgentAnswer.data`, so the phrasing can always be checked against the
numbers it claims to describe.

Verified against a real model: six questions through Gemini 3.1
Flash-Lite, data invariant held 6/6, tool selection matched expectation
6/6. Also re-verified live inside the demo after every answer.

## 32. The tool answered a question nobody asked

The best finding of Phase 6, and it came from the real model rather
than from a test.

Asked "How fast did the pipeline process this batch?", the agent
selected `get_throughput_report` -- correct -- and then answered:

> The provided data does not contain a live timing for the current
> batch of 61 records. It only provides a benchmark for a batch size of
> 60 records...

The model was right. `get_throughput_report()` returned a sweep across
60/300/1000/5000 records with a peak figure. The operator asked about
THIS batch of 61. Nothing in the payload addressed that.

The tool did not answer the question its own description claimed it
answered, and 287 tests did not notice, because every one of them
checked that the tool returned the data it was written to return. None
checked whether that data answered anything.

**Fix: the tool, not the prompt.** `get_throughput_report()` now leads
with the recorded run closest in size to the loaded batch, then the
sweep as scaling context. Prompting the model to be less honest about
a gap in its evidence would have been the wrong direction entirely.

The general shape is worth keeping: a model with no stake in the answer
looked at a payload and said it did not contain what was asked for. A
test suite cannot do that, because a test asserts what the author
already believed.

## 33. The demo script demonstrated nothing for a long time

`scripts/demo_agent.py` -- the most obviously-named file in the
repository, the one anyone opens first -- called a hardcoded
`mock_llm()` returning a canned string, under a docstring describing a
"GPT call when wiring in credentials."

That was accurate when written. It stayed in place through the entire
Gemini integration and the whole evaluation framework. For that period
the repository contained a working real-model provider, recorded live
runs, and a demo that used neither.

Nothing about it was hidden. It was simply never revisited, because it
was not blocking anything.

Now rewritten: real provider, real batch, five questions, and the data
invariant re-verified after every answer. An `--offline` mode runs the
same loop with a keyword stub for anyone without credentials, labelled
as a stub at every point the distinction could be misread.

## 34. The tests are hermetic; the scripts are not

Phase 5B deliberately made the test suite runnable without credentials
(section 23). 287 tests pass with `GEMINI_API_KEY` unset.

The scripts were never given the same treatment, and nothing says so.
Running `verify_agent_ask_real_model.py` without exporting `.env`
produces:

    RuntimeError: GEMINI_API_KEY is not configured.

with no indication that `.env` exists and needs sourcing. We hit this
ourselves immediately after writing the script.

Partly addressed: `demo_agent.py` now falls back to offline mode and
prints the export command. The other real-model scripts still fail
bluntly, and the README documents the requirement rather than the code
handling it.

Recorded rather than fixed, because adding `load_dotenv()` would put a
dependency and a behaviour change into Phase 5 files that are otherwise
frozen and green.

## 35. Measured accuracy, and what it does not mean

`scripts/report_accuracy.py` compares full-batch `decide_batch()` output
against `ground_truth.json`, which the pipeline never reads:

    Ground-truth entries    : 63
    Rejected at ingestion   : 2 (corrupted, never decisioned)
    Decisions evaluated     : 61

    STATUS accuracy         : 61/61 (100%)
    EXCEPTION-CODE accuracy : 61/61 (100%)

Every category at 100%, including the six ambiguous cases the per-case
E2E harness structurally cannot evaluate (section 26).

**A 100% figure deserves more scrutiny than a lower one, so:**

The two corrupted records are counted as `rejected_at_ingestion`, not
dropped. Dropping them would have inflated the percentage against a
smaller denominator, and a test asserts the accounting adds up.

Ground truth and the engine both derive from the same specification --
the decision table. Two labels disagreed with it and were corrected
(sections 14-15), and both corrections are printed with the result and
stored in the artifact rather than left to be discovered.

What this measures is that the implementation matches its own
specification across ten adversarial categories. That is a real result
and it is narrower than "the engine handles reconciliation". The
dataset is self-generated, the settlement model is one transaction to
one bank credit, and the narration formats are invented. Those limits
are in README.md and they are not small.

**Deliberately absent:** no test asserts a minimum accuracy. A test that
failed when accuracy dropped would create pressure to adjust ground
truth until it passed -- which is exactly the failure recorded twice
already in this log.
```

---

## Also update section 36 ("What is not done")

The first bullet currently reads:

```markdown
- **The agent itself.** There is no orchestration loop, no tool
  selection, no conversational surface...
```

**Replace with:**

```markdown
- **LLM-assisted candidate matching** -- built, not connected
  (section 24). The model explains decisions and answers questions
  about the batch. It does not participate in matching, so its
  contribution to the financial outcome is still zero by design.
- **Agent tool-selection accuracy is six questions.** A smoke test, not
  an evaluation. A real measurement needs a held-out labelled question
  set.
```

And in the milestone block, add:

```
phase-6-final        (agent tool layer, ask() loop, real-model demo)
```

# 36. Corrections to earlier entries in this log

Two entries above — sections 3 and 11 in the original numbering, on fuzzy
precision — reported this:

```
Threshold 60 → Precision 0.12, Recall 1.00
Threshold 70 → Precision 0.13, Recall 1.00
Threshold 90 → Precision 0.13, Recall 1.00
Threshold 95 → Precision 0.00, Recall 0.00
```

and attributed the poor precision to narrow amount diversity in the
synthetic dataset.

**Both the number and the explanation are withdrawn.**

## 37. The stated cause was tested and disproven

After fix A1 (section 16), accidental net collisions went to **zero**.
Precision did not move. It stayed at 0.12.

If narrow amount diversity had been the cause, removing it would have
changed the number. It did not.

## 38. The real cause — the metric counted correct matches as errors

The sweep computed, per record:

```python
fuzzy_fired = best_fuzzy_score >= threshold
FP          = (category != "reference_mismatch_fuzzy") and fuzzy_fired
```

That asks *"did fuzzy fire?"* — not *"did fuzzy pick the wrong record?"*

The generator writes narration as `"NEFT CR UTR123456789 MERCH_001"`. The
narration **contains the UTR verbatim**. So for every clean transaction,
`partial_ratio(pg.utr, narration)` returns 100. It cleared every
threshold, and because its category was not `reference_mismatch_fuzzy`,
it was counted as a false positive.

**Those 43 "false positives" were correct matches being scored as
errors.**

The tell was in the output the whole time: FP stayed at exactly 43 across
thresholds 60 through 95. A similarity-based false-positive count has to
fall as the threshold rises. A flat count means the metric was
independent of the variable it claimed to sweep. I looked at that table
several times without noticing.

## 39. The fuzzy tier is unreachable on this dataset

Rewriting the benchmark to report tier reachability first gave:

```
category                    exact_utr  exact_txn  fuzzy   none
----------------------------------------------------------------
reference_mismatch_fuzzy    0          6          0       0
...
Records that genuinely reach the fuzzy tier: 0
```

**Zero of 61 records reach the fuzzy tier.**

`find_bank_candidates()` tries exact UTR, then exact resolved txn_id, then
guarded fuzzy. `build_reference_mismatch()` corrupts only `bank["utr"]`
and leaves `bank_ref` as `BANKREF_<txn_id>`, so tier 2 resolves every one
of those records before fuzzy is consulted.

**This was already known.** `tests/test_matching.py:732` says:

> "In our data, this typically resolves via exact_txn since bank_ref
> still encodes the correct txn_id even when UTR is corrupted. The fuzzy
> tier exists for the realistic case where no such structured convention
> is available."

So it was not a discovery. The design fact was written down in a test
docstring and never reached the numbers published elsewhere in this
document. I kept reporting a precision figure for a code path production
does not take.

**Current position.** The fuzzy tier is a designed fallback for banks
with no structured reference convention. Our data uses
`BANKREF_<txn_id>`, so it is unexercised end-to-end. It has unit coverage
in isolation and no end-to-end measurement. `tune_fuzzy_threshold.py` now
reports tier reachability and refuses to print a sweep when the tier is
unreachable, because a tier that never runs has no precision.

**Why the fix was deferred.** Making it reachable means breaking
`bank_ref`, which is one line in the generator but three files in
practice — `verify_data.py` CHECK 4 and CHECK 6 both link bank rows
through `bank_ref` precisely because UTR is unreliable in these
categories, and `test_matching.py:732` is named for the signal being
removed. That is a change to the verification harness in exchange for
exercising a tier that is not on the primary path. Deferred deliberately,
recorded rather than hidden.

---

# 40. The pattern across all of this

Three separate things that looked like engine weaknesses were measurement
defects:

| Reported as | Actually was |
|---|---|
| Ground-truth divergence (L1, L2) | Labels asserting statuses the decision table cannot produce |
| Six fail-open auto-matches | A per-case harness structurally unable to observe batch-relational properties |
| Fuzzy precision 0.13 | A benchmark counting correct matches as false positives, on a tier that never runs |

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

# 41. Current state

```
184 / 184 tests passing
Gold baseline: stable
Baseline divergences: 0
Not evaluable (batch-relational): 6
Raw mismatches before exclusion: 6
Accidental net collisions: 0
```

**Deterministic core (Phases 0–4).** Decimal firewall, per-record fault
isolation, three-tier matching with deterministic tie-breaking,
independent GST/TDS validation, per-record seller ledger, priority
decision table with 512/512 combination coverage, full reason-code
preservation.

**AI boundary (5A).** Preemptive timeout, typed contracts with no
financial vocabulary, deterministic fallback, invariance tests proving
the decision is unchanged whether the model succeeds or fails.

**Real model (5B).** Gemini 3.1 Flash-Lite behind that boundary,
explanation only.

**Evaluation (5C).** Held-out narration and explanation sets, semantic
faithfulness scoring, E2E gold baseline harness, throughput benchmark,
tier reachability measurement.

---

# 42. What is not done

Listing these explicitly so nothing here is read as a completed claim.

- **The agent itself.** There is no orchestration loop, no tool
  selection, no conversational surface. A human runs scripts and reads
  JSON. The model is called by one function to rephrase one decision.
  This is the largest gap.
- **LLM-assisted candidate matching** — built, not connected (section 24).
- **Fuzzy tier** — unreachable end-to-end (section 33).
- **Held-out sets** — 8 cases each; too small to generalise from.
- **Real bank narration** — formats are self-invented. `BANKREF_<txn_id>`
  is a convention no real bank provides, and it makes matching easier
  than the real problem.
- **Settlement model** — one PG transaction to one bank credit. Real
  settlements are batched: many transactions net into one transfer, minus
  refunds and chargebacks. The hard part of real reconciliation is
  decomposing that, and this system never has to.
- **Scale** — throughput is a benchmark on one machine, not a capacity
  claim.

---

# 43. Milestones

```
phase-3-final
    ↓
phase-4-final
    ↓
phase-5-boundary
    ↓
phase-5-final        (real Gemini integration + evaluation)
```

These are architectural checkpoints, not version numbers.

---

# 44. Closing

The most useful thing I built was not the reconciliation engine. It was
the set of harnesses that kept disagreeing with it — and the discipline
of checking, each time, which one was actually wrong.

Usually it was the harness. That is not a comfortable result to write
down, but it is the honest one, and it is the reason I trust the engine
more than I would have if everything had passed the first time.