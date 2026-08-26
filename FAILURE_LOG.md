# Engineering Failure Log

## Deterministic Finance Controller — Phase 0 → Phase 5 AI Boundary

This document records material engineering failures, correctness defects, architectural weaknesses, misleading measurements, test-discovered gaps, and the corresponding recovery actions encountered while building the reconciliation and finance-control pipeline.

The purpose is not to present a perfect development history.

The purpose is to demonstrate how the system was made progressively more correct, measurable, auditable, and safe around financial decisions.

**Financial principle:**

> AI may assist interpretation, but financial truth must remain deterministic, evidence-backed, and independently verifiable.

Phase 0–4 establishes that deterministic financial core.

Phase 5 establishes the bounded AI boundary around it.

This log intentionally stops at the Phase 5 AI boundary.

Real-model integration and real-model evaluation are subsequent work.

## 1. Engineering Failure Philosophy

The project treats failures as architecture signals rather than isolated implementation mistakes.

A failure is considered important when it exposes one or more of:

* incorrect financial behavior
* hidden coupling between independent financial controls
* incomplete policy coverage
* contradictory internal state
* insufficient test coverage
* misleading benchmark methodology
* unverified assumptions about production behavior
* unsafe AI authority boundaries
* weak fallback behavior
* an interface/contract mismatch
* a gap between claimed behavior and measured behavior

Every material failure is therefore recorded with:

* **Symptom**
* **Detection mechanism**
* **Root cause**
* **Impact**
* **Corrective action**
* **Regression protection**
* **Remaining limitation**

## 1A. Five Strongest Failure-Recovery Stories

This section highlights the five highest-value engineering failure-recovery stories across Phase 0 through the Phase 5 boundary. These are not separate claims from the detailed incident record below; they are the clearest engineering narratives connecting failure detection to architectural correction and measurable recovery.

### Failure-Recovery Story 1 — The Catch-All Became a Production-Like Defect Detector

**What failed**

The decision table was made exhaustive across all 512 independent boolean combinations, and a lowest-priority catch-all was introduced for logically unreachable states.

The important discovery came afterward: the supposedly unreachable state actually occurred in the real generated batch.

**What exposed it**

The catch-all produced a runtime warning with:

```text
fully_clean=False
```

while all other context flags were also false.

**Root cause**

`missing_bank` and `missing_invoice` were derived from one source of truth, while tax verification depended on another.

**Recovery**

The source-of-truth construction was aligned to the actual record objects:

```text
missing_bank = match_result.bank_record is None
missing_invoice = match_result.invoice_record is None
```

A real-batch regression was then added to ensure the catch-all no longer fires.

**Engineering significance**

This is one of the strongest examples in the project because the test suite did not merely prove a theoretical property. It exposed a real internal consistency defect in the production-like execution path.

### Failure-Recovery Story 2 — Fuzzy Matching TP = 0 Exposed a Financial Representation Bug

**What failed**

The fuzzy-reference evaluation initially produced:

```text
TP = 0
```

for the expected fuzzy-reference category.

**Root cause**

The candidate guard was comparing the wrong financial amount representation, where gross and net settlement representations could differ.

**Recovery**

The amount guard was corrected to compare the appropriate financial representation before fuzzy similarity was evaluated.

**Measured recovery**

After correction:

```text
TP = 6
FN = 0
Recall = 1.00
```

for the six self-generated fuzzy-reference examples.

**Engineering significance**

The failure was not patched by weakening the matcher. The financial guard itself was corrected, and the recovery was demonstrated through measurement.

### Failure-Recovery Story 3 — Benchmark Results Exposed a Dataset Limitation Instead of a False Production Claim

**What failed**

After the fuzzy guard was corrected, recall was strong but precision remained low.

Observed behavior included:

```text
Threshold 60 → Precision 0.12, Recall 1.00
Threshold 70 → Precision 0.13, Recall 1.00
Threshold 90 → Precision 0.13, Recall 1.00
Threshold 95 → Precision 0.00, Recall 0.00
```

**Root cause**

The synthetic dataset contained only a small number of distinct transaction amounts. Multiple unrelated transactions therefore shared similar amounts and dates.

**Recovery**

The benchmark was aligned with the actual guarded matcher, and the synthetic data limitation was explicitly documented rather than hidden.

**Engineering significance**

This is a failure of measurement interpretation rather than a code defect.

The engineering response was to distinguish:

**measured behavior on this dataset**

from:

**general-world matcher performance**

That distinction prevents a misleading fintech performance claim.

### Failure-Recovery Story 4 — The First LLM Timeout Was Cosmetic, Not Operational

**What failed**

The first guardrail implementation checked elapsed time only after the LLM call returned.

A simulated 15-second call therefore caused the pipeline to wait 15 seconds despite a claimed 10-second timeout.

**What exposed it**

The real timeout regression test failed with:

```text
Pipeline waited 15.0s for a call that should have timed out at 10s
```

**Recovery**

The guardrail was changed to use:

```text
ThreadPoolExecutor
future.result(timeout=...)
```

The pipeline now returns after the configured timeout without waiting for the underlying thread to complete.

**Engineering significance**

The test converted a design intention into an operationally verified property. The implementation also explicitly documents the Python limitation that a running thread cannot be forcibly terminated.

### Failure-Recovery Story 5 — The AI Contract Was Tested but Initially Was Not the Real Execution Boundary

**What failed**

Typed contracts were introduced, but one real execution path still accepted a raw transaction-ID string.

This meant the contract existed and passed its own tests while the actual candidate lookup interface was not enforcing it.

**What exposed it**

The invariant test passed:

```text
"TXN_99999_NONEXISTENT"
```

directly into `lookup_proposed_txn_id()` after the production function had been changed to expect `NarrationExtraction`.

This produced:

```text
AttributeError:
'str' object has no attribute 'proposed_txn_id'
```

**Recovery**

The lookup boundary was changed to consume the formal `NarrationExtraction` contract, and the test was migrated to construct and pass that typed object.

The explanation path was similarly migrated to use the `Explanation` contract.

**Engineering significance**

The important lesson is that a tested contract is not sufficient if the production execution path bypasses it. The boundary had to be enforced end-to-end.

### Additional AI Judgment Correction — Fabricated Confidence Was Removed

A smaller but important Phase 5 judgment issue was also corrected.

`NarrationExtraction` initially used:

```text
confidence_hint = "medium"
```

even though the prompt did not actually request or validate model-derived confidence.

That was replaced with:

```text
confidence_hint = "unspecified"
```

until a genuine confidence signal can be designed and empirically validated.

This is intentionally treated as an engineering judgment correction rather than a model-performance result: confidence should never be manufactured merely because a schema has room for it.

### Why These Five Stories Matter

Together, these incidents demonstrate the project's complete engineering response pattern:

```text
detect
    ↓
reproduce
    ↓
identify root cause
    ↓
correct the architecture or methodology
    ↓
add regression protection
    ↓
measure again
    ↓
document remaining uncertainty
```

They also map directly to the core evaluation dimensions:

**Problem taste**

→ chose failures involving financial correctness, policy completeness, benchmark validity, and AI authority

**Build quality**

→ corrected implementation defects and added regression protection

**AI judgment**

→ constrained AI to proposal/explanation and removed fabricated confidence

**Failure recovery**

→ converted concrete failures into durable architectural controls

The detailed incident records that follow remain the authoritative history; this section is a high-level engineering synthesis and does not replace them.

## 2. Phase 0 — Foundation and Data Contracts

### 2.1 Principle Established: Raw Financial Sources Must Not Flow Directly Into Business Logic

**Risk**

Payment-gateway settlements, bank statements, and merchant invoices contain different schemas, naming conventions, representations, and optional fields.

Allowing downstream matching or tax logic to consume raw source structures would make every downstream component responsible for understanding every source format.

**Architectural correction**

The pipeline was structured around:

```text
Raw Sources
    ↓
Ingestion
    ↓
Normalization
    ↓
Matching
    ↓
Tax Validation
    ↓
Decision Policy
```

The ingestion layer acts as the schema boundary and normalization creates the canonical representation consumed by downstream components.

**Why this matters**

This prevents source-specific assumptions from leaking into financial decision logic.

It also makes the deterministic core independently testable.

**Status**

**CLOSED.**

## 3. Phase 1 — Normalization and Canonical Representation

### 3.1 Source Representation Differences Had to Be Isolated

**Risk**

Matching financial records directly using source-specific fields creates fragile dependencies such as:

* different transaction identifiers
* different reference formats
* different date representations
* different amount representations
* different narration structures

**Corrective architecture**

A canonical normalized record was introduced so that matching and tax validation operate on a stable internal contract rather than raw files.

**Result**

Downstream modules can reason about normalized records without knowing the original source representation.

**Status**

**CLOSED.**

## 4. Phase 2 — Tax Verification

### 4.1 GST and TDS Were Initially Coupled

**Failure**

The decision-context logic contained a coupling where TDS mismatch was effectively suppressed when GST verification had already failed.

The problematic behavior was equivalent to:

```text
tds_mismatch = not tax.tds_verified and tax.gst_verified
```

**Why this was dangerous**

GST and TDS are independent financial controls.

A transaction can simultaneously contain:

* a GST mismatch
* a TDS mismatch

Suppressing the second error because the first one exists produces incomplete financial evidence.

This is particularly dangerous in reconciliation because the output may look like a single exception when multiple statutory issues actually exist.

**Corrective action**

GST and TDS were evaluated independently:

```text
gst_mismatch = not tax.gst_verified
tds_mismatch = not tax.tds_verified
```

**Regression requirement**

A transaction with:

```text
gst_mismatch = True
tds_mismatch = True
```

must produce:

```text
status = TAX_MISMATCH
```

and preserve both:

```text
ERR_GST_MISMATCH
ERR_TDS_VARIANCE
```

**Status**

**CLOSED.**

## 5. Phase 2 — Multiple Violations Were Being Compressed Into One Reason

**Failure**

The original decision output could preserve only the winning exception reason rather than all violated conditions.

**Problem**

A single-valued status is appropriate for classification, but a single reason code is insufficient for audit evidence.

For example:

```text
TAX_MISMATCH
```

may be correct as the overall status while the evidence actually contains:

```text
ERR_GST_MISMATCH
ERR_TDS_VARIANCE
```

**Corrective architecture**

The system was changed to distinguish:

```text
Status
    =
overall decision classification
```

from:

```text
reason_codes
    =
complete set of violated conditions
```

An `_all_violated_codes(context)` path was introduced so that all applicable violations are preserved.

**Why this matters**

This creates a better audit trail without destroying the deterministic single-valued decision interface.

**Status**

**CLOSED.**

## 6. Phase 3 — Matching Engine Complexity and Confidence Needed Explicit Evidence

### 6.1 Matching Cannot Be Treated as a Boolean Lookup

**Risk**

Financial reconciliation requires candidate discovery, evidence comparison, confidence classification, and ambiguity handling.

A simplistic:

```text
match / no-match
```

model hides the reason why a candidate was accepted or rejected.

**Architectural response**

The matching engine was structured around evidence-bearing candidate resolution and confidence tiers.

The matching summary was explicitly measured:

```text
HIGH
MEDIUM
LOW
NO_MATCH
Ambiguous
```

**Verification observed**

The Phase 3 test run produced:

```text
Total processed  : 61
HIGH confidence  : 36
MEDIUM confidence: 8
LOW confidence   : 15
NO_MATCH         : 2
Ambiguous flagged: 3
```

**Status**

**CLOSED.**

## 7. Phase 3/4 — Fuzzy Matching Guard Was Found to Have a Gross-vs-Net Amount Problem

**Failure**

The fuzzy matching evaluation initially produced:

```text
TP = 0
```

for the expected fuzzy-reference category.

This indicated that the genuine candidate was not being surfaced.

**Root cause**

The amount comparison was using the wrong financial amount representation for the fuzzy candidate guard.

The payment-gateway and bank-side representations could differ because of gross versus net settlement treatment.

**Impact**

The fuzzy tier could reject the correct candidate before fuzzy reference similarity was even evaluated.

This made the apparent fuzzy recall look worse than the actual matcher behavior.

**Corrective action**

The amount guard was corrected to compare the appropriate gross/net financial representation.

**Evidence after correction**

The fuzzy sweep recovered:

```text
TP = 6
FN = 0
```

at thresholds from 60 through 90.

Therefore:

```text
Recall = 1.00
```

for the six self-generated fuzzy-reference examples.

**Status**

**CLOSED.**

## 8. Phase 4 — Fuzzy Threshold Benchmark Was Initially Not Sufficiently Representative

**Finding**

After the gross-vs-net correction, recall recovered, but precision remained very poor.

Observed behavior was approximately:

```text
Threshold 60 → Precision 0.12, Recall 1.00
Threshold 70 → Precision 0.13, Recall 1.00
Threshold 90 → Precision 0.13, Recall 1.00
Threshold 95 → Precision 0.00, Recall 0.00
```

**Initial interpretation risk**

A naive interpretation would be:

> "The fuzzy matcher is unsafe."

That conclusion would have been too broad.

**Root cause of poor precision**

The benchmark dataset had only a small number of distinct transaction amounts.

Multiple unrelated transactions therefore shared similar amounts and dates.

The amount + date guard was consequently much less selective than it would be with a realistic continuous distribution of settlement values.

**Additional benchmark issue identified**

The fuzzy sweep had to be aligned with the actual guarded matcher rather than measuring a looser fuzzy condition in isolation.

The evaluation was subsequently changed to measure the guarded path.

**Result**

The benchmark now measures the actual guarded fuzzy behavior and honestly reports its limitation.

**Remaining limitation**

The dataset is synthetic and contains narrow amount diversity.

Therefore the observed precision is a measurement of this dataset, not a claim about general-world bank narration performance.

**Status**

**CLOSED AS AN ENGINEERING FINDING.**

The limitation remains documented rather than hidden.

## 9. Phase 4 — Decision Logic Was Too Implicit

**Failure**

The exception manager originally contained ad-hoc conditional branching.

**Risk**

When financial policy grows, scattered `if/elif` logic becomes difficult to:

* inspect
* audit
* reason about
* exhaustively test
* prove for priority conflicts

**Corrective architecture**

A formal priority-ordered decision table was introduced:

```text
DecisionContext
    ↓
ordered DecisionRule list
    ↓
deterministic evaluate()
    ↓
MatchDecision
```

The rules are explicitly prioritized.

**Benefit**

Decision policy becomes data-like and reviewable rather than hidden inside control flow.

**Status**

**CLOSED.**

## 10. Phase 4 — fully_clean Was Missing From Decision Context

**Failure**

A decision rule expected:

```text
context.fully_clean
```

but the `DecisionContext` definition / construction path did not consistently provide it.

This produced:

```text
AttributeError
```

when the fully-clean rule was exercised.

**Root cause**

The policy definition and context-construction contract had drifted apart.

**Corrective action**

`fully_clean` was explicitly added as the ninth field:

```text
fully_clean: bool = False
```

and was explicitly passed when constructing `DecisionContext`.

**Why this matters**

The issue was not merely a missing attribute.

It exposed a deeper architectural requirement:

> Policy inputs must be represented by an explicit, typed contract and constructed consistently at the boundary.

**Regression coverage**

The decision table was expanded to include the ninth boolean field.

**Status**

**CLOSED.**

## 11. Phase 4 — Decision Table Was Not Total Under Independent Combinatorial Testing

**Failure**

The exhaustive decision-table sweep tested all independent boolean inputs.

With nine boolean fields:

```text
2^9 = 512
```

combinations exist.

One combination was:

```text
no_candidates_found = False
is_ambiguous       = False
low_confidence     = False
missing_bank       = False
missing_invoice    = False
gst_mismatch       = False
tds_mismatch       = False
tax_unverifiable   = False
fully_clean        = False
```

This state is logically unreachable under the intended `_build_context()` derivation, but the independent table test correctly discovered that the policy itself had no rule for it.

**Corrective action**

A lowest-priority catch-all rule was introduced:

```text
catch_all_unresolved_state
```

with `HUMAN_REVIEW` behavior.

**Design principle**

The catch-all is not intended to represent normal production behavior.

It is a safety net preventing an unexpected context from crashing or silently producing an undefined financial outcome.

**Result**

The decision table became total over the entire input space.

**Verification**

```text
512/512
```

combinations resolve deterministically.

**Status**

**CLOSED.**

## 12. Phase 4 — Catch-All Rule Then Exposed a REAL Context Construction Inconsistency

**Failure**

The catch-all was initially expected to be structurally unreachable.

However, the real pipeline produced:

```text
RuntimeWarning:
Decision policy fell through to catch-all
```

with:

```text
fully_clean=False
```

while every other context flag was also:

```text
False
```

**Why this mattered**

The combinatorial test had not merely found a theoretical edge case.

The production-like generated batch had actually produced the impossible state.

Therefore the catch-all safety mechanism had successfully discovered a real internal consistency defect.

**Root cause**

`missing_bank` and `missing_invoice` were initially derived from:

```text
match_result.sources_present
```

while tax verification was gated using:

```text
match_result.bank_record
match_result.invoice_record
```

These represented two different sources of truth.

If they ever disagreed, the context could become internally inconsistent.

**Corrective action**

The missing-source flags were aligned with the actual record objects:

```text
missing_bank = match_result.bank_record is None
missing_invoice = match_result.invoice_record is None
```

This made the source-presence decision use the same objects that controlled the downstream tax verification path.

**Regression test added**

A real-batch test was added to assert that no generated production-like transaction triggers the catch-all.

**Final result**

The warning disappeared and the real-batch regression passed.

**Status**

**CLOSED.**

## 13. Phase 4 — Throughput Had No Direct Measurement

**Finding**

The architecture claimed throughput characteristics without initially having a direct measurement artifact.

**Risk**

A reviewer could reasonably ask:

> "What is the measured records/sec?"

and the system had no defensible answer.

**Corrective action**

A benchmark was introduced:

```text
scripts/benchmark_throughput.py
```

It measures:

```text
load
normalize
match
decide
total
```

across:

```text
60
300
1000
5000
```

records.

The benchmark reports:

```text
records_per_second
```

and persists results to:

```text
data/throughput_benchmark.json
```

**Important limitation**

This is a benchmark on the project's generated dataset and execution environment.

It is not a production-scale performance guarantee.

**Status**

**CLOSED AS MEASURED EVIDENCE.**

## 14. Phase 4 — Fuzzy Threshold Was Not Allowed to Become an Arbitrary Configuration Number

**Finding**

A fuzzy threshold without empirical evaluation would be an unjustified magic number.

**Corrective action**

A threshold sweep was introduced across:

```text
60
65
70
75
80
85
90
95
```

with:

```text
TP
FP
FN
precision
recall
```

measured.

**Important methodological correction**

The sweep was aligned with the actual guarded matcher rather than pretending that raw fuzzy similarity alone represented the production matching path.

**Status**

**CLOSED.**

## 15. Phase 4 — Synthetic Data Ceiling Was Explicitly Recognized

**Finding**

The project's ground truth is self-generated.

Therefore benchmark numbers such as fuzzy precision and recall are real measurements, but they do not establish generalization to unseen real-world bank narration formats.

**Corrective action**

The limitation was explicitly documented.

**Principle**

> A measured number is not automatically a production claim.

**Status**

**CLOSED AS A DOCUMENTED LIMITATION.**

## 16. Phase 4 — Decision Policy Required Exhaustive Regression

**Requirement**

Individual happy-path tests were insufficient for a policy engine with multiple interacting boolean conditions.

**Corrective action**

The decision table received combinatorial coverage over all nine boolean inputs.

Tests explicitly verify:

* no_candidates_found priority
* ambiguity priority
* GST/TDS conflict priority
* fully-clean resolution
* unique priorities
* dense priority sequence
* total resolution of all 512 combinations

**Result**

```text
512/512
```

contexts resolve deterministically.

**Status**

**CLOSED.**

## 17. Phase 4 — Integration Between Matching, Tax, and Decision Layers Needed Explicit Verification

**Risk**

Unit tests can pass independently while the actual pipeline still produces incorrect context.

**Corrective action**

Integration tests were run through:

```text
matching
    ↓
tax
    ↓
decision
```

including tax mismatch cases and real generated records.

**Final observed decision distribution**

```text
PARTIAL_MATCH   4
MATCHED         33
HUMAN_REVIEW    14
TAX_MISMATCH    7
AMBIGUOUS       3
```

**Status**

**CLOSED.**

## 18. Phase 4 — Documentation Had to Catch Up With the Engineering Reality

**Finding**

Some limitations were discovered after the core behavior had already been implemented.

**Required disclosure**

The project explicitly acknowledges that bank narration regex fallback was tested against synthetic generator formats and that real-world narration variance remains untested.

**Why this matters**

A fintech system should not present synthetic-data coverage as production coverage.

**Status**

**CLOSED / DOCUMENTED.**

## 19. Phase 4 — Intermediate "Complete" Claims Were Tightened

**Engineering lesson**

Several checkpoints initially appeared complete while later tests exposed additional closure work.

Examples included:

* the two tax fixes being treated as the entire Phase 4 closure
* throughput/fuzzy work being treated as optional despite being evaluation evidence
* the catch-all being treated as purely theoretical until it fired on real generated data
* benchmark numbers being discussed before their methodology was fully aligned with the guarded matcher

**Corrective process**

The closure criterion was changed from:

> "the code looks correct"

to:

> "the code, tests, benchmarks, integration behavior, and documented limitations all agree."

**Status**

**CLOSED.**

## 20. Phase 5 Boundary — Initial Timeout Implementation Was Not Actually Preemptive

**Failure**

The first guardrail implementation measured elapsed time after:

```text
call_fn()
```

returned.

That meant a call that took 15 seconds could only be declared "too slow" after waiting the full 15 seconds.

**Why this was unacceptable**

A finance pipeline cannot claim a 10-second timeout if it actually blocks for 15 seconds.

This was a cosmetic timeout, not an operational timeout.

**Detection**

A real timeout regression test was introduced using a function that sleeps for 15 seconds.

The test required the pipeline to return in less than 12 seconds.

**Initial result**

The test failed:

```text
Pipeline waited 15.0s for a call that should have timed out at 10s
```

**Corrective action**

The guardrail was changed to use:

```text
ThreadPoolExecutor
future.result(timeout=...)
```

The pipeline returns after the configured timeout even if the underlying thread continues executing.

**Important Python limitation**

Python cannot forcibly terminate an arbitrary running thread.

Therefore the guarantee is:

> pipeline does not wait

not:

> underlying provider execution is forcibly killed

This distinction is explicitly documented.

**Final verification**

The real timeout test passed.

**Status**

**CLOSED FOR CURRENT SYNCHRONOUS BOUNDARY.**

## 21. Phase 5 — AI Output Initially Used Bare Strings

**Finding**

Narration extraction returned a bare transaction-ID string.

Explanation generation also returned a bare string.

**Risk**

A bare string does not communicate the authority boundary.

It allows future code to accidentally treat an AI output as a domain-level decision object.

**Corrective architecture**

Typed contracts were introduced:

```text
NarrationExtraction
Explanation
```

Both are frozen dataclasses.

**Design principle**

The AI boundary should have a type that expresses:

> proposal

rather than:

> decision

**Status**

**CLOSED.**

## 22. Phase 5 — AI Contract Initially Carried Artificial Confidence

**Finding**

`NarrationExtraction` initially contained:

```text
confidence_hint = "medium"
```

even though the prompt did not actually request or validate model-derived confidence.

**Problem**

That would create fabricated precision.

A hardcoded "medium" is not model confidence.

**Corrective action**

The field was changed to:

```text
confidence_hint = "unspecified"
```

until a genuine confidence signal is designed and empirically validated.

**Engineering principle**

> Do not manufacture confidence metrics merely because a schema has room for one.

**Status**

**CLOSED.**

## 23. Phase 5 — Contract Layer Initially Existed but Was Not Fully Connected to the Execution Path

**Finding**

The contract types existed and were tested independently, but the actual candidate lookup path still accepted a raw string.

**Problem**

This created a "tested boundary" that was not necessarily the real boundary.

**Corrective action**

`candidate_lookup.py` was changed to accept:

```text
NarrationExtraction
```

rather than:

```text
str
```

The actual execution path now consumes the typed contract.

**Status**

**CLOSED.**

## 24. Phase 5 — Explanation Contract Migration Exposed Stale Call Sites

**Failure**

After `Explanation` became the formal return type, old tests still asserted:

```text
result.value == "TXN_00042"
```

instead of accessing the contract field.

**Result**

The guardrail test failed even though the underlying AI extraction logic was working.

**Corrective action**

Tests and callers were updated to access:

```text
result.value.proposed_txn_id
```

for narration extraction and:

```text
result.value.text
```

for explanations.

**Status**

**CLOSED.**

## 25. Phase 5 — Candidate Lookup Test Passed a Raw String After Contract Migration

**Failure**

The invariant test still called:

```text
lookup_proposed_txn_id("TXN_99999_NONEXISTENT", empty_index)
```

while the production function now required:

```text
NarrationExtraction
```

This produced:

```text
AttributeError:
'str' object has no attribute 'proposed_txn_id'
```

**Root cause**

The test had not been migrated alongside the production interface.

**Corrective action**

The test now constructs:

```text
NarrationExtraction(proposed_txn_id="TXN_99999")
```

and passes the typed object into the lookup boundary.

**Status**

**CLOSED.**

## 26. Phase 5 — Demo Used the New Explanation Contract Incorrectly

**Failure**

The fallback path printed the entire dataclass:

```text
Explanation(text='...', ...)
```

instead of the actual explanation text.

**Corrective action**

The demo now accesses:

```text
fallback.text
```

**Why this matters**

This is an interface-consistency correction, not a financial correctness issue.

It ensures demonstrations represent the actual public behavior cleanly.

**Status**

**CLOSED.**

## 27. Phase 5 — Deterministic Invariant Protection Was Added

**Requirement**

It is insufficient to say:

> "The AI should not change financial decisions."

The system needs tests demonstrating that property.

**Corrective action**

Before/after invariant tests compare:

* status
* exception_code
* reason_codes
* confidence_score
* evidence

before and after successful and failed agent explanation calls.

**Result**

The financial decision remains unchanged.

Only additive explanation content changes.

**Status**

**CLOSED.**

## 28. Phase 5 — Failed LLM Calls Must Degrade Gracefully

**Failure scenario**

The LLM may fail because of:

* provider outage
* network failure
* malformed response
* timeout
* invalid schema

**Required behavior**

The deterministic decision must survive unchanged.

**Corrective architecture**

Every AI call goes through:

```text
call_llm_bounded()
```

and produces:

```text
AgentCallResult
```

rather than a raw value.

Failure therefore becomes an explicit state.

**Fallback**

Explanation generation uses a deterministic template fallback.

Narration extraction simply contributes no candidate when the AI call fails.

**Status**

**CLOSED.**

## 29. Phase 5 — Nonexistent LLM Candidate Must Never Become a Match

**Risk**

A model could hallucinate:

```text
TXN_999999
```

The system must not trust that identifier merely because it is syntactically valid.

**Corrective architecture**

Candidate lookup checks the proposed identifier against the real deterministic candidate index.

Therefore:

```text
syntactically valid
    ≠
financially valid
```

**Required flow**

```text
LLM proposal
    ↓
typed contract
    ↓
deterministic candidate lookup
    ↓
existing candidate?
    ↓
normal deterministic verification
```

**Status**

**CLOSED AT BOUNDARY LEVEL.**

## 30. Phase 5 — The AI Was Explicitly Prevented From Becoming the Financial Decision Maker

**Architectural boundary**

The AI is permitted to:

* propose a transaction reference
* explain an already-computed decision

The AI is not permitted to:

* calculate tax
* calculate settlement amount
* determine match/no-match
* override status
* override exception_code
* modify reason_codes
* modify financial evidence
* create a financial decision

**Structural enforcement**

The typed contracts deliberately contain no fields for:

* amount
* GST
* TDS
* status
* decision
* exception_code
* matched state

**Status**

**CLOSED FOR PHASE 5 BOUNDARY.**

## 31. Phase 5 — Real Model Was Deliberately NOT Integrated Yet

**Important architectural decision**

The current Phase 5 boundary uses injected/mock LLM call functions.

This is intentional.

The project does **NOT** claim:

> production LLM integration complete

or:

> real-world LLM performance validated

**Why this is correct**

The deterministic boundary must be proven before introducing provider-specific behavior.

The sequence is:

```text
deterministic core
    ↓
bounded AI boundary
    ↓
contract verification
    ↓
guardrail verification
    ↓
invariant verification
    ↓
real model integration
    ↓
real model evaluation
```

**Status**

**INTENTIONALLY OPEN — NEXT STAGE.**

## 32. Phase 5 — Candidate Integration Into the Deterministic Matching Engine Is Intentionally Deferred

**Current working-tree discovery**

An additional function exists in the local working tree:

```text
find_bank_candidates_with_llm_assist(...)
```

This represents the future integration point for the LLM-assisted candidate proposal.

**Important distinction**

It is **NOT** part of the frozen Phase 4 architecture.

It is also **NOT** part of the phase-5-boundary milestone.

**Reason for deferral**

The function changes the execution path of matching and therefore requires dedicated evaluation before being treated as a production-ready Phase 5 feature.

**Required future evaluation**

Before merging this integration, verify:

* deterministic tiers retain priority
* LLM is invoked only after deterministic tiers fail
* proposed ID must exist in deterministic index
* amount/date constraints remain enforced
* LLM cannot directly create a match
* LLM failure produces identical deterministic behavior
* real-model latency is bounded
* false-positive rate is measured
* recall improvement is measured
* held-out narration cases are evaluated

**Status**

**INTENTIONALLY DEFERRED TO REAL-MODEL INTEGRATION/EVALUATION.**

## 33. Phase 5 — Demo Success Must Not Be Confused With Model Evaluation

**Finding**

The mock explanation successfully generated human-readable text.

The broken mock successfully demonstrated fallback.

**What this proves**

It proves:

* integration mechanics
* contract flow
* fallback flow
* invariant preservation

**What it does NOT prove**

It does not prove:

* real LLM accuracy
* real narration extraction accuracy
* provider latency
* provider reliability
* prompt robustness
* hallucination rate
* cost
* generalization

**Status**

**DOCUMENTED LIMITATION.**

## 34. Final Verified State Before Real-Model Integration

### Phase 0–4

The deterministic financial core is frozen at:

```text
phase-4-final
```

It contains:

* ingestion
* normalization
* deterministic matching
* tax validation
* formal decision policy
* complete reason-code evidence
* exhaustive decision-table testing
* throughput measurement
* fuzzy evaluation
* regression protection

**Core invariant**

> Financial truth is computed by deterministic code.

## 35. Phase 5 Boundary

The bounded AI architecture is frozen at:

```text
phase-5-boundary
```

It contains:

* typed AI contracts
* bounded LLM gateway
* preemptive timeout
* output validation
* deterministic fallback
* narration extraction boundary
* candidate lookup boundary
* explanation boundary
* financial invariant tests

**Core invariant**

> AI can propose or explain.
> AI cannot decide financial truth.

## 36. Current Verification Evidence

The Phase 5 local verification sequence completed successfully:

```text
python tests/test_agent_contracts.py
```

All Phase 5 contract tests passed.

```text
python tests/test_agent_guardrails.py
```

All Phase 5 agent guardrail tests passed, including a **REAL enforced timeout**.

```text
python tests/test_agent_invariants.py
```

All Phase 5 invariant tests passed.

Financial facts are provably unchanged by the agent layer.

The demonstration also verified:

* successful AI explanation
* failed AI call
* deterministic fallback
* unchanged decision status and exception code

## 37. Phase 0–4 Final Verification Evidence

The deterministic decision system achieved:

```text
512/512 decision-table combinations resolve deterministically
```

and the integrated decision distribution was:

```text
PARTIAL_MATCH   4
MATCHED         33
HUMAN_REVIEW    14
TAX_MISMATCH    7
AMBIGUOUS       3
```

The real-batch catch-all warning was eliminated after correcting the source-of-truth inconsistency in `_build_context()`.

## 38. What These Failures Changed Architecturally

The most important result of the failure history is not the individual bug fixes.

The architecture evolved from:

```text
working pipeline
```

into:

```text
typed
deterministic
policy-driven
exhaustively tested
benchmarked
auditable
failure-aware
AI-bounded
invariant-protected system
```

The major architectural lessons were:

### Lesson 1

A financial decision should never depend on an implicit branch hidden inside application logic.

Therefore:

```text
formal decision table
```

### Lesson 2

Independent statutory controls must remain independent.

Therefore:

```text
GST and TDS evaluated independently
```

### Lesson 3

A single decision status is not sufficient audit evidence.

Therefore:

```text
complete reason_codes
```

### Lesson 4

An unreachable state should still have defined safety behavior.

Therefore:

```text
catch-all HUMAN_REVIEW rule
```

### Lesson 5

A catch-all that fires in real data is evidence of a real upstream defect.

Therefore:

```text
source-of-truth alignment
```

### Lesson 6

A benchmark without representative methodology can mislead.

Therefore:

```text
guarded fuzzy evaluation
explicit synthetic-data limitation
```

### Lesson 7

A timeout that waits for the call to finish is not a real timeout.

Therefore:

```text
future.result(timeout=...)
```

### Lesson 8

A typed AI contract is stronger than a behavioral promise.

Therefore:

```text
NarrationExtraction
Explanation
```

### Lesson 9

A model's confidence must never be fabricated.

Therefore:

```text
confidence_hint = "unspecified"
```

### Lesson 10

An AI proposal must be verified against deterministic state before it can enter a financial workflow.

Therefore:

```text
LLM proposal
    ↓
typed contract
    ↓
deterministic lookup
    ↓
deterministic verification
```

### Lesson 11

The strongest AI safety property is not "the model usually behaves."

It is:

> "the model has no interface through which it can express financial authority."

## 39. Explicitly Unresolved / Next Evaluation Items

The following are intentionally **NOT** claimed as complete:

**Real LLM provider integration**

Still required.

**Real-model narration extraction evaluation**

Still required.

**Held-out / unseen narration evaluation**

Still required.

**Real provider latency measurement**

Still required.

**Real provider timeout/failure behavior**

Still required.

**Token/cost measurement**

Still required.

**Prompt robustness evaluation**

Still required.

**LLM-assisted candidate integration**

Requires controlled integration and evaluation before merging.

**Production-scale concurrency behavior**

Not yet established.

**Real-world bank narration generalization**

Not yet established.

These are not Phase 0–4 correctness defects.

They are the next evaluation stage of Phase 5.

## 40. Final Engineering Position

Phase 0–4 is considered closed because the deterministic financial core has been subjected to correctness fixes, combinatorial policy testing, integration testing, throughput measurement, fuzzy evaluation, and real-batch regression checks.

Phase 5 is considered architecturally bounded because the AI layer cannot express or directly modify financial authority through its defined contracts and because successful and failed AI execution has been tested against financial invariants.

The current system therefore establishes:

```text
deterministic financial truth

            +

bounded AI assistance

            +

explicit failure handling

            +

typed authority boundaries

            +

audit-oriented evidence
```

The next engineering step is **NOT** another architecture rewrite.

The next step is:

```text
REAL MODEL INTEGRATION
        ↓
REAL MODEL EVALUATION
        ↓
HELD-OUT DATA
        ↓
LATENCY / COST / RELIABILITY
        ↓
FALSE-POSITIVE / RECALL ANALYSIS
        ↓
FINAL PHASE 5 ASSESSMENT
```

No real-model performance claim should be made until those measurements exist.

## 41. Release Milestones

```text
phase-3-final
    ↓
phase-4-final
    ↓
phase-5-boundary
    ↓
REAL LLM INTEGRATION
    ↓
REAL MODEL EVALUATION
    ↓
phase-5-final
```

The tags represent architectural milestones, not arbitrary software-version numbers.

## 42. Closing Statement

The failures recorded here are evidence of engineering maturity rather than evidence that the architecture was poorly designed.

Several of the most valuable findings were discovered precisely because the system was made testable:

```text
512-combination policy sweep
        ↓
exposed missing policy state
        ↓
catch-all safety rule
        ↓
real batch triggered catch-all
        ↓
source-of-truth inconsistency discovered
        ↓
context construction corrected
```

Likewise:

```text
fuzzy evaluation
        ↓
TP = 0
        ↓
gross/net mismatch discovered
        ↓
candidate guard corrected
        ↓
recall recovered
        ↓
precision limitation exposed
        ↓
synthetic-data limitation documented
```

And:

```text
Phase 5 timeout test
        ↓
15-second execution
        ↓
claimed 10-second timeout failed
        ↓
post-hoc timeout identified
        ↓
ThreadPoolExecutor boundary introduced
        ↓
real timeout test passes
```

This is the intended development pattern for a financial-control system:

```text
detect
  ↓
reproduce
  ↓
identify root cause
  ↓
correct architecture
  ↓
add regression protection
  ↓
measure again
  ↓
document remaining uncertainty
```

The system is now ready for the next controlled step:

> **real-model integration and empirical evaluation — without reopening the deterministic financial core.**
