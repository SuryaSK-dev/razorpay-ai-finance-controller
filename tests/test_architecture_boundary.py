# tests/test_architecture_boundary.py
"""
Guards the direction of the hard boundary.

THE CLAIM THIS PROTECTS
-----------------------
README.md and ARCHITECTURE.md both state that deterministic code owns
financial truth and the model sits outside it. That is a claim about
DIRECTION: the agent layer may depend on the core, and the core must not
depend on the agent layer.

Until this file existed, the claim was guarded in the wrong place.
`test_script_imports_no_provider` in test_run_pipeline.py asserts a
REPORTING SCRIPT imports no provider -- useful, but that script is not
where the boundary matters. Nothing asserted the property for
src/matching/, src/tax/ or src/exceptions/, which are the modules that
actually compute financial outcomes.

The weaker boundary was guarded and the stronger one was not.

TWO LEVELS OF CHECK
-------------------
    STATIC   no core module may reference src.agent, except one
             explicitly named exemption

    RUNTIME  importing the core must not LOAD the agent package,
             which is the property that actually matters at execution
             time and which a deferred import preserves

The runtime check is the stronger of the two. A function-local import
inside a never-called function is invisible at run time; the static check
exists so the exemption stays visible to a reader.
"""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

sys.path.append(str(ROOT))


# ======================================================================
# THE ONE KNOWN EXEMPTION
# ======================================================================
#
# src/matching/candidates.py contains a function-local import of
# src.agent.narration_extractor inside find_bank_candidates_with_llm_assist.
#
# That function is deliberately disconnected -- FAILURE_LOG.md section 50
# records three designs for connecting it and the concrete reason each
# failed, and section 24 lists the preconditions that were never met.
#
# The exemption is NAMED rather than the check being weakened, so that:
#   - a reader sees exactly one hole and why it is there
#   - a SECOND core module importing the agent layer fails immediately
#   - moving the function into src/agent/ would let this entry be deleted
#
# It is paired with test_the_exempted_import_is_still_deferred(), which
# asserts the exemption stays function-local. A module-level import at
# the same site would be a genuine boundary breach and would still fail.

EXEMPTIONS = {
    ("src/matching/candidates.py", "src.agent.narration_extractor"),
}


def _core_files():
    """Every tracked src/ file that is NOT part of the agent layer."""
    for path in sorted(SRC.rglob("*.py")):
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        if relative.startswith("src/agent/"):
            continue
        yield relative, path


def _agent_imports(path: Path):
    """(lineno, module) for every src.agent reference in one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("src.agent"):
                yield node.lineno, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("src.agent"):
                    yield node.lineno, alias.name


# ======================================================================
# STATIC — the direction of the dependency graph
# ======================================================================

def test_the_core_does_not_import_the_agent_layer():
    """
    THE BOUNDARY, ASSERTED WHERE IT MATTERS.

    Matching, tax and decisioning compute financial outcomes. None of
    them may depend on the layer that talks to a language model, because
    "the model sits outside the financial computation" is exactly what
    such a dependency would contradict.
    """
    violations = []

    for relative, path in _core_files():
        for lineno, module in _agent_imports(path):
            if (relative, module) in EXEMPTIONS:
                continue
            violations.append(f"{relative}:{lineno} imports {module}")

    assert not violations, (
        "the deterministic core imports the AI layer:\n  "
        + "\n  ".join(violations)
        + "\n\nThe agent layer may depend on the core. The core may not "
        "depend on the agent layer. If this import is deliberate, add it "
        "to EXEMPTIONS with the reasoning, rather than deleting this test."
    )


def test_every_exemption_still_exists():
    """
    An exemption for an import that has since been removed is dead
    permission -- it would silently allow that module to reintroduce the
    dependency later without anyone noticing.
    """
    for relative, module in EXEMPTIONS:
        path = ROOT / relative
        assert path.exists(), f"exempted file {relative} no longer exists"

        found = any(m == module for _, m in _agent_imports(path))
        assert found, (
            f"{relative} no longer imports {module} -- delete this "
            "exemption rather than leaving it as dead permission"
        )


def test_the_exempted_import_is_still_deferred():
    """
    The exemption is tolerable ONLY because the import is function-local.

    A module-level `from src.agent...` at the same site would load the
    agent package whenever anything imported candidates.py, which would
    make the boundary violation real rather than latent. This asserts the
    import stays inside a function body.
    """
    path = ROOT / "src/matching/candidates.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # Collect the line ranges of every top-level statement that is NOT a
    # function or class definition -- i.e. genuine module scope.
    module_level_lines = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            if hasattr(sub, "lineno"):
                module_level_lines.add(sub.lineno)

    for lineno, module in _agent_imports(path):
        assert lineno not in module_level_lines, (
            f"candidates.py:{lineno} imports {module} at MODULE level. "
            "The exemption assumes a deferred, function-local import; a "
            "module-level one loads the agent package for every consumer "
            "of the matching layer."
        )


# ======================================================================
# RUNTIME — the property that actually matters
# ======================================================================

def test_importing_the_core_does_not_load_the_agent_package():
    """
    THE STRONGER CHECK.

    Static analysis says where the text sits. This says what actually
    happens: importing the whole deterministic pipeline must not pull the
    agent package -- and therefore not the provider SDK -- into memory.

    Run in a subprocess with a clean interpreter, because the test
    session itself imports agent modules for other tests and would
    pollute sys.modules.
    """
    program = (
        "import sys; "
        "import src.ingestion.loader, src.normalization.engine, "
        "src.matching.candidates, src.matching.scoring, src.matching.engine, "
        "src.tax.validator, src.tax.seller_ledger, "
        "src.exceptions.manager, src.exceptions.decision_table, "
        "src.financial, src.models, src.config; "
        "leaked = sorted(m for m in sys.modules if m.startswith('src.agent')); "
        "print(','.join(leaked))"
    )

    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )

    assert completed.returncode == 0, completed.stderr[-1500:]

    leaked = [m for m in completed.stdout.strip().split(",") if m]

    assert not leaked, (
        f"importing the deterministic core loaded {leaked} -- the "
        "function-local import in candidates.py has become eager, or a "
        "new module-level dependency was added"
    )


def test_importing_the_core_does_not_load_a_provider_sdk():
    """
    The same property one level out: no google.genai in memory after
    importing the pipeline. Cheap, and it is the failure a reviewer would
    actually notice -- a reconciliation engine that cannot start without
    an LLM SDK installed.
    """
    program = (
        "import sys; "
        "import src.exceptions.manager, src.matching.engine; "
        "leaked = sorted(m for m in sys.modules "
        "                if m.startswith('google.genai') or m == 'google'); "
        "print(','.join(leaked))"
    )

    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )

    assert completed.returncode == 0, completed.stderr[-1500:]
    assert not [m for m in completed.stdout.strip().split(",") if m], (
        "importing the core loaded a provider SDK"
    )


# ======================================================================
# THE REVERSE DIRECTION IS ALLOWED, AND SHOULD BE
# ======================================================================

def test_the_agent_layer_may_depend_on_the_core():
    """
    Asserted so the rule above is not mistaken for "these two must never
    touch". The tool layer READS decide_batch() output; that dependency
    is the architecture working, not a violation.
    """
    query_tools = (SRC / "agent/tools/query_tools.py").read_text(encoding="utf-8")

    assert "from src.exceptions.manager import" in query_tools
    assert "from src.matching.engine import" in query_tools
    assert "from src.financial import" in query_tools


# ======================================================================
# THE SECOND DIRECTION — THE ANSWER KEY MUST NOT REACH THE ENGINE
# ======================================================================
#
# README.md: "Ground truth is generated alongside the data and never read
# by the pipeline." ARCHITECTURE.md repeats it. Every accuracy number in
# the repository depends on it -- an engine that can see the answer key
# is not being measured, it is being graded on a test it wrote.
#
# The property held before this test existed. It held by convention.
# The import-direction boundary above is enforced structurally and this
# one was not, which is the same asymmetry FAILURE_LOG.md section 63
# records for the guardrail sweep.

GROUND_TRUTH_MARKERS = ("ground_truth", "GroundTruthRecord")

# src/models.py DEFINES GroundTruthRecord. Defining the type is not
# reading the file -- the evaluation scripts need somewhere to get the
# contract from, and models.py is where every contract in this system
# lives. What matters is that no pipeline module LOADS the data.
DEFINITION_ONLY = {"src/models.py"}


def test_the_pipeline_never_reads_ground_truth():
    """
    No module under src/ may reference the answer key.

    Checked as text rather than by import graph on purpose: the failure
    mode worth catching is a path string -- open("data/ground_truth.json")
    -- which no import analysis would see.
    """
    offenders = []

    for relative, path in _core_files():
        if relative in DEFINITION_ONLY:
            continue

        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            code = line.split("#", 1)[0]
            for marker in GROUND_TRUTH_MARKERS:
                if marker in code:
                    offenders.append(f"{relative}:{lineno} {line.strip()}")

    assert not offenders, (
        "the deterministic pipeline references ground truth:\n  "
        + "\n  ".join(offenders)
        + "\n\nEvery accuracy figure in this repository assumes the "
        "engine cannot see the answer key. If a module genuinely needs "
        "the GroundTruthRecord type, add it to DEFINITION_ONLY with the "
        "reasoning -- do not delete this test."
    )


def test_the_agent_layer_never_reads_ground_truth_either():
    """
    The tool layer answers operator questions from `decide_batch()`
    output. A tool that could read ground truth would let the agent
    report the expected answer instead of the produced one, which is the
    same defect one layer out and harder to notice because the number
    would look better.
    """
    offenders = []

    for path in sorted((SRC / "agent").rglob("*.py")):
        relative = str(path.relative_to(ROOT)).replace("\\", "/")

        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            code = line.split("#", 1)[0]
            if any(m in code for m in GROUND_TRUTH_MARKERS):
                offenders.append(f"{relative}:{lineno} {line.strip()}")

    assert not offenders, (
        "the agent layer references ground truth:\n  "
        + "\n  ".join(offenders)
    )


def test_the_ground_truth_file_exists_so_the_check_is_not_vacuous():
    """
    THE CONTROL.

    Both tests above pass trivially if `ground_truth` is simply not a
    concept in this repository any more -- a renamed file would make the
    guard green and meaningless. This asserts there is something real
    being kept out, and that the evaluation layer genuinely reads it.
    """
    assert (ROOT / "data" / "ground_truth.json").exists(), (
        "data/ground_truth.json is missing -- the two tests above are "
        "now asserting the absence of something that does not exist"
    )

    readers = [
        path.name
        for path in sorted((ROOT / "scripts").glob("*.py"))
        if "ground_truth" in path.read_text(encoding="utf-8")
    ]
    assert readers, (
        "no script reads ground truth -- if the evaluation layer stopped "
        "using the answer key, the accuracy numbers have no source"
    )
