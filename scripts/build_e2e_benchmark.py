"""
Phase 5C.5.1 — Frozen E2E AI-assisted reconciliation benchmark builder.

Purpose
-------
Build the frozen E2E evaluation dataset from the existing deterministic
source data and ground truth.

CRITICAL ARCHITECTURAL RULES
----------------------------
1. This script is evaluation-only.
2. The production reconciliation pipeline must NEVER read ground_truth.json.
3. The benchmark contains the actual transaction inputs and bank narration
   that the AI layer will see.
4. Deterministic expected decisions remain evaluation authority.
5. This script does NOT call Gemini.
6. This script does NOT modify production financial logic.
7. The benchmark must be reproducible from the frozen source artifacts.

Required benchmark fields per case
-----------------------------------
case_id
category
input_transaction
narration
deterministic_expected_decision
expected_reason_codes
expected_financial_facts

Output
------
data/eval/e2e_reconciliation_benchmark_5C5_1.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
EVAL_DIR = DATA_DIR / "eval"

PG_PATH = RAW_DIR / "pg_settlement.json"
BANK_PATH = RAW_DIR / "bank_statement.json"
INVOICE_PATH = RAW_DIR / "merchant_invoice.json"
GROUND_TRUTH_PATH = DATA_DIR / "ground_truth.json"

OUTPUT_PATH = (
    EVAL_DIR / "e2e_reconciliation_benchmark_5C5_1.json"
)

EXPECTED_STAGE = "5C.5.1"
EXPECTED_VERSION = "5C.5-v1"


def load_json(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(
            f"Required evaluation input does not exist: {path}"
        )

    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def require_list(value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(
            f"{name} must be a JSON list."
        )

    result: list[dict[str, Any]] = []

    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(
                f"{name}[{index}] must be an object."
            )
        result.append(item)

    return result


def normalize_id(value: Any) -> str:
    if value is None:
        raise ValueError(
            "Transaction ID cannot be null."
        )

    value = str(value).strip()

    if not value:
        raise ValueError(
            "Transaction ID cannot be empty."
        )

    return value


def get_txn_id(
    record: dict[str, Any],
    source_name: str,
) -> str:
    """
    Resolve the logical transaction ID from each source's actual schema.

    PG:
        txn_id / transaction_id

    Invoice:
        txn_id / transaction_ref

    Bank:
        bank_ref = BANKREF_<txn_id>

    The bank source intentionally does not contain a direct txn_id.
    """

    if source_name == "pg":
        for field in (
            "txn_id",
            "transaction_id",
        ):
            if field in record and record[field] is not None:
                return normalize_id(record[field])

    elif source_name == "invoice":
        for field in (
            "txn_id",
            "transaction_ref",
            "transaction_id",
        ):
            if field in record and record[field] is not None:
                return normalize_id(record[field])

    elif source_name == "bank":
        bank_ref = record.get("bank_ref")

        if bank_ref is not None:
            bank_ref = str(bank_ref).strip()

            prefix = "BANKREF_"

            if bank_ref.startswith(prefix):
                txn_id = bank_ref[len(prefix):].strip()

                # Duplicate bank rows are deliberately represented by
                # appending "_DUP" to bank_ref.  That suffix identifies
                # a second physical bank row, NOT a new logical
                # transaction.  The benchmark must therefore resolve:
                #
                #   BANKREF_TXN_00051
                #   BANKREF_TXN_00051_DUP
                #
                # to the same logical transaction:
                #
                #   TXN_00051
                #
                # Keep this normalization confined to the evaluation
                # adapter.  We preserve the original bank_ref in the
                # actual input_transaction so duplicate evidence is not
                # lost.
                duplicate_suffix = "_DUP"

                if txn_id.endswith(duplicate_suffix):
                    txn_id = txn_id[:-len(duplicate_suffix)].rstrip()

                if txn_id:
                    return normalize_id(txn_id)

    raise ValueError(
        f"{source_name} record does not contain a resolvable "
        f"transaction identifier: {record}"
    )


def index_records(
    records: list[dict[str, Any]],
    source_name: str,
) -> dict[str, list[dict[str, Any]]]:
    indexed: dict[str, list[dict[str, Any]]] = {}

    for record in records:
        txn_id = get_txn_id(
            record,
            source_name,
        )

        indexed.setdefault(
            txn_id,
            [],
        ).append(record)

    return indexed


def extract_narration(
    bank_records: list[dict[str, Any]],
) -> str:
    """
    Return the bank narration that represents the unstructured input
    available to the AI extraction layer.

    We intentionally do NOT synthesize narration from txn_id.

    If multiple bank records exist for the transaction, use the first
    deterministic source record only; duplicate/ambiguity information
    remains represented in the input_transaction.
    """

    for bank in bank_records:
        for field in (
            "narration",
            "description",
        ):
            value = bank.get(field)

            if value is not None:
                text = str(value).strip()

                if text:
                    return text

    return ""


def make_input_transaction(
    pg_records: list[dict[str, Any]],
    bank_records: list[dict[str, Any]],
    invoice_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Preserve the actual source records.

    These are evaluation inputs, not financial decisions.
    """

    return {
        "pg": pg_records,
        "bank": bank_records,
        "invoice": invoice_records,
    }


def make_financial_facts(
    pg: dict[str, Any] | None,
    bank: dict[str, Any] | None,
    invoice: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Preserve financial values required for downstream faithfulness/
    equivalence evaluation without inventing values.

    Only values actually present in the source records are copied.
    """

    facts: dict[str, Any] = {}

    if pg is not None:
        for field in (
            "txn_id",
            "transaction_id",
            "gross_amount",
            "amount",
            "pg_fee",
            "fee",
            "gst_on_fee",
            "tax_amount",
            "tds_withheld",
            "net_payout",
            "net_amount",
            "utr",
        ):
            if field in pg:
                facts[f"pg_{field}"] = pg[field]

    if bank is not None:
        for field in (
            "utr",
            "credited_amount",
            "amount_credited",
            "value_date",
            "date",
            "bank_charges",
        ):
            if field in bank:
                facts[f"bank_{field}"] = bank[field]

    if invoice is not None:
        for field in (
            "txn_id",
            "transaction_ref",
            "invoice_amount",
            "gross_amount",
            "fee_amount",
            "claimed_gst",
            "tax_amount",
            "claimed_tds",
            "tds_amount",
            "net_amount",
        ):
            if field in invoice:
                facts[f"invoice_{field}"] = invoice[field]

    return facts


def normalize_reason_codes(
    truth: dict[str, Any],
) -> list[str]:
    """
    Ground-truth schema compatibility.

    Prefer an explicit reason_codes field when available.

    Otherwise derive ONLY the primary exception code from the
    deterministic ground truth. We do not invent additional codes.
    """

    raw = truth.get("expected_reason_codes")

    if raw is not None:
        if not isinstance(raw, list):
            raise ValueError(
                "expected_reason_codes must be a list."
            )

        return [
            str(code)
            for code in raw
        ]

    exception_code = truth.get(
        "expected_exception_code"
    )

    if exception_code is None:
        return []

    code = str(exception_code)

    if code in (
        "",
        "NONE",
    ):
        return []

    return [code]


def normalize_expected_decision(
    truth: dict[str, Any],
) -> dict[str, Any]:
    """
    Preserve the deterministic expected decision.

    Existing datasets use expected_status / expected_exception_code.
    Newer benchmark data may already contain a structured decision.
    """

    structured = truth.get(
        "deterministic_expected_decision"
    )

    if isinstance(structured, dict):
        return structured

    decision: dict[str, Any] = {}

    for field in (
        "expected_status",
        "expected_exception_code",
        "expected_matched_sources",
        "expected_tax_verified",
    ):
        if field in truth:
            decision[field] = truth[field]

    if not decision:
        raise ValueError(
            "Ground truth does not contain a deterministic "
            "expected decision."
        )

    return decision


def get_truth_records(
    ground_truth: Any,
) -> list[dict[str, Any]]:
    """
    Support both:

    1. list-based ground_truth
    2. GroundTruthDataset-style:
       {"records": {...}}
    """

    if isinstance(ground_truth, list):
        return require_list(
            ground_truth,
            "ground_truth",
        )

    if isinstance(ground_truth, dict):
        records = ground_truth.get("records")

        if isinstance(records, dict):
            result = []

            for txn_id, truth in records.items():
                if not isinstance(truth, dict):
                    raise ValueError(
                        f"ground_truth.records[{txn_id}] "
                        "must be an object."
                    )

                item = dict(truth)

                item.setdefault(
                    "txn_id",
                    txn_id,
                )

                result.append(item)

            return result

        cases = ground_truth.get("cases")

        if isinstance(cases, list):
            return require_list(
                cases,
                "ground_truth.cases",
            )

    raise ValueError(
        "Unsupported ground_truth.json structure."
    )


def build_case(
    index: int,
    truth: dict[str, Any],
    pg_index: dict[str, list[dict[str, Any]]],
    bank_index: dict[str, list[dict[str, Any]]],
    invoice_index: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    txn_id = normalize_id(
        truth.get(
            "txn_id",
            truth.get("transaction_id"),
        )
    )

    pg_records = pg_index.get(
        txn_id,
        [],
    )

    bank_records = bank_index.get(
        txn_id,
        [],
    )

    invoice_records = invoice_index.get(
        txn_id,
        [],
    )

    if not pg_records:
        raise ValueError(
            f"{txn_id}: no PG source record found."
        )

    pg = pg_records[0]
    bank = bank_records[0] if bank_records else None
    invoice = (
        invoice_records[0]
        if invoice_records
        else None
    )

    narration = extract_narration(
        bank_records
    )

    expected_decision = (
        normalize_expected_decision(truth)
    )

    reason_codes = normalize_reason_codes(
        truth
    )

    financial_facts = make_financial_facts(
        pg,
        bank,
        invoice,
    )

    category = str(
        truth.get(
            "category",
            "unspecified",
        )
    )

    case_id = f"E2E_{index:03d}"

    return {
        "case_id": case_id,
        "category": category,

        # Actual source information available to the E2E system.
        "input_transaction": make_input_transaction(
            pg_records,
            bank_records,
            invoice_records,
        ),

        # This is the unstructured AI input.
        # Never synthesize this from txn_id.
        "narration": narration,

        # Deterministic evaluation authority.
        "deterministic_expected_decision": (
            expected_decision
        ),

        "expected_reason_codes": reason_codes,

        "expected_financial_facts": financial_facts,

        "source_transaction_id": txn_id,

        "notes": truth.get(
            "notes",
            "",
        ),
    }


def validate_case(
    case: dict[str, Any],
) -> None:
    required = (
        "case_id",
        "category",
        "input_transaction",
        "narration",
        "deterministic_expected_decision",
        "expected_reason_codes",
        "expected_financial_facts",
    )

    for field in required:
        if field not in case:
            raise ValueError(
                f"{case['case_id']}: missing required "
                f"benchmark field '{field}'."
            )

    if not isinstance(
        case["input_transaction"],
        dict,
    ):
        raise ValueError(
            f"{case['case_id']}: input_transaction "
            "must be an object."
        )

    if not isinstance(
        case["deterministic_expected_decision"],
        dict,
    ):
        raise ValueError(
            f"{case['case_id']}: deterministic_expected_decision "
            "must be an object."
        )

    if not isinstance(
        case["expected_reason_codes"],
        list,
    ):
        raise ValueError(
            f"{case['case_id']}: expected_reason_codes "
            "must be a list."
        )

    if not isinstance(
        case["expected_financial_facts"],
        dict,
    ):
        raise ValueError(
            f"{case['case_id']}: expected_financial_facts "
            "must be an object."
        )

    if not isinstance(
        case["narration"],
        str,
    ):
        raise ValueError(
            f"{case['case_id']}: narration must be a string."
        )


def build_benchmark() -> dict[str, Any]:
    pg_records = require_list(
        load_json(PG_PATH),
        "PG records",
    )

    bank_records = require_list(
        load_json(BANK_PATH),
        "bank records",
    )

    invoice_records = require_list(
        load_json(INVOICE_PATH),
        "invoice records",
    )

    ground_truth = load_json(
        GROUND_TRUTH_PATH
    )

    truths = get_truth_records(
        ground_truth
    )

    if not truths:
        raise ValueError(
            "Ground truth contains no records."
        )

    pg_index = index_records(
    pg_records,
    "pg",
    )

    bank_index = index_records(
    bank_records,
    "bank",
    )

    invoice_index = index_records(
    invoice_records,
    "invoice",
    )

    cases = []

    for index, truth in enumerate(
        truths,
        start=1,
    ):
        case = build_case(
            index,
            truth,
            pg_index,
            bank_index,
            invoice_index,
        )

        validate_case(case)

        cases.append(case)

    case_ids = [
        case["case_id"]
        for case in cases
    ]

    if len(case_ids) != len(
        set(case_ids)
    ):
        raise ValueError(
            "Duplicate case IDs detected."
        )

    txn_ids = [
        case["source_transaction_id"]
        for case in cases
    ]

    if len(txn_ids) != len(
        set(txn_ids)
    ):
        raise ValueError(
            "Duplicate source transaction IDs detected."
        )

    EVAL_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    return {
        "dataset_version": EXPECTED_VERSION,
        "evaluation_stage": EXPECTED_STAGE,

        "authority": "deterministic",

        "model_role": (
            "read_only_candidate_and_explanation"
        ),

        "description": (
            "Frozen end-to-end benchmark containing "
            "real source transaction inputs and bank "
            "narrations for AI-assisted reconciliation "
            "evaluation."
        ),

        "source_artifacts": {
            "pg": str(
                PG_PATH.relative_to(ROOT)
            ),
            "bank": str(
                BANK_PATH.relative_to(ROOT)
            ),
            "invoice": str(
                INVOICE_PATH.relative_to(ROOT)
            ),
            "ground_truth": str(
                GROUND_TRUTH_PATH.relative_to(ROOT)
            ),
        },

        "case_count": len(cases),

        "cases": cases,
    }


def main() -> None:
    benchmark = build_benchmark()

    with OUTPUT_PATH.open(
        "w",
        encoding="utf-8",
    ) as handle:
        json.dump(
            benchmark,
            handle,
            indent=2,
            ensure_ascii=False,
        )

    print("=" * 72)
    print(
        "5C.5.1 FROZEN E2E AI-ASSISTED "
        "RECONCILIATION BENCHMARK"
    )
    print("=" * 72)
    print()
    print(
        f"Cases: {benchmark['case_count']}"
    )
    print(
        "Required input: "
        "transaction + actual narration"
    )
    print(
        "Financial authority: deterministic"
    )
    print(
        "AI authority: none"
    )
    print()
    print(
        "Benchmark validation: PASS"
    )
    print(
        f"Artifact: {OUTPUT_PATH}"
    )
    print(
        "5C.5.1 frozen E2E benchmark: COMPLETE"
    )


if __name__ == "__main__":
    main()