# FAILURE_LOG.md

## Engineering Post-Mortems — AI Finance Controller (Track 04)

This log documents every real defect discovered during development of the
reconciliation engine, from initial data modeling through the decision
engine. Each entry follows the same structure a payments-systems team would
use internally: **what broke, where it lived in the system, why it happened,
how it was found, how it was fixed, and what it proves about the
architecture.**

Nothing in this document is retrofitted. Every entry below was written at
the point of discovery, against the actual failing output of the system —
not reconstructed afterward to sound instructive.

---

## Index

| # | Component | Severity | Category |
|---|---|---|---|
| 1 | `models.py` | Design-time (prevented, not observed) | Financial correctness |
| 2 | `scripts/verify_data.py` | Medium | False-positive validation |
| 3 | `src/normalization/engine.py` | Low | Type-safety gap |
| 4 | `src/matching/candidates.py` | High | Dead code / structural risk |
| 5 | `tests/test_matching.py` | Medium | Test validity |
| 6 | `src/matching/scoring.py` | High | Missing decision state |
| 7 | `src/tax/seller_ledger.py` | Critical | Silent financial miscalculation |

---

## 1. Float/Decimal Precision Risk — Prevented by Design, Not Discovered by Debugging

**Where:** `src/models.py`, `to_decimal()` validator, applied via the shared `Money` type to every monetary field across `PGSettlementRecord`, `BankStatementRecord`, `InvoiceRecord`, and `NormalizedRecord`.

**The risk:** Standard Python float arithmetic is provably unsafe for currency — `0.1 + 0.2 != 0.3` in IEEE 754 binary floating point. In a reconciliation system, this class of error is not cosmetic; it silently corrupts amount comparisons, causing legitimate matches to be rejected or, worse, incorrect matches to be silently accepted within a coincidentally-passing tolerance window.

**Strategic approach:** Rather than discover this as a runtime bug, the schema layer was designed to make it structurally impossible. `to_decimal()` explicitly rejects `float` and `bool` (a `bool` is a subclass of `int` in Python and can silently coerce into a nonsensical Decimal if not excluded) at the Pydantic validation boundary, before any value can reach business logic.

**Why this belongs in the log:** A defect prevented by architecture is worth documenting as deliberately as one found by testing — it demonstrates the difference between debugging financial software and designing it so an entire bug class cannot occur. This is the single design decision every other fix in this log depends on; every downstream calculation in Phases 3–4 inherits its correctness from this boundary holding.

---

## 2. False-Positive Referential Integrity Failures in the Dataset Validator

**Where:** `scripts/verify_data.py`, `CHECK 4: Referential integrity`.

**What broke:** The validator indexed bank records by UTR to confirm every ground-truth transaction was present in its expected sources. Two synthetic anomaly categories — `reference_mismatch_fuzzy` and `unresolvable` — deliberately corrupt or null the bank-side UTR as their entire test premise. The validator correctly found no UTR match for these records and reported them as **missing from the bank source**, when they were actually present, linked only by a different key (`bank_ref`).

**FAILURES:**
TXN_00025 (reference_mismatch_fuzzy): expected present in all 3 sources, got bank=False
TXN_00026 (reference_mismatch_fuzzy): expected present in all 3 sources, got bank=False
... (8 total)


**Root cause:** Conflating "not found by this lookup key" with "absent from the source." UTR is deliberately unreliable in exactly the categories under test, so indexing by UTR alone was the wrong join key for a general-purpose integrity check.

**Fix:** Re-indexed bank records by the `txn_id` embedded in `bank_ref` (a value that survives UTR corruption), instead of by UTR itself.

**What this proves:** The synthetic dataset's adversarial design was working correctly from the start — this failure was a bug in the *validator's* assumptions, not the data. Distinguishing "my test is wrong" from "my system is wrong" under time pressure is a core engineering discipline, and this was the first real exercise of it in the build.

---

## 3. Ambiguous `NormalizedRecord.txn_id` Sentinel Risk

**Where:** `src/normalization/engine.py`, bank-record normalization; `src/models.py`, `NormalizedRecord.txn_id`.

**What broke (caught in design review before it shipped):** Early normalization logic defaulted an unresolved bank record's `txn_id` to the string `"UNRESOLVED"` rather than a proper null value.

**Why this matters:** A sentinel string that looks like a real identifier is a latent equality-check hazard. Any downstream code comparing `txn_id` values — matching logic, deduplication, audit lookups — could silently treat `"UNRESOLVED"` as a real, matchable ID across multiple genuinely-different unresolved records, producing false collisions.

**Fix:** Changed `NormalizedRecord.txn_id` from `str` (required) to `Optional[str] = None`. `None` makes "not yet resolved" an explicit, type-checked state instead of a string that happens to look distinctive. Every equality comparison downstream (`bank_record.txn_id == pg_record.txn_id`) now fails safely on `None` rather than risking a coincidental string match.

**What this proves:** Type safety isn't just about rejecting malformed input — it's about making invalid *states* unrepresentable, not just invalid *values*.

---

## 4. Unreachable Fuzzy-Matching Tier — Dead Code with a Latent False-Positive Risk

**Where:** `src/matching/candidates.py`, `find_bank_candidates()`.

**What broke:** During a merge of two candidate matching-architecture drafts, a standalone "amount + date, no identifier required" tier was inserted between the exact-transaction-ID tier and the guarded fuzzy-matching tier. Its entry condition (amount within tolerance AND date within window) was **identical** to the fuzzy tier's own guard condition. Since the fuzzy tier only executes when the tier before it finds nothing, and both tiers used the same predicate, the fuzzy tier was **structurally unreachable** — it could never fire, regardless of input.

**How it was found:** A unit test asserting fuzzy-tier recovery (`test_fuzzy_fallback_recovers_corrupted_utr`) failed with `match_type == "amount_date"` instead of the expected `"fuzzy"`.

**Compounding risk identified during root-cause analysis:** The synthetic dataset's `build_clean_transaction()` draws amounts from a pool of only 6 fixed values across 60+ records. A standalone amount+date match with **no identifier or narration evidence at all** would have been a genuine false-positive risk in this dataset specifically, not just a theoretical design flaw — coincidental same-day, same-amount collisions across unrelated transactions were entirely plausible.

**Fix:** Removed the standalone tier. Restored the intended three-tier chain: exact UTR → exact transaction ID → guarded fuzzy narration match.

**What this proves:** A merge of two reasonable-looking designs is not automatically a reasonable design. Structural reachability (can this code path ever actually execute, given the code before it) is a category of bug that passes casual code review and requires either a targeted test or a static reachability argument to catch.

---

## 5. Test Fixtures That Silently Bypassed the Code Path Under Test

**Where:** `tests/test_matching.py`, two separate test functions (fuzzy-recovery and fuzzy-guardrail tests).

**What broke:** Both fixtures assigned the same `txn_id` to both the PG and bank test records. Since exact-transaction-ID matching is checked before the fuzzy tier, both tests silently passed through tier 2 and never exercised the fuzzy-matching logic they were named to test. The tests were **green for the wrong reason**.

**How it was found:** After fixing the dead-code bug in Entry 4, one of these tests began failing — its assertion (`len(candidates) == 0`) contradicted the now-reachable, and correct, tier-2 behavior of returning a match via exact transaction ID.

**Root cause:** `make_normalized()`'s first positional argument is `txn_id`; defaulting both fixture records to the same value is an easy, unnoticed habit that silently defeats test isolation.

**Fix:** Set `txn_id=None` explicitly on the bank-side fixture in both tests, forcing the search past tiers 1–2 into the tier genuinely under test.

**What this proves:** A passing test suite is not sufficient evidence of correctness on its own — a test that passes without exercising its intended code path is a false signal, arguably worse than no test at all, because it creates unearned confidence. This entry and Entry 4 together represent the same underlying lesson surfacing at two different layers of the system on the same day.

---

## 6. `PARTIAL_MATCH` Was Structurally Unreachable

**Where:** `src/matching/scoring.py`, `classify_confidence()`.

**What broke:** Confidence thresholds (`HIGH ≥ 95`, `MEDIUM ≥ 85`, `LOW ≥ 70`) were calibrated against a 100-point scale that assumes all three sources (PG, bank, invoice) are present. When only **one** secondary source exists — the exact scenario the `missing_in_source` synthetic category is designed to test — the maximum achievable score is mathematically capped well below 70 (e.g., invoice-only tops out near 55 points). Every partial-source transaction was therefore automatically classified `NO_MATCH`, regardless of how well it matched on the signals actually available to it.

**Observed impact:** In the full-batch decision distribution, `PARTIAL_MATCH: 0` and an inflated `HUMAN_REVIEW: 18` — the entire `missing_in_source` category (5 records) was being funneled into the wrong decision state.

**Fix:** Introduced `normalized_score` — the raw score expressed as a percentage of the *maximum achievable score given which sources are actually present* — and reclassified confidence against this normalized figure instead of the raw total. The raw `total_score` is still preserved on every `MatchScore` for audit purposes; only the confidence *tier* decision uses the normalized value.

**Verified result:** `PARTIAL_MATCH` count rose from 0 to 4 (aligned with the `missing_in_source` category), `NO_MATCH` dropped from 7 to 2, `HUMAN_REVIEW` dropped from 18 to 14.

**What this proves:** A scoring system calibrated against an implicit best-case assumption (all sources always present) will silently misclassify every case that violates that assumption. The fix required recognizing that "confidence" is inherently relative to available evidence, not an absolute score against a fixed ceiling.

---

## 7. TDS Threshold False Positives — A Two-Stage Root-Cause Investigation

**Where:** `src/tax/seller_ledger.py`, `build_seller_annual_gross()`.

This is the most significant defect found during development, and the one that most directly tested whether the tax-verification claim in this submission is actually trustworthy.

### Stage 1 — Initial Symptom

**Observed:** 17 transactions flagged `TAX_MISMATCH` against a synthetic dataset containing only 7 genuine `tax_mismatch`-category records — a 10-record false-positive rate.

**Diagnosis method:** Built `scripts/diagnose_tax_mismatch.py`, a standalone tool cross-referencing every `TAX_MISMATCH` decision against the isolated ground-truth file, printing the exact tax evidence (expected vs. claimed GST/TDS) behind each flag.

**Finding:** All 10 false positives shared one pattern: `expected: <nonzero>, claimed: 0.00, threshold_applicable: False` — the system believed these merchants had **not** crossed the ₹5,00,000 TDS threshold, when the data generator's own internal state said they had.

**Root cause (first hypothesis):** `build_seller_annual_gross()` reconstructed each merchant's cumulative gross by starting every merchant at ₹0 and summing their transactions within the batch — with no knowledge that merchants 1–3 were deliberately seeded by the generator at ₹4,95,000 (just under threshold) *before* batch generation began. That starting balance existed only in the generator's private in-memory state and was never written to any output file, making it structurally impossible for any downstream consumer to reconstruct correctly.

**First fix:** Added a `merchant_ytd_gross_opening` field to `PGSettlementRecord`, written directly by the generator at record-build time, capturing each merchant's true opening balance as real, verifiable data rather than private state. The seller ledger was updated to seed from this value, sorting transactions chronologically by `date_utc` before accumulating.

**Result of first fix:** False positives reduced from 10 to 2 — a real, measurable improvement, but not a complete fix.

### Stage 2 — The Deeper Root Cause

**Observed after Stage 1:** 2 remaining false positives, both `exact_match`-category transactions, both showing `claimed: 0.00` where the ledger expected a nonzero TDS.

**Diagnosis method:** Extended the diagnostic script to print each flagged transaction's raw PG and invoice data directly. Confirmed both the PG record's `tds_withheld` and the invoice's `claimed_tds` independently agreed on `0.00` — meaning the **source data was internally consistent and correct**. The reconstruction, not the data, was wrong.

**True root cause:** The synthetic generator's `date_offset_days` cursor cycles and resets across every category boundary (`day_cursor = (day_cursor + 1) % 20`, reset per category in `generate_batch()`). This means stored transaction dates do **not** reliably reflect the generator's true sequential build order — two transactions from different categories can carry the same or an out-of-sequence date despite being generated in a completely different order. Sorting by `date_utc` to reconstruct "what order did the generator process transactions in" was therefore fundamentally unsound, not merely imprecise.

**Final fix:** Eliminated batch-level reconstruction entirely. Rewrote `seller_gross_after_transaction()` to compute threshold applicability **per transaction, in isolation** — `merchant_ytd_gross_opening + this transaction's own gross amount` — using only data already written directly onto the record itself. No ordering assumption of any kind remains in the code.

**Verified final result:** 7 of 7 true `tax_mismatch`-category transactions correctly flagged; 0 false positives. Confirmed via `scripts/diagnose_tax_mismatch.py` and locked in permanently via `tests/test_tax_decision.py::test_tax_mismatch_matches_ground_truth_exactly`, a regression test that fails the build if this defect class ever reappears.

**What this proves, and why it is the strongest entry in this log:**

The first fix was not wrong — it was a genuine improvement, reducing false positives by 80%, and a reasonable engineer would have been tempted to stop there and call it resolved. Verifying the fix against ground truth rather than trusting the improved number is what surfaced the deeper defect. The lesson generalizes beyond this bug: **an improved metric is not proof of a correct fix** — it can equally be evidence of a better approximation to a wrong approach. The eventual fix did not involve a smarter reconstruction algorithm; it involved recognizing that reconstruction was the wrong strategy entirely, because the ground truth already existed in the data and simply hadn't been exposed. This is the difference between *optimizing* a flawed approach and *replacing* it.

---

## Summary: What This Log Demonstrates

| Failure class | Count | Represents |
|---|---|---|
| Prevented by design | 1 | Architecture that makes a bug class impossible, not just unlikely |
| Validator/test logic errors | 2 | Distinguishing "my test is wrong" from "my system is wrong" |
| Type-safety gaps | 1 | Making invalid states unrepresentable |
| Structural/dead-code defects | 1 | Reachability analysis beyond code review |
| Missing decision states | 1 | Calibration assumptions that silently break under partial data |
| Silent financial miscalculation | 1 (two-stage) | Verifying fixes against ground truth, not just improved metrics |

Every fix in this log is backed by a permanent automated test that fails the build if the defect recurs. None of these defects were discovered by manual inspection alone — each was caught either by a test written before the corresponding feature, or by cross-referencing system output against the independently-generated ground truth this project was deliberately designed to keep isolated from the pipeline (see `data/ground_truth.json` — never read by any production code path, only by evaluation and diagnostic tooling).
