#!/usr/bin/env bash
#
# verify.sh -- reproduce the headline metrics this repository publishes.
#
# NOT every number. The banner says what is and is not covered. That
# distinction matters: an earlier draft called itself "verifying every
# published number", which was a claim stronger than its mechanism --
# inside the script written to catch claims stronger than their
# mechanisms. FAILURE_LOG.md section 72.
#
# FAILURE_LOG.md section 70 ends with a rule this script exists to obey:
#
#     If a document cites a value as evidence, a command in the
#     repository must reproduce it.
#
# Section 70 applied that to one hash. Section 71 found the same defect
# in the README, whose tool-selection table cited a figure its own
# artifact contradicted -- because nothing in the repository read the
# README. This script is the command behind the citations.
#
# Every expected value below is HARD-CODED. That is deliberate: a script
# that reads the expected value out of the same artifact it is checking
# proves only that a file equals itself. When a number legitimately
# changes, this file must be edited, and that edit is the record.
#
# Requires no API key. If it ever does, it is wrong -- the deterministic
# core is the thing being verified, and it has no model in it.
#
# Usage:  ./verify.sh            (exit 0 = every line PASS)

set -u

PY="${PYTHON:-python}"
FAILURES=0

pass() { printf '[PASS] %-28s %s\n' "$1" "$2"; }
fail() {
    printf '[FAIL] %-28s expected %s, got %s\n' "$1" "$2" "$3"
    FAILURES=$((FAILURES + 1))
}

check() {
    # check <label> <expected> <actual>
    if [ "$2" = "$3" ]; then pass "$1" "$2"; else fail "$1" "$2" "$3"; fi
}

cd "$(dirname "$0")" || exit 2
export PYTHONIOENCODING=utf-8

echo "======================================================================"
echo "  AI Finance Controller -- verifying the headline metrics"
echo "  $(git rev-parse --short HEAD 2>/dev/null || echo 'not a git checkout')"
echo "======================================================================"
echo "  23 checks: reconciliation, cash position, accuracy, decision"
echo "  snapshot, tool selection, throughput artifact, README/artifact"
echo "  consistency, ground-truth isolation."
echo "  NOT covered: fuzzy-tier precision, explanation quality, and any"
echo "  measurement that needs a live model."
echo

# ---------------------------------------------------------------------
# 1. The suite
# ---------------------------------------------------------------------
SUITE=$($PY -m pytest tests/ -q 2>&1 | tail -20 | grep -oE '[0-9]+ passed' | head -1)
check "test suite" "486 passed" "${SUITE:-no result}"

# ---------------------------------------------------------------------
# 2. The deterministic pipeline -- decisions and money
# ---------------------------------------------------------------------
PIPE=$($PY scripts/run_pipeline.py 2>&1)

grab() { echo "$PIPE" | grep -oE "$1" | head -1; }

check "match rate"        "24/61 (39.34%)" "$(grab 'Match rate: [0-9]+/[0-9]+ \([0-9.]+%\)' | sed 's/Match rate: //')"
check "exceptions"        "37"             "$(grab 'EXCEPTION LIST — [0-9]+ records' | grep -oE '[0-9]+' | head -1)"
check "settled+verified"  "292,353.70"     "$(echo "$PIPE" | grep 'settled and verified'  | grep -oE '[0-9,]+\.[0-9]{2}')"
check "awaiting verif."   "67,328.14"      "$(echo "$PIPE" | grep 'awaiting verification' | grep -oE '[0-9,]+\.[0-9]{2}')"
check "blocked"           "601,761.49"     "$(echo "$PIPE" | grep 'blocked in exceptions' | grep -oE '[0-9,]+\.[0-9]{2}')"
check "not yet credited"  "38,456.77"      "$(echo "$PIPE" | grep 'not yet credited'      | grep -oE '[0-9,]+\.[0-9]{2}')"
check "total expected"    "999,900.10"     "$(echo "$PIPE" | grep 'total expected'        | grep -oE '[0-9,]+\.[0-9]{2}')"
check "bank credited"     "961,259.33"     "$(echo "$PIPE" | grep 'bank actually credited'| grep -oE '[0-9,]+\.[0-9]{2}')"
check "variance"          "38,640.77"      "$(echo "$PIPE" | grep -E '^  variance'        | grep -oE '[0-9,]+\.[0-9]{2}')"

# Bank-side completeness is a partition, so the parts must sum to the whole.
check "bank rows accounted" "59+3+2=64" \
    "$(echo "$PIPE" | awk '
        /selected into a match/ {s=$NF}
        /duplicate credit/      {d=$3}
        /claimed by nothing/    {o=$NF}
        END {printf "%d+%d+%d=%d", s, d, o, s+d+o}')"
check "unclaimed value"   "517.48" "$(echo "$PIPE" | grep -oE 'INR [0-9,]+\.[0-9]{2} the bank moved' | grep -oE '[0-9,]+\.[0-9]{2}')"

# Ingestion refusals are part of the published story, not a footnote.
check "ingestion rejects" "2" "$(echo "$PIPE" | grep -c 'SCHEMA_VALIDATION_FAILED')"

# ---------------------------------------------------------------------
# 3. Accuracy against ground truth the pipeline never reads
# ---------------------------------------------------------------------
ACC=$($PY scripts/report_accuracy.py 2>&1)
check "status accuracy"    "55/61  (90.16%)" "$(echo "$ACC" | grep 'STATUS accuracy'     | sed 's/.*: //')"
check "exc-code accuracy"  "55/61  (90.16%)" "$(echo "$ACC" | grep 'EXCEPTION-CODE'      | sed 's/.*: //')"

# The category table must reproduce the headline's denominator. Section 71
# is the case where it did not.
check "category table" "63 total / 61 evaluable / 55 ok" "$($PY - <<'PYEOF'
import json
d = json.load(open("data/eval/accuracy_report.json"))
bc = d["by_category"].values()
t = sum(v["total"] for v in bc)
e = sum(v["total"] - v.get("not_evaluable", 0) for v in bc)
s = sum(v["status_ok"] for v in bc)
print(f"{t} total / {e} evaluable / {s} ok")
PYEOF
)"

# ---------------------------------------------------------------------
# 4. The decision snapshot -- the pin, and proof the pin can fail
# ---------------------------------------------------------------------
check "decision snapshot" "d8134bab221d1046" "$($PY - <<'PYEOF'
import hashlib, sys
sys.path.insert(0, ".")
sys.path.insert(0, "tests")
from test_decision_snapshot import run_pipeline, snapshot_hash
print(snapshot_hash(run_pipeline()))
PYEOF
)"
SNAP=$($PY -m pytest tests/test_decision_snapshot.py -q 2>&1 | grep -oE '[0-9]+ passed' | head -1)
check "snapshot controls" "16 passed" "${SNAP:-no result}"

# ---------------------------------------------------------------------
# 5. Tool selection -- the one measurement that needs a model to produce,
#    read from the committed artifact rather than re-run.
# ---------------------------------------------------------------------
check "tool selection" "model 29/32 baseline 27/32 (3 provider failures)" "$($PY - <<'PYEOF'
import json
d = json.load(open("data/eval/agent_tool_selection_report.json"))
m, b = d["model"]["metrics"], d["baseline"]["metrics"]
print(f"model {m['tool_correct']}/{m['total_cases']} "
      f"baseline {b['tool_correct']}/{b['total_cases']} "
      f"({m['provider_failures']} provider failures)")
PYEOF
)"

# The README quotes that artifact per category. Section 71 is what happens
# when nothing checks that it quotes it correctly.
check "README matches artifact" "ok" "$($PY - <<'PYEOF'
import json, re
d = json.load(open("data/eval/agent_tool_selection_report.json"))
bc = d["model"]["metrics"]["by_category"]
readme = open("README.md", encoding="utf-8").read()
alias = {"out of scope": "out_of_scope", "prompt injection": "prompt_injection"}
bad, seen = [], 0
for name, bo, bt, mo, mt in re.findall(
        r"^\| [`*]{0,2}([a-z_ ]+)[`*]{0,2} \| \*{0,2}(\d)/(\d)\*{0,2} \| \*{0,2}(\d)/(\d)\*{0,2} \|",
        readme, re.M):
    key = alias.get(name.strip(), name.strip())
    if key not in bc:
        continue
    seen += 1
    if (int(mo), int(mt)) != (bc[key]["tool_ok"], bc[key]["total"]):
        bad.append(f"{key}: README {mo}/{mt} vs artifact "
                   f"{bc[key]['tool_ok']}/{bc[key]['total']}")
total = sum(bc[alias.get(n.strip(), n.strip())]["tool_ok"]
            for n, *_ in re.findall(
                r"^\| [`*]{0,2}([a-z_ ]+)[`*]{0,2} \| \*{0,2}(\d)/(\d)\*{0,2} \| \*{0,2}(\d)/(\d)\*{0,2} \|",
                readme, re.M)
            if alias.get(n.strip(), n.strip()) in bc)
if seen != len(bc):
    bad.append(f"README lists {seen} categories, artifact has {len(bc)}")
if total != d["model"]["metrics"]["tool_correct"]:
    bad.append(f"README column sums to {total}, headline is "
               f"{d['model']['metrics']['tool_correct']}")
print("ok" if not bad else "; ".join(bad))
PYEOF
)"

# ---------------------------------------------------------------------
# 5b. The published throughput artifact against the README table that
#     quotes it. Reads the committed artifact; runs no benchmark.
# ---------------------------------------------------------------------
check "throughput vs README" "ok" "$($PY - <<'PYEOF'
import json
rows = json.load(open("data/throughput_benchmark.json"))
by_n = {r["n_records"]: r["records_per_second"] for r in rows}
readme = open("README.md", encoding="utf-8").read()
bad = []
for n, quoted in ((60, "1,348.5"), (5000, "179.2")):
    actual = f"{by_n[n]:,}"
    if actual != quoted:
        bad.append(f"n={n}: artifact says {actual}, README quotes {quoted}")
    elif quoted not in readme:
        bad.append(f"n={n}: README no longer quotes {quoted}")
print("ok" if not bad else "; ".join(bad))
PYEOF
)"

# ---------------------------------------------------------------------
# 6. Boundaries that no number would show
# ---------------------------------------------------------------------
check "ground truth isolation" "0" \
    "$(grep -rn 'ground_truth' src/ --include='*.py' 2>/dev/null | wc -l | tr -d ' ')"
check "failure log sections" "72" \
    "$(grep -cE '^#{1,3} [0-9]+[.:] ' FAILURE_LOG.md)"

echo
echo "----------------------------------------------------------------------"
if [ "$FAILURES" -eq 0 ]; then
    echo "  ALL CHECKS PASSED -- every headline metric reproduced."
    echo "----------------------------------------------------------------------"
    exit 0
fi
echo "  $FAILURES CHECK(S) FAILED -- a headline metric no longer reproduces."
echo "  This is a defect in the change, not a new result to document."
echo "----------------------------------------------------------------------"
exit 1
