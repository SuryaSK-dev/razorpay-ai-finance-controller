# scripts/demo_agent.py
"""
Runnable demonstration: shows the agent explaining real decisions
from the actual generated batch, and shows the fallback path working
when the LLM call is simulated to fail. Suitable for the pitch video.
"""

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from src.ingestion.loader import load_batch
from src.normalization.engine import normalize_batch
from src.matching.engine import run_matching
from src.exceptions.manager import decide_batch
from src.agent.explainer import explain_decision_via_llm, fallback_template_explanation

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def mock_llm(prompt: str) -> str:
    """Placeholder for a real API call -- swap for an actual Claude/
    GPT call when wiring in credentials. Kept as a mock here so the
    demo runs without requiring API keys."""
    return ("This transaction was flagged because the invoice's claimed "
            "GST amount does not match the statutory 18% calculation on "
            "the payment gateway fee, indicating a tax reporting error "
            "that requires correction before settlement can be confirmed.")


def broken_llm(prompt: str) -> str:
    raise ConnectionError("simulated failure for fallback demonstration")


def main():
    batch = load_batch(RAW_DIR)
    normalized = normalize_batch(batch)
    match_results = run_matching(normalized.records)
    decisions = decide_batch(match_results)

    tax_mismatches = [d for d in decisions if d.status.value == "TAX_MISMATCH"]
    if not tax_mismatches:
        print("No TAX_MISMATCH decisions in this batch to demonstrate.")
        return

    sample = tax_mismatches[0]

    print("=" * 70)
    print(f"DEMO 1: Successful LLM explanation for {sample.txn_id}")
    print("=" * 70)
    result = explain_decision_via_llm(sample, mock_llm)
    print(f"Succeeded: {result.succeeded}")
    print(f"Explanation: {result.value.text}\n")

    print("=" * 70)
    print(f"DEMO 2: LLM failure -- deterministic fallback for {sample.txn_id}")
    print("=" * 70)
    result = explain_decision_via_llm(sample, broken_llm)
    print(f"Succeeded: {result.succeeded}")
    print(f"Error: {result.error}")
    fallback = fallback_template_explanation(sample)
    print(f"Fallback explanation used instead: {fallback.text}")
    print("\nNote: decision.status and decision.exception_code are UNCHANGED")
    print("in both cases -- only the human-readable narration differs.")


if __name__ == "__main__":
    main()