
## Bug: False-positive referential integrity failures in verify_data.py

**What broke:** The dataset validator indexed bank records by UTR to
check cross-source referential integrity. Two synthetic categories
(reference_mismatch_fuzzy, unresolvable) deliberately corrupt or null
the bank-side UTR as their entire test premise. The validator
correctly found no UTR match for these records and reported them as
missing -- when in fact they were present, just linked by a different
signal (bank_ref).

**Root cause:** Conflating "not found by this lookup key" with
"absent from the source." UTR is deliberately unreliable in exactly
the categories being tested, so indexing by UTR alone was the wrong
join key for a general-purpose integrity check.

**Fix:** Re-indexed bank records by the txn_id embedded in bank_ref
instead, which remains stable even when UTR is corrupted or missing.

## Bug: NormalizedRecord.txn_id sentinel string risk

**What broke (caught before it shipped, not in production):** Early
normalization code defaulted an unresolved bank record's txn_id to
the string "UNRESOLVED" rather than a proper null value.

**Root cause:** A string default that looks like a real identifier is
a latent equality-check hazard -- any downstream code comparing
txn_id values could silently treat "UNRESOLVED" as a real, matchable
ID rather than recognizing it as unknown.

**Fix:** Changed NormalizedRecord.txn_id to Optional[str] = None,
making "not yet resolved" an explicit, type-checked state instead of
a string that happens to look distinctive.

## Bug: Unreachable fuzzy-matching tier (dead code)

**What broke:** A standalone "amount + date, no identifier required"
matching tier was added between exact-txn-id and guarded-fuzzy. Its
guard condition (amount within tolerance AND date within window) was
identical to the fuzzy tier's own entry guard. Since the fuzzy tier
only runs when the standalone tier finds nothing, and both use the
same condition, the fuzzy tier could never actually fire -- it was
structurally unreachable.

**Root cause:** Introduced during a refactor to merge two draft
architectures; the standalone tier wasn't part of the original
3-tier design and duplicated logic already covered by the fuzzy
tier's guard.

**Additional risk this exposed:** the synthetic dataset only uses 6
distinct amount values across 60+ records. A standalone amount+date
match with no identifier or narration evidence at all would have
been a real false-positive risk in our own test data, not just a
theoretical concern.

**Fix:** Removed the standalone tier. Restored the intended 3-tier
chain: exact UTR -> exact txn_id -> guarded fuzzy narration match.

## Bug: Test fixtures accidentally bypassing the code path under test

**What broke:** Two matching unit tests (fuzzy-recovery and fuzzy-
guardrail tests) gave the PG and bank fixture records the same
txn_id. Since exact-txn-id is checked before the fuzzy tier, both
tests silently passed through the wrong tier and never actually
exercised fuzzy-matching logic at all.

**Root cause:** make_normalized()'s first positional argument is
txn_id, easy to default to the "same" identifier across both
fixtures out of habit, without considering it would short-circuit
the search.

**Fix:** Set txn_id=None explicitly on the bank fixture in both
tests, forcing the search past tiers 1-2 into the tier actually
being tested.

**Lesson:** when writing an isolated test for a specific matching
tier, deliberately withhold the signals that stronger tiers would
use -- otherwise the test can pass for the wrong reason.
