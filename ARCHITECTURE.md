# Architecture

Where financial truth lives, where the model sits, and what it would take
to extend this system to the settlement shape a real payment gateway
produces.

This document answers the five questions a reviewer should ask about any
system that puts a language model near money, and then specifies — in
detail, with file references — the one extension that matters most and was
deliberately **not** built.

---

## The idea, in one sentence

> **Deterministic code owns financial truth. The model retrieves,
> explains, and investigates. It never becomes the source.**

The enforcement is **structural, not behavioural**. The usual version of
this claim is *"we instructed the model not to invent numbers"*, which
fails silently the first time it does. Here, the contracts that cross the
AI boundary have no field for an amount, a status, a tax value, or an
exception code — so a financial fact is not something the model can
express, correctly or otherwise.

---

## 1. Where is the financial truth?

```
src/financial.py            settlement arithmetic — ONE definition
src/tax/validator.py        GST and TDS, verified independently
src/tax/seller_ledger.py    per-record YTD opening balance
src/matching/               candidate generation, scoring, selection
src/exceptions/manager.py   DecisionContext construction
src/exceptions/decision_table.py   the policy, as data
```

Every number in every answer originates from `decide_batch()`
([manager.py:518](src/exceptions/manager.py)). The model reads that
output. It never produces it.

**The settlement equation lives in exactly one place:**

```python
# src/financial.py
def settlement_expected_net(pg_record: NormalizedRecord) -> Decimal:
    return (
        pg_record.amount
        - _or_zero(pg_record.fee)
        - _or_zero(pg_record.gst)
        - _or_zero(pg_record.tds)
    )
```

It previously existed as four independent inline copies across
`candidates.py`, `engine.py`, `scoring.py` and `manager.py`. They agreed,
so no test could observe the problem — see `FAILURE_LOG.md` §52.
`tests/test_financial_invariants.py` now enforces both that every consumer
agrees over the real batch, and that no module re-derives the expression
inline.

**Policy is data, not control flow.** `DECISION_TABLE` is a
priority-ordered list of rules. All 2¹¹ = 2048 combinations of the
`DecisionContext` boolean space are swept, and every one resolves to
exactly one rule.

---

## 2. Where is the AI?

Three places, all outside the boundary.

| Use | File | What the model sees | What it may emit |
|---|---|---|---|
| **Tool selection** | `agent/tool_selection.py` | The question + the tool catalogue. **Not the data** | A tool name and an argument dict |
| **Phrasing** | `agent/controller.py` | The real tool output | Prose |
| **Explanation** | `agent/explainer.py` | A finished `MatchDecision` | Prose |

It is **not** in matching, scoring, tax verification, or decisioning.

**The contracts that cross the boundary:**

```python
# src/agent/tool_selection.py
@dataclass(frozen=True)
class ToolSelection:
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
```

```python
# src/agent/contracts.py
@dataclass(frozen=True)
class Explanation:
    text: str
    grounded_evidence_keys: tuple[str, ...] = field(default_factory=tuple)
    source: Literal["llm", "deterministic_fallback"] = "llm"
```

There is no field anywhere in these types for an amount, a status, a tax
value, or an exception code. `test_contracts_have_no_financial_authority_fields`
greps the field names to keep it that way.

**The agent loop:**

```
question
   │
   ├─1─ SELECTION   [MODEL]  sees tools, not data
   │                         validate_selection() runs inside
   │                         call_llm_bounded — an invalid selection
   │                         never reaches dispatch
   │
   ├─2─ dispatch()   [NO MODEL]  validate_arguments → raise on unknown,
   │                             missing, or out-of-set values.
   │                             Nothing coerced. Nothing dropped.
   │
   └─3─ PHRASING    [MODEL]  sees the real result, instructed to use
                             only the numbers given
   ↓
AgentAnswer(answer=prose, data=the actual numbers)
```

`AgentAnswer` carries **both** the prose and the raw tool output, so the
phrasing is always checkable against the numbers it describes.

---

## 3. What happens when the AI is wrong?

| Failure mode | Behaviour |
|---|---|
| Picks the wrong tool | A true answer to the wrong question — annoying, and **visible** |
| Invents a tool name | `dispatch()` returns `ok=False`. Never defaulted to a "most likely" tool |
| Invents an argument | Rejected. Never dropped — dropping `{"limit": 5}` would return *all* exceptions and let the phrasing layer describe them as five |
| Supplies a bad value | Rejected against `allowed_values`. Never coerced |
| Hallucinates a `txn_id` | `TxnNotFoundError` → an honest "no record of that". Never a fabricated record |
| Returns malformed output | `parse_selection` raises; `call_llm_bounded` contains it |
| Writes wrong prose about right data | `AgentAnswer.data` is attached, and the faithfulness evaluation measures it |
| Fails entirely | Deterministic fallback renders the real numbers |

**The designed-for asymmetry:** a wrong tool gives a *true answer to the
wrong question*, which the operator can see. The failure mode this
architecture exists to prevent is a *false answer to the right question*,
which they cannot. Confining the model to selection and phrasing means the
worst it can do is misroute.

**The invariant, verified after every answer:**

```python
answer.data == getattr(context, answer.tool_used)(**answer.tool_arguments)
```

A stubbed model returning *"All 9999 records matched perfectly"* still
produces `data == context.get_match_rate()`
(`test_a_lying_model_cannot_corrupt_the_data`).

---

## 4. How do we know the numbers are correct?

**Measured accuracy: 55/61 (90.16%)** against `data/ground_truth.json`,
which is generated alongside the data and never read by the pipeline.

Every published number has a test asserting its arithmetic reconciles:

```
accuracy report   evaluated + rejected == full ground-truth set
                  → a record cannot silently vanish

gold baseline     raw == divergent + not_evaluable + known_policy
                  → 12 == 0 + 6 + 6
                  → the two exclusion buckets must stay disjoint
                  → every policy exclusion carries a written rationale

cash position     the four buckets sum to total_expected_settlement
                  → exact Decimal equality, not a tolerance
```

**What this does and does not establish.** Ground truth and the engine
both derive from the decision table, so 90.16% measures *spec-conformance
across ten adversarial categories* — not generalisation to real bank data.
That limitation is stated in `README.md` rather than left for a reader to
infer.

**Deliberately absent: no test asserts a minimum accuracy.** A test that
failed when accuracy dropped would create pressure to adjust ground truth
until it passed — the exact failure recorded twice in `FAILURE_LOG.md`
(§14, §15).

---

## 5. What happens when the provider fails?

Every model call returns an `AgentCallResult`, never an exception.

```python
# src/agent/guardrails.py — the ONLY sanctioned LLM call path
future = _executor.submit(call_fn)
try:
    raw = future.result(timeout=AGENT_CALL_TIMEOUT_SECONDS)
except concurrent.futures.TimeoutError:
    return AgentCallResult(succeeded=False, ...)
```

The timeout is **preemptive**, not measured after the fact. A regression
test uses a function that sleeps 15 seconds and requires return within 12
(`test_real_timeout_returns_before_slow_call_completes`).

**Honest limitation:** Python cannot forcibly kill a running thread. The
guarantee is *"the pipeline does not wait"*, not *"the provider call is
terminated."*

**Every call site has a deterministic fallback:**

| Failure | Fallback |
|---|---|
| Explanation fails | `fallback_template_explanation()` — returns the **same** `Explanation` type, so callers never branch |
| Tool selection fails | An honest "I could not determine which tool answers that" |
| Phrasing fails | `_render_fallback()` renders the real numbers directly |

The last one matters most: by the time phrasing runs, the numbers already
exist. A cosmetic failure must not discard a correct answer.

`test_decision_facts_unchanged_after_failed_llm_explanation` deep-copies a
`MatchDecision`, runs the agent over it with a failing model, and asserts
every financial field is byte-identical.

---

# N:1 Batched Settlement — the design

**This is the largest gap in the system, and it is not built.** What
follows is the specification of what building it would require, why it was
not attempted before the submission deadline, and — most importantly —
which layers it would and would not touch.

## What this system currently models

```
one PG transaction  ──→  one bank credit
```

`build_clean_transaction()` emits exactly one PG row, one bank row, one
invoice. `settlement_id` is written as `f"SET_{txn_id}"` — a 1:1 mapping
by construction.

## What actually happens at a payment gateway

```
    capture_1  ┐
    capture_2  │
    capture_3  ├──→  SETTLEMENT BATCH  ──→  ONE bank credit, ONE UTR
    ...        │      minus refunds
    capture_N  ┘      minus chargebacks
                      minus adjustments
                      minus fees, GST, TDS
```

*N* captures net into **one** T+1/T+2 transfer. The bank statement shows a
single line. Reconciliation means **decomposing that one credit back into
its constituent transactions** — and explaining any residual.

That decomposition is the hard part of real reconciliation, and this
system never has to do it.

## Why the current design makes this tractable

The layering was chosen so that this change lands in **one** layer.

### What changes

**1. The reconciliation anchor moves from `txn_id` to `settlement_id`.**

`settlement_id` already exists on the source contract:

```python
# src/models.py
class PGSettlementRecord(BaseModel):
    settlement_id: str          # ← already here
    txn_id: str
```

But it is **discarded at the normalization boundary** —
`NormalizedRecord` has no `settlement_id` field. Adding it is the first
concrete step, and it is additive: every existing consumer keys off
`txn_id` and would be unaffected.

**2. `expected_net` becomes a sum over line items.**

```python
# today — one record
def settlement_expected_net(pg_record) -> Decimal:
    return record.amount - fee - gst - tds

# N:1 — one batch
def batch_expected_net(pg_records, adjustments) -> Decimal:
    return (
        sum(settlement_expected_net(r) for r in pg_records)
        - sum(a.amount for a in adjustments)     # refunds, chargebacks
    )
```

**This is precisely why Stage 1 exists.** Before `src/financial.py`, that
expression lived as four independent inline copies in `candidates.py`,
`engine.py`, `scoring.py` and `manager.py`. Adding a refund term would
have required four correct edits, and a partial edit would have left
candidate ranking, confidence scoring and the `AMOUNT_MISMATCH` control
each reconciling against a **different definition of the same
settlement** — silently. See `FAILURE_LOG.md` §52.

Today it is one function with a structural test preventing it from forking
again.

**3. Candidate generation anchors on batches.**

`find_bank_candidates()` currently searches for a bank row matching one
PG record's expected net. Under N:1 it searches for a bank row matching a
**batch total**, and the tier structure survives intact:

```
tier 1   exact UTR on the settlement           (unchanged in spirit)
tier 2   exact settlement_id                   (replaces exact txn_id)
tier 3   guarded fuzzy on batch total + date   (unchanged in spirit)
```

The critical property is preserved: **amount and date remain gates, not
signals.**

**4. New decision-context facts.**

```python
partial_decomposition   the credit matches the batch total, but one or
                        more line items cannot be attributed
orphan_line_item        a capture claims membership in a batch that has
                        no bank credit
unexplained_residual    the credit and the batch total differ by an
                        amount no adjustment accounts for
```

Each needs a `DecisionRule` with an explicit priority. The exhaustive
sweep grows from 2¹¹ to 2¹⁴ = 16,384 combinations —
`test_context_dimensions_match_the_swept_space` will fail loudly until
both the sweep and the published coverage figure are updated, which is
exactly what that test is for.

### What does NOT change

This is the part worth emphasising.

| Layer | Impact |
|---|---|
| `src/models.py` — the Decimal firewall | **None** |
| `src/config.py` — rates and thresholds | **None** (one tolerance semantic changes; see below) |
| `src/tax/` — GST and TDS verification | **None.** Tax is per-invoice and stays per-invoice |
| `src/exceptions/decision_table.py` — the mechanism | **None.** New rows, same machinery |
| `src/agent/**` — the entire AI boundary | **None** |
| `agent/tools/` — the read-only tool layer | Return shapes gain batch fields; the read-only property is untouched |

> A layered architecture should absorb a change like this in the layer
> that owns the concept. Matching owns "which records belong together", so
> matching is where N:1 lands. If tax verification or the AI boundary
> needed to change, the layering would be wrong.

## The trap: paise-level netting

**This is the part most implementations get wrong, and it is why the
tolerance model has to change rather than just the matching.**

```
AT 1:1
    |bank_credit − expected_net| ≤ ₹0.01
    Sensible. One comparison, one rounding step, one paise of slack.

AT N:1 WITH PER-LINE TOLERANCE
    500 line items × ₹0.01 = ₹5.00 of "legitimate" drift
    A ₹5 discrepancy is now indistinguishable from rounding.
    The control silently stops working as the batch grows.
```

**The fix is not a bigger tolerance.** It is that batch reconciliation
must be **exact-sum**, with tolerance applied **once**, at the batch
level:

```python
# WRONG — tolerance per line, accumulating
all(abs(line.expected - line.actual) <= AMOUNT_TOLERANCE for line in batch)

# RIGHT — exact sum, tolerance once
abs(batch_credit - sum(line.expected for line in batch)) <= AMOUNT_TOLERANCE
```

Line items are already quantised by `money()` at generation, so the sum is
exact. The single ₹0.01 of slack covers the one place rounding can
legitimately occur: the final comparison against what the bank actually
moved.

**A prerequisite this exposed.** The tolerance is currently defined in two
places:

```python
# src/config.py:85
AMOUNT_TOLERANCE = Decimal("0.01")

# src/exceptions/manager.py:61
_MONEY_TOLERANCE = Decimal("0.01")      # ← a second definition
```

`config.py`'s own module docstring states the rule this violates:

> *"every rate, threshold, weight, or tolerance used anywhere in this
> codebase MUST be imported from this file. No module outside config.py
> may hardcode a financial constant."*

The two values agree today, so nothing is wrong with any current output —
the same shape as §52. But moving tolerance to batch level would require
changing it in two places, with one of them invisible to anyone reading
`config.py`. **Unifying this is a prerequisite, not an optional cleanup.**

## Prerequisites, in order

```
1. Unify _MONEY_TOLERANCE into config.AMOUNT_TOLERANCE
       one definition before its semantics change

2. Carry settlement_id through NormalizedRecord
       additive; existing consumers key off txn_id and are unaffected

3. Introduce batch_expected_net() in src/financial.py
       alongside settlement_expected_net(), not replacing it

4. Move the amount control to exact-sum with batch-level tolerance
       the paise-netting fix above

5. Generator: emit N:1 batches with refunds and chargebacks
       and a generation-time check that batch totals actually reconcile
       — the same discipline that caught the ambiguity fail-open

6. New DecisionRules + extend the sweep to 2^14
```

## Why it was not built before 5 September

Steps 1–6 change candidate generation, the decision context, ambiguity
semantics, the generator, and every evaluation artifact **simultaneously**.
The frozen E2E baseline, the accuracy report, the fuzzy-tier sweep and the
cash position would all need rebuilding, and each has its own integrity
tests.

That is a multi-week change, and a half-built N:1 is strictly worse than
an honest 1:1: it would produce numbers that look like batch
reconciliation and are not.

**The current model is documented as a limitation in `README.md` rather
than presented as a design choice.** One PG transaction to one bank
credit. Real settlements are batched, and the hard part of real
reconciliation is decomposing that — this system never has to.

---

# Other extension points

## Method-aware MDR

`payment_method` is written on every PG record and **never read**. Fee is
a flat 2% of gross.

Real MDR is method-dependent: UPI P2M is largely zero-rated, RuPay debit
is capped, credit cards run ~2%, international ~3%, netbanking is often a
flat per-transaction fee.

```python
MDR_BY_METHOD = {
    "UPI":        Decimal("0.0000"),
    "CARD":       Decimal("0.0200"),
    "NETBANKING": Decimal("0.0180"),
}
```

This is a **generator plus config** change. `verify_gst()` already
computes GST from whatever fee is on the record, so the tax layer needs no
edit. It would also make GST verification meaningfully harder — three fee
bases instead of one, including a legitimate ₹0 fee implying ₹0 GST.

## Ledger-backed YTD

`merchant_ytd_gross_opening` is currently a generated field on each PG
record. In production it comes from a merchant ledger.

The design already anticipates this: `seller_ledger.py` reads the opening
balance **per record** and deliberately does not reconstruct a running
total by sorting the batch. An earlier version did, and produced wrong
answers — the generator's day cursor cycles across categories, so
transaction dates do not reflect true sequence.

> A running balance should come from a ledger, never be re-derived from
> whatever ordering a batch happens to have.

## Idempotency

Reconciliation runs daily. Running it twice must not double-count.

Nothing in the current system persists state between runs —
`BatchQueryContext` re-derives everything from disk. Production would need
idempotency keys per settlement and a durable exception store, so that a
human's resolution survives the next run.

## LLM-assisted candidate matching

`find_bank_candidates_with_llm_assist()` exists in `candidates.py` and is
deliberately **off the live path**.

Three designs were considered and rejected, each for a concrete reason —
the `TXN_` token no longer exists in narration (Upgrade B removed it
deliberately), a reference-free bank row contains no recoverable signal to
extract, and with zero accidental net collisions the candidate shortlist
has length one. Full reasoning in `FAILURE_LOG.md` §50.

> The model's contribution to financial outcomes is zero by design and by
> measurement, not by omission.

Connecting it would mean adding a model to a path that does not need one.

---

## Summary

| Question | Answer |
|---|---|
| Where is financial truth? | `src/financial.py`, `src/tax/`, `src/matching/`, `src/exceptions/` — via `decide_batch()` |
| Where is the AI? | Tool selection, phrasing, explanation. Nowhere else |
| What if the AI is wrong? | Rejected, or visible, or falls back deterministically. Never a fabricated number |
| How do we know the numbers? | 55/61 vs independent ground truth, with every total asserted to reconcile |
| What if the provider fails? | Preemptive timeout, deterministic fallback, decision byte-identical |
| Biggest gap? | N:1 batched settlement — specified above, deliberately not built |
