"""
Phase 5C.4.4b — Offline semantic evaluation integrity tests.

No Gemini/API calls.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


from scripts.eval_explanation_semantics import (
    validate_dataset,
    validate_generation_artifact,
)


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


def load(path: Path) -> dict:
    with path.open(
        "r",
        encoding="utf-8",
    ) as f:
        return json.load(f)


def test_dataset_is_frozen() -> None:
    dataset = load(DATASET_PATH)

    cases = validate_dataset(dataset)

    assert dataset["dataset_version"] == "5C.4-v1"
    assert dataset["authority"] == "deterministic"
    assert dataset["model_role"] == (
        "read_only_explanation"
    )
    assert len(cases) == 8


def test_case_ids_are_unique() -> None:
    dataset = load(DATASET_PATH)

    cases = validate_dataset(dataset)

    ids = [
        case["case_id"]
        for case in cases
    ]

    assert len(ids) == len(set(ids))


def test_real_run_matches_frozen_dataset() -> None:
    dataset = load(DATASET_PATH)
    artifact = load(RUN_OUTPUT_PATH)

    benchmark_cases = validate_dataset(
        dataset
    )

    expected_ids = {
        case["case_id"]
        for case in benchmark_cases
    }

    captured = validate_generation_artifact(
        artifact,
        expected_ids,
    )

    assert len(captured) == 8
    assert artifact["evaluated_cases"] == 8


def test_real_run_is_5c43() -> None:
    artifact = load(RUN_OUTPUT_PATH)

    assert artifact["evaluation_stage"] == (
        "5C.4.3"
    )

    assert artifact["dataset_version"] == (
        "5C.4-v1"
    )

    assert artifact["authority"] == (
        "deterministic"
    )

    assert artifact["model_role"] == (
        "read_only_explanation"
    )


def test_no_duplicate_captured_cases() -> None:
    artifact = load(RUN_OUTPUT_PATH)

    ids = [
        case["case_id"]
        for case in artifact["cases"]
    ]

    assert len(ids) == len(set(ids))


def main() -> None:
    test_dataset_is_frozen()
    test_case_ids_are_unique()
    test_real_run_matches_frozen_dataset()
    test_real_run_is_5c43()
    test_no_duplicate_captured_cases()

    print(
        "5C.4.4b semantic evaluation integrity tests passed."
    )


if __name__ == "__main__":
    main()