
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
