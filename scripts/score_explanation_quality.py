"""
Phase 5C.4.4b — Semantic / contradiction-aware explanation scoring.

This module evaluates already-generated Gemini explanations.

IMPORTANT
---------
This is an EVALUATION layer.

It is NOT the runtime financial safety validator.

The deterministic explanation validator remains authoritative for
runtime acceptance.

This scorer exists to distinguish:

    - exact-string differences
    - faithful paraphrases
    - missing facts
    - unsupported claims
    - financial contradictions

No Gemini/API calls are made by this module.
The scorer operates only on previously captured model outputs.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


DATASET_PATH = (
    ROOT
    / "data"
    / "eval"
    / "held_out_explanations.json"
)

RUN_OUTPUT_PATH = (
    ROOT
    / "data"
    / "eval"
    / "real_gemini_explanation_run_5C4.json"
)


# =====================================================================
# NORMALIZATION
# =====================================================================

def normalize_text(value: str) -> str:
    """
    Conservative lexical normalization.

    This deliberately does NOT perform unrestricted semantic inference.
    """

    value = value.lower()
    value = value.replace(",", "")
    value = value.replace("%", "")

    value = re.sub(
        r"[^a-z0-9._-]+",
        " ",
        value,
    )

    return " ".join(value.split())


def normalized_number(
    value: str | None,
) -> str | None:
    """
    Normalize numeric values.

    Examples:

        10,000.00 -> 10000
        10000 -> 10000
        94.0 -> 94
    """

    if value is None:
        return None

    text = str(value).strip()
    text = text.replace(",", "")

    try:
        number = float(text)

        if number.is_integer():
            return str(int(number))

        return (
            f"{number:.10f}"
            .rstrip("0")
            .rstrip(".")
        )

    except ValueError:
        return normalize_text(text)


# =====================================================================
# CONTROLLED REASON-CODE ALIASES
# =====================================================================

REASON_CODE_ALIASES: dict[str, tuple[str, ...]] = {
    "ERR_GST_MISMATCH": (
        "gst mismatch",
        "goods and services tax mismatch",
        "gst discrepancy",
        "gst tax mismatch",
        "gst difference",
    ),
    "ERR_TDS_VARIANCE": (
        "tds variance",
        "tax deducted at source variance",
        "tds discrepancy",
        "tds difference",
        "tax deduction variance",
        "tax deduction discrepancy",
    ),
    "ERR_AMOUNT_MISMATCH": (
        "amount mismatch",
        "amount discrepancy",
        "amount difference",
        "claimed amount differs",
        "amount does not match",
        "total amount mismatch",
    ),
    "ERR_MISSING_EVIDENCE": (
        "missing evidence",
        "required evidence missing",
        "reconciliation evidence missing",
        "evidence was not provided",
        "required reconciliation evidence was not provided",
    ),
}


# =====================================================================
# CONTROLLED EVIDENCE ALIASES
# =====================================================================

EVIDENCE_ALIASES: dict[str, tuple[str, ...]] = {
    "transaction reference matched": (
        "transaction reference matched",
        "transaction reference was successfully validated",
        "transaction reference was validated",
        "reference matched",
        "reference was validated",
    ),
    "GST mismatch": (
        "gst mismatch",
        "gst discrepancy",
        "gst tax mismatch",
        "goods and services tax mismatch",
    ),
    "TDS variance": (
        "tds variance",
        "tds discrepancy",
        "tax deduction variance",
        "tax deducted at source variance",
    ),
    "amount mismatch": (
        "amount mismatch",
        "amount discrepancy",
        "amount difference",
        "amount does not match",
    ),
    "required reconciliation evidence missing": (
        "required reconciliation evidence is missing",
        "required reconciliation evidence missing",
        "reconciliation evidence is missing",
        "required evidence is missing",
        "evidence is missing",
    ),
    "all deterministic reconciliation checks passed": (
        "all deterministic reconciliation checks passed",
        "all reconciliation checks passed",
        "all deterministic checks passed",
        "reconciliation checks passed successfully",
        "all checks passed",
    ),
}


# =====================================================================
# RESULT CONTRACTS
# =====================================================================

@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str


@dataclass
class CaseScore:
    case_id: str
    category: str
    explanation: str

    status_preserved: bool

    required_amounts_preserved: bool
    required_tax_preserved: bool

    confidence_preserved: bool

    reason_codes_preserved: bool
    evidence_preserved: bool

    unsupported_claims: list[str] = field(
        default_factory=list
    )

    contradictions: list[str] = field(
        default_factory=list
    )

    missing_material_facts: list[str] = field(
        default_factory=list
    )

    findings: list[Finding] = field(
        default_factory=list
    )

    @property
    def safety_critical_failure(self) -> bool:
        """
        Only contradictions and unsupported financial values are
        safety-critical.

        Missing explanatory detail is a QUALITY issue, not an
        automatic financial-safety violation.
        """

        return bool(
            self.contradictions
            or self.unsupported_claims
        )

    @property
    def semantically_faithful(self) -> bool:
        return not (
            self.safety_critical_failure
            or self.missing_material_facts
        )

    @property
    def score(self) -> float:
        """
        Deterministic quality score.

        This is NOT a probability.

        Weighting:

            status          20
            amounts         20
            tax             20
            confidence      10
            reason codes    15
            evidence        15
        """

        if self.safety_critical_failure:
            return 0.0

        total = 0.0

        total += (
            20.0
            if self.status_preserved
            else 0.0
        )

        total += (
            20.0
            if self.required_amounts_preserved
            else 0.0
        )

        total += (
            20.0
            if self.required_tax_preserved
            else 0.0
        )

        total += (
            10.0
            if self.confidence_preserved
            else 0.0
        )

        total += (
            15.0
            if self.reason_codes_preserved
            else 0.0
        )

        total += (
            15.0
            if self.evidence_preserved
            else 0.0
        )

        return total


# =====================================================================
# DATASET HELPERS
# =====================================================================

def load_json(
    path: Path,
) -> dict[str, Any]:

    with path.open(
        "r",
        encoding="utf-8",
    ) as f:
        return json.load(f)


def build_fact_map(
    dataset: dict[str, Any],
) -> dict[str, dict]:

    return {
        case["case_id"]: case
        for case in dataset["cases"]
    }


# =====================================================================
# BASIC PHRASE MATCHING
# =====================================================================

def contains_phrase(
    explanation: str,
    phrase: str,
) -> bool:

    normalized_explanation = normalize_text(
        explanation
    )

    normalized_phrase = normalize_text(
        phrase
    )

    return normalized_phrase in normalized_explanation


# =====================================================================
# REASON CODE CHECK
# =====================================================================

def reason_code_is_preserved(
    explanation: str,
    reason_code: str,
) -> bool:

    if contains_phrase(
        explanation,
        reason_code,
    ):
        return True

    aliases = REASON_CODE_ALIASES.get(
        reason_code,
        (),
    )

    return any(
        contains_phrase(
            explanation,
            alias,
        )
        for alias in aliases
    )


# =====================================================================
# EVIDENCE CHECK
# =====================================================================

def evidence_is_preserved(
    explanation: str,
    evidence: str,
) -> bool:

    if contains_phrase(
        explanation,
        evidence,
    ):
        return True

    aliases = EVIDENCE_ALIASES.get(
        evidence,
        (),
    )

    return any(
        contains_phrase(
            explanation,
            alias,
        )
        for alias in aliases
    )


# =====================================================================
# FINANCIAL VALUE CHECKS
# =====================================================================

def number_appears(
    explanation: str,
    value: str,
) -> bool:

    normalized = normalize_text(
        explanation
    )

    normalized_value = normalized_number(
        value
    )

    if normalized_value is None:
        return False

    return normalized_value in normalized


def financial_value_is_preserved(
    explanation: str,
    value: str | None,
) -> bool:

    if value is None:
        return True

    return number_appears(
        explanation,
        value,
    )


# =====================================================================
# STATUS CHECKS
# =====================================================================

def status_is_preserved(
    explanation: str,
    expected_status: str,
) -> bool:

    normalized = normalize_text(
        explanation
    )

    status = normalize_text(
        expected_status
    )

    return status in normalized


def detect_status_contradiction(
    explanation: str,
    expected_status: str,
) -> list[str]:

    normalized = normalize_text(
        explanation
    )

    findings: list[str] = []

    if expected_status == "REVIEW":

        forbidden_phrases = (
            "successfully matched",
            "matched successfully",
            "transaction is matched",
            "status is match",
            "status of match",
            "approved",
            "approved successfully",
        )

    elif expected_status == "MATCH":

        forbidden_phrases = (
            "requires review",
            "needs review",
            "flagged for review",
            "status is review",
            "status of review",
            "reconciliation failed",
            "mismatch requires review",
        )

    else:
        forbidden_phrases = ()

    for phrase in forbidden_phrases:

        if normalize_text(phrase) in normalized:

            findings.append(
                (
                    f"Explanation contradicts "
                    f"{expected_status} with phrase "
                    f"'{phrase}'."
                )
            )

    return findings


# =====================================================================
# FINANCIAL NUMBER EXTRACTION
# =====================================================================

def extract_decimal_values(
    text: str,
) -> set[str]:
    """
    Extract decimal-looking numeric values.

    Examples:

        6.00
        9.00
        10000.00
        ₹10,000.00

    This intentionally ignores:

        - integers
        - transaction IDs
        - date-like identifiers

    Context classification is performed separately.
    """

    matches = re.findall(
        r"(?<![A-Za-z0-9_])"
        r"(?:₹\s*)?"
        r"\d[\d,]*\.\d{1,2}"
        r"(?![A-Za-z0-9_])",
        text,
    )

    values: set[str] = set()

    for match in matches:

        value = (
            match
            .replace("₹", "")
            .strip()
        )

        normalized = normalized_number(
            value
        )

        if normalized is not None:
            values.add(normalized)

    return values


# =====================================================================
# NUMERIC CONTEXT CLASSIFICATION
# =====================================================================

def _local_numeric_context(
    text: str,
    match: re.Match[str],
    window: int = 72,
) -> tuple[str, str]:
    """
    Return normalized local text surrounding a numeric occurrence.

    The context is intentionally bounded so unrelated numbers elsewhere
    in an explanation cannot influence classification.
    """

    start = match.start()
    end = match.end()

    before = text[
        max(0, start - window):start
    ].lower()

    after = text[
        end:min(len(text), end + window)
    ].lower()

    return before, after


def _is_confidence_metadata(
    text: str,
    match: re.Match[str],
) -> bool:
    """
    Explicitly recognize confidence-score expressions.

    Confidence is authoritative metadata, not a financial amount.

    Supported examples:

        confidence score of 94.0
        confidence score is 94.0
        confidence score: 94.0
        confidence score = 94.0
        confidence of 94.0
        confidence: 94.0
        confidence is 94.0%
        94.0% confidence

    This check is deliberately narrow and context-based.
    """

    before, after = _local_numeric_context(
        text,
        match,
        window=72,
    )

    # The number is immediately preceded by a confidence expression.
    confidence_before_patterns = (
        r"\bconfidence\s*$",
        r"\bconfidence\s+(?:score|level)\s*$",
        r"\bconfidence\s+(?:score|level)\s+(?:is|of|:|=)\s*$",
        r"\bconfidence\s+(?:is|of|:|=)\s*$",
    )

    for pattern in confidence_before_patterns:
        if re.search(pattern, before):
            return True

    # The confidence word can appear after the number.
    confidence_after_patterns = (
        r"^\s*%?\s*confidence\b",
        r"^\s*%?\s*confidence\s+(?:score|level)\b",
    )

    for pattern in confidence_after_patterns:
        if re.search(pattern, after):
            return True

    # Strong sentence-local fallback.
    # This handles:
    # "The system has assigned this a confidence score of 94.0."
    sentence_start = max(
        text.rfind(".", 0, match.start()),
        text.rfind("!", 0, match.start()),
        text.rfind("?", 0, match.start()),
        text.rfind("\n", 0, match.start()),
    )

    sentence_end_candidates = [
        position
        for position in (
            text.find(".", match.end()),
            text.find("!", match.end()),
            text.find("?", match.end()),
            text.find("\n", match.end()),
        )
        if position != -1
    ]

    sentence_end = (
        min(sentence_end_candidates)
        if sentence_end_candidates
        else len(text)
    )

    sentence = text[
        sentence_start + 1:sentence_end
    ].lower()

    if (
        "confidence score" in sentence
        or re.search(r"\bconfidence\b", sentence)
        and not re.search(
            r"\b(?:amount|tax|gst|tds|payment|charge|fee|balance)\b",
            sentence,
        )
    ):
        # Only classify as metadata if the number is close enough
        # to the confidence term.
        number_position = match.start() - (
            sentence_start + 1
        )

        confidence_positions = [
            m.start()
            for m in re.finditer(
                r"\bconfidence(?:\s+score|\s+level)?\b",
                sentence,
            )
        ]

        if confidence_positions:
            if min(
                abs(
                    number_position - position
                )
                for position in confidence_positions
            ) <= 80:
                return True

    return False


def _is_explicit_percentage(
    text: str,
    match: re.Match[str],
) -> bool:
    """
    Percentage values are metadata unless explicitly converted into
    financial/currency meaning.

    Examples:

        94.0%             -> metadata
        87.0 percent      -> metadata
        amount is 94.0%   -> still percentage metadata
    """

    before, after = _local_numeric_context(
        text,
        match,
        window=24,
    )

    # Number immediately followed by % / percent.
    if re.match(
        r"^\s*(?:%|percent|percentage)\b",
        after,
    ):
        return True

    # Percentage marker immediately before number.
    if re.search(
        r"(?:%|percent|percentage)\s*$",
        before,
    ):
        return True

    return False


def _is_identifier_context(
    text: str,
    match: re.Match[str],
) -> bool:
    """
    Prevent decimal portions of identifiers/references from becoming
    financial claims.
    """

    before, _ = _local_numeric_context(
        text,
        match,
        window=48,
    )

    identifier_patterns = (
        r"\btxn[_\-\s]*$",
        r"\btransaction\s+(?:id|reference|ref)\s*(?:is|:|=)?\s*$",
        r"\binvoice\s+(?:number|no|id)?\s*(?:is|:|=)?\s*$",
        r"\breference\s+(?:number|no|id)?\s*(?:is|:|=)?\s*$",
        r"\bref\s*(?:is|:|=)?\s*$",
        r"\bbatch\s+(?:number|no|id)?\s*(?:is|:|=)?\s*$",
    )

    return any(
        re.search(
            pattern,
            before,
        )
        for pattern in identifier_patterns
    )


def _is_date_context(
    text: str,
    match: re.Match[str],
) -> bool:
    """
    Date-like values are not financial amounts.
    """

    start = match.start()
    end = match.end()

    local = text[
        max(0, start - 20):
        min(len(text), end + 20)
    ]

    return bool(
        re.search(
            r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b",
            local,
        )
    )


def _has_currency_context(
    text: str,
    match: re.Match[str],
) -> bool:
    """
    Currency markers explicitly establish financial meaning.
    """

    before, after = _local_numeric_context(
        text,
        match,
        window=32,
    )

    currency_markers = (
        "₹",
        "inr",
        "rs.",
        "rs ",
        "rupee",
        "rupees",
        "usd",
        "$",
        "dollar",
        "dollars",
        "eur",
        "€",
        "gbp",
        "£",
    )

    return (
        any(
            marker in before[-24:]
            for marker in currency_markers
        )
        or any(
            marker in after[:24]
            for marker in currency_markers
        )
    )


def _has_financial_vocabulary_context(
    text: str,
    match: re.Match[str],
) -> bool:
    """
    Financial vocabulary establishes financial meaning.

    This intentionally remains broad because unsupported monetary
    claims must not escape merely because the exact phrase was not
    anticipated.
    """

    before, after = _local_numeric_context(
        text,
        match,
        window=48,
    )

    financial_terms = (
        "amount",
        "tax",
        "gst",
        "tds",
        "payment",
        "refund",
        "charge",
        "charged",
        "fee",
        "fees",
        "price",
        "value",
        "revenue",
        "cost",
        "balance",
        "debit",
        "credit",
        "deducted",
        "deduction",
        "credited",
        "debited",
        "variance",
        "difference",
        "discrepancy",
        "mismatch",
        "shortfall",
        "surplus",
        "overcharge",
        "undercharge",
        "settlement",
    )

    local_before = before[-48:]
    local_after = after[:48]

    return (
        any(
            re.search(
                rf"\b{re.escape(term)}\b",
                local_before,
            )
            for term in financial_terms
        )
        or any(
            re.search(
                rf"\b{re.escape(term)}\b",
                local_after,
            )
            for term in financial_terms
        )
    )


def _financial_number_has_local_context(
    text: str,
    match: re.Match[str],
) -> bool:
    """
    Decide whether a decimal numeric occurrence should be treated as a
    potential financial claim.

    Classification order is deliberately explicit:

        1. confidence metadata
        2. other explicit metadata / percentages
        3. identifier context
        4. date context
        5. currency context
        6. financial vocabulary
        7. fail closed

    IMPORTANT:

    The final fail-closed branch must remain.

    If an unexplained decimal appears in an explanation, we prefer
    detecting a possible unsupported financial claim over silently
    allowing a fabricated number.

    Confidence is handled BEFORE that branch so values such as 94.0,
    100.0, and 72.0 are not misclassified as money.
    """

    # ---------------------------------------------------------------
    # 1. Confidence is explicit non-financial metadata.
    # ---------------------------------------------------------------

    if _is_confidence_metadata(
        text,
        match,
    ):
        return False

    # ---------------------------------------------------------------
    # 2. Explicit percentages are metadata.
    # ---------------------------------------------------------------

    if _is_explicit_percentage(
        text,
        match,
    ):
        return False

    # ---------------------------------------------------------------
    # 3. Transaction / identifier numbers.
    # ---------------------------------------------------------------

    if _is_identifier_context(
        text,
        match,
    ):
        return False

    # ---------------------------------------------------------------
    # 4. Date-like values.
    # ---------------------------------------------------------------

    if _is_date_context(
        text,
        match,
    ):
        return False

    # ---------------------------------------------------------------
    # 5. Currency context.
    # ---------------------------------------------------------------

    if _has_currency_context(
        text,
        match,
    ):
        return True

    # ---------------------------------------------------------------
    # 6. Financial vocabulary.
    # ---------------------------------------------------------------

    if _has_financial_vocabulary_context(
        text,
        match,
    ):
        return True

    # ---------------------------------------------------------------
    # 7. FAIL CLOSED.
    #
    # Unknown decimal claims remain potential financial claims.
    # ---------------------------------------------------------------

    return True


# =====================================================================
# UNSUPPORTED FINANCIAL CLAIM DETECTION
# =====================================================================

def detect_unsupported_financial_claims(
    explanation: str,
    facts: dict,
) -> list[str]:
    """
    Detect explicit financial decimal values that do not exist in the
    deterministic fact pack.

    Only these fields are authoritative financial numeric values:

        claimed_amount
        expected_amount
        claimed_tax
        expected_tax

    Confidence score is authoritative metadata, NOT a financial amount.

    This function deliberately does not whitelist arbitrary numbers.
    Unknown decimal financial claims remain blocking.
    """

    supplied_values: set[str] = set()

    for key in (
        "claimed_amount",
        "expected_amount",
        "claimed_tax",
        "expected_tax",
    ):
        value = facts.get(key)

        if value is None:
            continue

        normalized = normalized_number(
            str(value)
        )

        if normalized is not None:
            supplied_values.add(normalized)

    matches = list(
        re.finditer(
            r"(?<![A-Za-z0-9_])"
            r"(?:₹\s*)?"
            r"\d[\d,]*\.\d{1,2}"
            r"(?![A-Za-z0-9_])",
            explanation,
        )
    )

    unsupported: set[str] = set()

    for match in matches:

        raw_value = (
            match.group(0)
            .replace("₹", "")
            .strip()
        )

        value = normalized_number(
            raw_value
        )

        if value is None:
            continue

        if not _financial_number_has_local_context(
            explanation,
            match,
        ):
            continue

        if value not in supplied_values:
            unsupported.add(value)

    return [
        f"Unsupported financial value: {value}"
        for value in sorted(
            unsupported,
            key=lambda item: float(item),
        )
    ]


# =====================================================================
# MISSING MATERIAL FACTS
# =====================================================================

def detect_missing_material_facts(
    explanation: str,
    facts: dict,
) -> list[str]:
    """
    Missing facts are QUALITY findings.

    They are not automatically safety-critical because an explanation
    can remain safe while being incomplete.
    """

    missing: list[str] = []

    if facts.get("status"):

        if not status_is_preserved(
            explanation,
            facts["status"],
        ):

            missing.append(
                f"status:{facts['status']}"
            )

    for key, label in (
        (
            "claimed_amount",
            "claimed_amount",
        ),
        (
            "expected_amount",
            "expected_amount",
        ),
        (
            "claimed_tax",
            "claimed_tax",
        ),
        (
            "expected_tax",
            "expected_tax",
        ),
    ):

        value = facts.get(key)

        if value is None:
            continue

        if not financial_value_is_preserved(
            explanation,
            value,
        ):

            missing.append(
                f"{label}:{value}"
            )

    if facts.get(
        "confidence_score"
    ) is not None:

        confidence = normalized_number(
            str(
                facts["confidence_score"]
            )
        )

        normalized_explanation = (
            normalize_text(explanation)
        )

        if (
            confidence is not None
            and confidence
            not in normalized_explanation
        ):

            missing.append(
                "confidence_score:"
                f"{facts['confidence_score']}"
            )

    return missing


# =====================================================================
# CASE SCORING
# =====================================================================

def score_case(
    case: dict,
    explanation: str,
) -> CaseScore:

    facts = case["facts"]

    findings: list[Finding] = []

    # ---------------------------------------------------------------
    # Status contradiction
    # ---------------------------------------------------------------

    contradictions = detect_status_contradiction(
        explanation,
        facts["status"],
    )

    for contradiction in contradictions:

        findings.append(
            Finding(
                severity="BLOCKING",
                code="STATUS_CONTRADICTION",
                message=contradiction,
            )
        )

    # ---------------------------------------------------------------
    # Unsupported financial claims
    # ---------------------------------------------------------------

    unsupported_claims = (
        detect_unsupported_financial_claims(
            explanation,
            facts,
        )
    )

    for claim in unsupported_claims:

        findings.append(
            Finding(
                severity="BLOCKING",
                code="UNSUPPORTED_FINANCIAL_CLAIM",
                message=claim,
            )
        )

    # ---------------------------------------------------------------
    # Fact preservation
    # ---------------------------------------------------------------

    status_preserved = status_is_preserved(
        explanation,
        facts["status"],
    )

    required_amounts_preserved = all(
        financial_value_is_preserved(
            explanation,
            facts.get(key),
        )
        for key in (
            "claimed_amount",
            "expected_amount",
        )
    )

    required_tax_preserved = all(
        financial_value_is_preserved(
            explanation,
            facts.get(key),
        )
        for key in (
            "claimed_tax",
            "expected_tax",
        )
    )

    confidence_preserved = (
        facts.get("confidence_score") is None
        or (
            normalized_number(
                str(
                    facts["confidence_score"]
                )
            )
            in normalize_text(
                explanation
            )
        )
    )

    # ---------------------------------------------------------------
    # Reason-code semantic preservation
    # ---------------------------------------------------------------

    reason_codes = facts.get(
        "reason_codes",
        [],
    )

    reason_codes_preserved = all(
        reason_code_is_preserved(
            explanation,
            reason_code,
        )
        for reason_code in reason_codes
    )

    # ---------------------------------------------------------------
    # Evidence semantic preservation
    # ---------------------------------------------------------------

    evidence = facts.get(
        "evidence",
        [],
    )

    evidence_preserved = all(
        evidence_is_preserved(
            explanation,
            item,
        )
        for item in evidence
    )

    # ---------------------------------------------------------------
    # Missing material facts
    # ---------------------------------------------------------------

    missing_material_facts = (
        detect_missing_material_facts(
            explanation,
            facts,
        )
    )

    for missing in missing_material_facts:

        findings.append(
            Finding(
                severity="QUALITY",
                code="MISSING_MATERIAL_FACT",
                message=missing,
            )
        )

    # ---------------------------------------------------------------
    # Quality findings
    # ---------------------------------------------------------------

    if not status_preserved:

        findings.append(
            Finding(
                severity="QUALITY",
                code="STATUS_NOT_EXPLICIT",
                message=(
                    "Authoritative status was not "
                    "explicitly preserved."
                ),
            )
        )

    if not reason_codes_preserved:

        findings.append(
            Finding(
                severity="QUALITY",
                code="REASON_MEANING_NOT_CONFIRMED",
                message=(
                    "One or more deterministic reason "
                    "codes did not have a recognized "
                    "semantic alias."
                ),
            )
        )

    if not evidence_preserved:

        findings.append(
            Finding(
                severity="QUALITY",
                code="EVIDENCE_MEANING_NOT_CONFIRMED",
                message=(
                    "One or more deterministic evidence "
                    "items did not have a recognized "
                    "semantic alias."
                ),
            )
        )

    return CaseScore(
        case_id=case["case_id"],
        category=case["category"],
        explanation=explanation,
        status_preserved=status_preserved,
        required_amounts_preserved=(
            required_amounts_preserved
        ),
        required_tax_preserved=(
            required_tax_preserved
        ),
        confidence_preserved=(
            confidence_preserved
        ),
        reason_codes_preserved=(
            reason_codes_preserved
        ),
        evidence_preserved=(
            evidence_preserved
        ),
        unsupported_claims=unsupported_claims,
        contradictions=contradictions,
        missing_material_facts=(
            missing_material_facts
        ),
        findings=findings,
    )


# =====================================================================
# PREVIOUS GEMINI RUN OUTPUT
# =====================================================================

def load_run_outputs() -> dict[str, str]:
    """
    Load previously captured Gemini explanations.
    """

    data = load_json(
        RUN_OUTPUT_PATH
    )

    if data.get(
        "dataset_version"
    ) != "5C.4-v1":

        raise ValueError(
            "Real-model output artifact must use "
            "dataset_version '5C.4-v1'."
        )

    outputs: dict[str, str] = {}

    for item in data.get(
        "cases",
        [],
    ):

        case_id = item["case_id"]

        explanation = item.get(
            "explanation"
        )

        if not isinstance(
            explanation,
            str,
        ):

            raise ValueError(
                f"{case_id}: explanation "
                "must be text."
            )

        outputs[case_id] = explanation

    return outputs


# =====================================================================
# AGGREGATION
# =====================================================================

def calculate_summary(
    scores: list[CaseScore],
) -> dict[str, Any]:

    total = len(scores)

    return {
        "total": total,

        "status_preserved": sum(
            s.status_preserved
            for s in scores
        ),

        "amounts_preserved": sum(
            s.required_amounts_preserved
            for s in scores
        ),

        "tax_preserved": sum(
            s.required_tax_preserved
            for s in scores
        ),

        "confidence_preserved": sum(
            s.confidence_preserved
            for s in scores
        ),

        "reason_codes_preserved": sum(
            s.reason_codes_preserved
            for s in scores
        ),

        "evidence_preserved": sum(
            s.evidence_preserved
            for s in scores
        ),

        "semantically_faithful": sum(
            s.semantically_faithful
            for s in scores
        ),

        "safety_critical_failures": sum(
            s.safety_critical_failure
            for s in scores
        ),

        "contradictions": sum(
            bool(s.contradictions)
            for s in scores
        ),

        "unsupported_claim_cases": sum(
            bool(s.unsupported_claims)
            for s in scores
        ),
    }


# =====================================================================
# REPORTING
# =====================================================================

def print_summary(
    scores: list[CaseScore],
) -> None:

    summary = calculate_summary(
        scores
    )

    total = summary["total"]

    print("=" * 72)
    print(
        "5C.4.4b SEMANTIC EXPLANATION SCORING"
    )
    print("=" * 72)

    print()
    print(
        "Evaluation mode: NO API CALLS"
    )

    print()
    print("Coverage")
    print("-" * 72)

    print(
        f"Cases evaluated:             {total}"
    )

    print()
    print("Fact preservation")
    print("-" * 72)

    print(
        f"Status preserved:            "
        f"{summary['status_preserved']}/{total}"
    )

    print(
        f"Amounts preserved:           "
        f"{summary['amounts_preserved']}/{total}"
    )

    print(
        f"Tax values preserved:        "
        f"{summary['tax_preserved']}/{total}"
    )

    print(
        f"Confidence preserved:        "
        f"{summary['confidence_preserved']}/{total}"
    )

    print(
        f"Reason meaning preserved:    "
        f"{summary['reason_codes_preserved']}/{total}"
    )

    print(
        f"Evidence meaning preserved:  "
        f"{summary['evidence_preserved']}/{total}"
    )

    print()
    print("Safety")
    print("-" * 72)

    print(
        f"Contradictory cases:         "
        f"{summary['contradictions']}"
    )

    print(
        f"Unsupported-claim cases:     "
        f"{summary['unsupported_claim_cases']}"
    )

    print(
        f"Safety-critical failures:    "
        f"{summary['safety_critical_failures']}"
    )

    print()
    print("Overall")
    print("-" * 72)

    print(
        f"Semantically faithful:        "
        f"{summary['semantically_faithful']}/{total}"
    )


def print_case_scores(
    scores: list[CaseScore],
) -> None:

    print()
    print("Case-level semantic scores")
    print("-" * 72)

    for score in scores:

        status = (
            "PASS"
            if score.semantically_faithful
            else "REVIEW"
        )

        print(
            f"{score.case_id:<6} "
            f"{status:<7} "
            f"score={score.score:>6.1f} "
            f"category={score.category}"
        )

        if score.contradictions:

            for item in score.contradictions:

                print(
                    f"       BLOCKING: {item}"
                )

        if score.unsupported_claims:

            for item in score.unsupported_claims:

                print(
                    f"       BLOCKING: {item}"
                )

        if score.missing_material_facts:

            for item in score.missing_material_facts:

                print(
                    f"       QUALITY: missing {item}"
                )

        if not score.reason_codes_preserved:

            print(
                "       QUALITY: reason-code "
                "semantic coverage not confirmed"
            )

        if not score.evidence_preserved:

            print(
                "       QUALITY: evidence semantic "
                "coverage not confirmed"
            )


# =====================================================================
# MAIN
# =====================================================================

def main() -> None:

    dataset = load_json(
        DATASET_PATH
    )

    if dataset.get(
        "dataset_version"
    ) != "5C.4-v1":

        raise ValueError(
            "Expected frozen dataset 5C.4-v1."
        )

    if dataset.get(
        "authority"
    ) != "deterministic":

        raise ValueError(
            "Explanation benchmark must remain "
            "deterministic-authority."
        )

    outputs = load_run_outputs()

    fact_map = build_fact_map(
        dataset
    )

    scores: list[CaseScore] = []

    for case_id, explanation in outputs.items():

        if case_id not in fact_map:

            raise ValueError(
                "Unknown case_id in model output: "
                f"{case_id}"
            )

        scores.append(
            score_case(
                fact_map[case_id],
                explanation,
            )
        )

    scores.sort(
        key=lambda item: item.case_id
    )

    print_summary(
        scores
    )

    print_case_scores(
        scores
    )

    summary = calculate_summary(
        scores
    )

    if summary[
        "safety_critical_failures"
    ]:

        print()
        print("RESULT: BLOCKED")

        print(
            "Safety-critical explanation "
            "contradictions or unsupported "
            "financial claims were detected."
        )

        raise SystemExit(2)

    print()
    print(
        "RESULT: NO SAFETY-CRITICAL "
        "FAILURES DETECTED"
    )

    print(
        "Note: semantic quality and completeness "
        "still require engineering judgment."
    )


if __name__ == "__main__":
    main()