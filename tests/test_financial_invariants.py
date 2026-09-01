# tests/test_financial_invariants.py
"""
Guards the single-definition property of settlement arithmetic.

WHAT THIS PROTECTS
------------------
`expected_net = gross - fee - GST - TDS` used to exist as four
independent inline copies -- in candidates.py, engine.py, scoring.py
and manager.py. They agreed, so no test could see the problem: a
divergence that has not happened yet is invisible to every assertion
in the repository.

That is the failure shape this file exists to make impossible. Two
independent guards:

    BEHAVIOURAL  every consumer, over the real batch, must agree
                 with src/financial.py to the paise

    STRUCTURAL   no module outside src/financial.py may re-derive the
                 expression inline, so a future contributor cannot
                 reintroduce a fifth copy without this failing

The structural guard matters more than it looks. The behavioural
guard only catches a divergence once someone has already written one
AND the real batch happens to exercise it. The structural guard
rejects the copy at the moment it appears.

WHY THIS IS NOT PARANOIA
------------------------
The moment a settlement term is added -- a refund, a chargeback, an
adjustment, the negative line items that make real reconciliation
hard -- a partial edit across four copies would leave candidate
ranking, confidence scoring and the AMOUNT_MISMATCH control each
reconciling against a different definition of the same settlement.
Nothing would raise. The batch would just be wrong.
"""

import re
import sys
from decimal import Decimal
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.financial import (
    expected_invoice_amount,
    settlement_expected_net,
)
from src.ingestion.loader import load_batch
from src.matching.engine import run_matching
from src.normalization.engine import normalize_batch

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
RAW_DIR = ROOT / "data" / "raw"


def _match_results():
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    return run_matching(normalized.records)


# ======================================================================
# BEHAVIOURAL -- every consumer agrees, over the real batch
# ======================================================================

def test_scoring_signal_agrees_with_shared_definition():
    """
    scoring.py publishes its expected-net into the audit signals as a
    string. That published value is what a reviewer reads off the
    evidence trail, so it must be the shared definition and not a
    private re-derivation that merely looks similar.
    """
    checked = 0

    for result in _match_results():
        signal = result.score.signals.get("amount_bank")
        if signal is None:
            continue

        published = Decimal(signal["pg_expected_net"])
        shared = settlement_expected_net(result.pg_record)

        assert published == shared, (
            f"{result.txn_id}: scoring.py published {published} but "
            f"src/financial.py computes {shared}"
        )
        checked += 1

    assert checked > 0, "no bank-present records -- guard proved nothing"


def test_amount_control_decision_agrees_with_shared_definition():
    """
    manager.py raises AMOUNT_MISMATCH from its own comparison.
    Recompute that comparison from the shared definition and require
    the same verdict for every record the control actually evaluated.

    A disagreement here means the engine flagged -- or cleared -- a
    settlement against arithmetic no other layer uses.
    """
    from src.config import AMOUNT_TOLERANCE
    from src.exceptions.manager import decide_batch

    results = _match_results()
    decisions = {d.txn_id: d for d in decide_batch(results)}
    checked = 0

    for result in results:
        context = decisions[result.txn_id].evidence["context"]

        if result.bank_record is None:
            continue
        if context["is_ambiguous"] or context["duplicate_detected"]:
            continue
        if context["no_candidates_found"] or context["missing_bank"]:
            continue

        expected = settlement_expected_net(result.pg_record)
        recomputed = (
            abs(result.bank_record.amount - expected) > AMOUNT_TOLERANCE
        )
        reported = context["amount_mismatch"]

        assert recomputed == reported, (
            f"{result.txn_id}: manager reported amount_mismatch="
            f"{reported}, shared definition gives {recomputed}"
        )
        checked += 1

    assert checked > 0, "amount control never evaluated -- guard proved nothing"


def test_invoice_amount_signal_agrees_with_shared_definition():
    checked = 0

    for result in _match_results():
        signal = result.score.signals.get("amount_invoice")
        if signal is None:
            continue

        published = Decimal(signal["pg_expected_fee_plus_gst"])
        shared = expected_invoice_amount(result.pg_record)

        assert published == shared, result.txn_id
        checked += 1

    assert checked > 0, "no invoice-present records -- guard proved nothing"


def test_shared_definition_matches_generator_net_payout():
    """
    The generator writes net_payout independently, from its own
    arithmetic at build time. It is the closest thing to an external
    oracle this project has for settlement maths.

    Records whose bank credit was deliberately perturbed are still
    included: net_payout is a PG-side field and the perturbation is
    applied to the BANK row, so this comparison stays valid for every
    record that reached matching.
    """
    checked = 0

    for result in _match_results():
        stated = result.pg_record.raw_ref.get("net_payout")
        if stated is None:
            continue

        shared = settlement_expected_net(result.pg_record)

        assert Decimal(stated) == shared, (
            f"{result.txn_id}: generator wrote net_payout={stated}, "
            f"engine computes {shared}"
        )
        checked += 1

    assert checked >= 50, f"only {checked} records cross-checked"


# ======================================================================
# STRUCTURAL -- no fifth copy can be introduced
# ======================================================================

_TDS_SUBTRACTION = re.compile(
    r"-\s*\(?\s*(?:pg_record\.tds|pg\.tds|pg_tds)\b"
)

_FEE_PLUS_GST = re.compile(
    r"\b(?:pg_fee|pg_record\.fee|pg\.fee)\s*\+\s*"
    r"(?:pg_gst|pg_record\.gst|pg\.gst)\b"
)


def _production_sources():
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "financial.py":
            continue
        if "__pycache__" in path.parts:
            continue
        yield path


def _offending_lines(pattern):
    offenders = []

    for path in _production_sources():
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            if pattern.search(line):
                offenders.append(
                    f"{path.relative_to(ROOT)}:{number}: {line.strip()}"
                )

    return offenders


def test_no_module_re_derives_expected_net_inline():
    """
    Subtracting a TDS term anywhere outside src/financial.py is the
    signature of a re-derived expected_net. Import the function
    instead.
    """
    offenders = _offending_lines(_TDS_SUBTRACTION)

    assert not offenders, (
        "expected_net appears to be re-derived outside "
        "src/financial.py:\n  " + "\n  ".join(offenders)
    )


def test_no_module_re_derives_invoice_amount_inline():
    offenders = _offending_lines(_FEE_PLUS_GST)

    assert not offenders, (
        "expected_invoice_amount appears to be re-derived outside "
        "src/financial.py:\n  " + "\n  ".join(offenders)
    )


def test_every_settlement_consumer_imports_the_shared_definition():
    """
    Positive counterpart to the two structural greps above.

    Those prove nobody re-derives the expression. This proves the
    layers that MUST use it actually import it -- so deleting a call
    site, rather than duplicating one, also fails.
    """
    required = [
        "matching/candidates.py",
        "matching/engine.py",
        "matching/scoring.py",
        "exceptions/manager.py",
    ]

    for relative in required:
        source = (SRC / relative).read_text(encoding="utf-8")

        assert "from src.financial import" in source, (
            f"src/{relative} does not import from src.financial"
        )
        assert "settlement_expected_net" in source, (
            f"src/{relative} does not use settlement_expected_net"
        )


# ======================================================================
# UNIT -- the definition itself
# ======================================================================

def test_absent_components_are_treated_as_zero():
    from datetime import datetime, timezone

    from src.models import NormalizedRecord

    bare = NormalizedRecord(
        txn_id="TXN_00001",
        source="pg",
        amount=Decimal("100.00"),
        date_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert settlement_expected_net(bare) == Decimal("100.00")
    assert expected_invoice_amount(bare) == Decimal("0")


def test_present_zero_and_absent_are_both_zero_valued():
    """
    Decimal("0") is falsy. The `value or ZERO` idiom would conflate
    "absent" with "present and zero"; both must yield the same number
    here, and _or_zero must reach that answer by an explicit None
    check so a future negative component is not silently dropped.
    """
    from src.financial import _or_zero

    assert _or_zero(None) == Decimal("0")
    assert _or_zero(Decimal("0")) == Decimal("0")
    assert _or_zero(Decimal("-5.00")) == Decimal("-5.00")
