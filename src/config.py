# src/config.py
"""
Central configuration for all financial rules, scoring parameters, and
tolerances used by the reconciliation engine.

RULE: every rate, threshold, weight, or tolerance used anywhere in this
codebase MUST be imported from this file. No module outside config.py
may hardcode a financial constant. This is what makes the system
auditable — one file a reviewer (or a future you) can check against
statute, instead of hunting through business logic for magic numbers.
"""

from decimal import Decimal, ROUND_HALF_UP

# =======================================================================
# TAX RULES
# Verified against the Income Tax Act 2025 / current GST framework,
# applicable FY 2026-27. Do not "round" these for convenience — a wrong
# digit here invalidates every downstream tax-verification claim.
# =======================================================================

# GST is levied on the Payment Gateway's own fee income (the MDR charged
# to the merchant) — NOT on the merchant's underlying sale amount.
# 18% splits as 9% CGST + 9% SGST for intra-state, or 18% IGST for
# inter-state; we validate the total rate, since the split doesn't
# change whether the tax amount itself is correct.
GST_RATE_ON_FEE = Decimal("0.18")

# TDS under Section 393 (formerly Section 194-O). E-commerce operators /
# payment aggregators must withhold this on the GROSS transaction value
# paid to a resident seller — not on the fee, and not on the net payout.
# The rate was reduced from a legacy 1% to the current 0.1% — this is a
# recent enough change that getting it wrong is a real, plausible
# mistake, which is exactly why it's isolated here instead of being
# re-typed anywhere else in the codebase.
TDS_RATE_SECTION_393 = Decimal("0.001")

# TDS applies only once a seller's CUMULATIVE ANNUAL gross sales through
# the platform exceed this figure. Below it, withholding ₹0 is the
# CORRECT behavior, not a missed deduction — the matching/tax engine
# must be able to tell these two cases apart.
TDS_ANNUAL_THRESHOLD = Decimal("500000")  # ₹5,00,000 (five lakh)

# =======================================================================
# MATCHING SCORE WEIGHTS
# Explicit per-source constants rather than a shared dict split at
# runtime (e.g. SCORE_TXN_ID_EXACT // 2) -- self-documenting, and a
# future maintainer never has to mentally reconstruct why 40 became
# 20. Sums to 100 when bank + invoice are both present and every
# signal matches; a subset still scores meaningfully when only one
# secondary source is present.
# =======================================================================

SCORE_TXN_ID_BANK = 20        # exact txn_id match, PG <-> bank
SCORE_TXN_ID_INVOICE = 20     # exact txn_id match, PG <-> invoice
SCORE_AMOUNT_BANK = 15        # PG's derived net amount <-> bank credited amount
SCORE_AMOUNT_INVOICE = 15     # PG's fee+GST <-> invoice amount
SCORE_DATE_PROXIMITY = 15     # awarded proportionally by day-delta,
                               # satisfied once by whichever secondary
                               # source is present
SCORE_UTR_EXACT = 10          # bank-only signal -- invoices carry no UTR
SCORE_FEE_EXACT = 5           # PG fee <-> invoice fee consistency

# =======================================================================
# CONFIDENCE THRESHOLDS
# Deliberately conservative: it is safer for a genuine match to land in
# MEDIUM or get a second look than for a false match to be auto-approved
# as HIGH. This asymmetry is a design choice, not an oversight — see
# "Trust Architecture" reasoning: a false "matched" is a worse outcome
# than an honest "needs review."
# =======================================================================

CONFIDENCE_HIGH_THRESHOLD = 95     # >= 95: auto-eligible for match
CONFIDENCE_MEDIUM_THRESHOLD = 85   # 85-94: eligible only if txn_id was exact
CONFIDENCE_LOW_THRESHOLD = 70      # 70-84: too weak -> AMBIGUOUS
                                    # < 70: no viable candidate -> UNMATCHED

# =======================================================================
# TOLERANCES
# Real-world settlement data is never bit-exact even when correct —
# these tolerances distinguish genuine discrepancies from rounding
# noise or normal settlement timing lag.
# =======================================================================

AMOUNT_TOLERANCE = Decimal("0.01")   # ₹0.01 — rounding slack for money comparisons
TAX_TOLERANCE = Decimal("0.01")      # ₹0.01 — rounding slack for tax math
DATE_TOLERANCE_DAYS = 3              # T+1 to T+3 is normal settlement lag,
                                      # not a timing error to flag.
                                      # NOTE: this name (not
                                      # SETTLEMENT_WINDOW_DAYS) is what
                                      # matching/candidates.py and
                                      # matching/scoring.py actually
                                      # import -- keep this name stable.

# =======================================================================
# FUZZY MATCH GUARDRAILS
# Fuzzy string similarity is never trusted alone — a similarity score
# only breaks a tie between candidates that already agree on amount
# and date. This prevents "TXN-123" vs "TXN-1234" false positives.
# =======================================================================

FUZZY_MIN_SIMILARITY = 85            # rapidfuzz score (0-100) floor
FUZZY_REQUIRES_AMOUNT_MATCH = True
FUZZY_REQUIRES_DATE_WINDOW = True

# =======================================================================
# ROUNDING
# ROUND_HALF_UP mirrors standard accounting rounding convention (not
# banker's rounding) — this matches how Indian tax computations are
# conventionally rounded, and keeps every money() call consistent.
# =======================================================================

MONEY_QUANTIZE = Decimal("0.01")
ROUNDING_MODE = ROUND_HALF_UP


def money(value: Decimal) -> Decimal:
    """
    Single shared rounding entry point for every monetary calculation
    in the codebase. Never call .quantize() directly elsewhere — route
    every money value through this function so rounding behavior is
    guaranteed identical everywhere (tax validator, matcher, reporter).
    """
    return value.quantize(MONEY_QUANTIZE, rounding=ROUNDING_MODE)


# =======================================================================
# SYNTHETIC DATA GENERATION
# =======================================================================

RANDOM_SEED = 12345  # fixed for reproducibility -- log this value in
                      # every generated report so a reviewer can re-run
                      # and get an identical dataset.

BATCH_DISTRIBUTION = {
    "exact_match": 18,
    "timing_difference": 6,
    "reference_mismatch_fuzzy": 6,
    "amount_fee_discrepancy": 8,
    "tax_mismatch": 7,
    "missing_in_source": 5,
    "duplicate": 3,
    "ambiguous": 3,
    "corrupted": 2,
    "unresolvable": 2,
}
TOTAL_RECORDS = sum(BATCH_DISTRIBUTION.values())  # = 60