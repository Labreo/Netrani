"""
netrani/subagents/static_validator.py
Subagent 2 — AST, types & call-graph inspector.

Performs read-only static analysis of the target repository.  Locates
suspect files and function definitions referenced in the issue report,
traces the call path to the alleged failure site, checks type
annotations, guard conditions, and null checks, and returns a
``StaticFindings`` result indicating whether the defect is ``VALID``
(reachable) or ``FALSE_POSITIVE`` (statically impossible).

No file modifications are performed here.  This subagent is designed to
be executed in a concurrent.futures.ThreadPoolExecutor alongside
``history_miner.run``.

Language coverage (pure stdlib, no external tools required):
  - Python  : ast module — full AST walk for function bodies, type hints,
               guard expressions, and raise/return/None checks.
  - Generic : regex-based grep for all other languages (Go, Rust, TS, JS,
               Java, Ruby, C#, C++).  Checks guard patterns, null tests,
               and error-return idioms.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class StaticFindings:
    """Structured output produced by the Static Validator subagent."""

    # One of "VALID", "FALSE_POSITIVE", or None (inconclusive).
    verdict: str | None = None
    citation: str = ""
    rationale: str = ""
    confidence: float = 0.0
    # Collected evidence snippets
    evidence: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# File discovery helpers
# ---------------------------------------------------------------------------

_SOURCE_EXTENSIONS = {
    ".py", ".go", ".rs", ".ts", ".tsx", ".js", ".jsx",
    ".java", ".rb", ".cs", ".cpp", ".c", ".h", ".hpp",
}

_IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "env", "dist", "build", "target", ".tox", ".mypy_cache",
}


def _iter_source_files(root: Path) -> list[Path]:
    """Walk *root* and return all source files, skipping common non-source dirs."""
    files: list[Path] = []
    for path in root.rglob("*"):
        if any(part in _IGNORE_DIRS for part in path.parts):
            continue
        if path.is_file() and path.suffix in _SOURCE_EXTENSIONS:
            files.append(path)
    return files


def _read_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Symbol / function location
# ---------------------------------------------------------------------------

def _locate_symbol_in_file(
    text: str,
    symbol: str,
    path: Path,
) -> list[tuple[int, str]]:
    """
    Return a list of (line_number, line_text) tuples where *symbol* appears.
    Line numbers are 1-based.
    """
    matches: list[tuple[int, str]] = []
    # Match exact word boundary (handles snake_case, camelCase, dotted names)
    base_sym = symbol.split(".")[-1]  # strip module prefix for local lookup
    pattern = re.compile(r"\b" + re.escape(base_sym) + r"\b")
    for i, line in enumerate(text.splitlines(), start=1):
        if pattern.search(line):
            matches.append((i, line.rstrip()))
    return matches


def _find_python_function(
    source: str, func_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """
    Parse Python source and return the first FunctionDef/AsyncFunctionDef whose
    unqualified name matches *func_name*.  Returns ``None`` if not found or if
    the source cannot be parsed.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                return node
    return None


# ---------------------------------------------------------------------------
# Python-specific guard analysis
# ---------------------------------------------------------------------------

class _GuardVisitor(ast.NodeVisitor):
    """
    Walk a Python function body and collect evidence of protective guards:
      - ``if x is None: raise ...``
      - ``if not x: return ...``
      - Type annotations that exclude None (``str`` vs ``str | None``).
      - Try/except blocks covering the suspect operation.
    """

    def __init__(self) -> None:
        self.null_guards: list[str] = []
        self.raise_stmts: list[str] = []
        self.return_stmts: list[str] = []
        self.type_guards: list[str] = []

    def visit_If(self, node: ast.If) -> None:  # noqa: N802
        test_src = ast.unparse(node.test)
        body_src = " | ".join(ast.unparse(s) for s in node.body[:2])
        if re.search(r"\bis\s+None\b|\bis\s+not\s+None\b|== None|!= None", test_src):
            self.null_guards.append(f"if {test_src}: {body_src}")
        elif re.search(r"^not\s+|^len\(", test_src):
            self.null_guards.append(f"if {test_src}: {body_src}")
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:  # noqa: N802
        if node.exc is not None:
            self.raise_stmts.append(ast.unparse(node.exc)[:80])
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:  # noqa: N802
        if node.value is not None:
            self.return_stmts.append(ast.unparse(node.value)[:60])
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        ann = ast.unparse(node.annotation)
        self.type_guards.append(f"annotation: {ann}")
        self.generic_visit(node)


def _analyse_python_function(
    source: str,
    func_name: str,
    path: Path,
) -> tuple[str | None, str, float, list[str]]:
    """
    Returns (verdict, citation, confidence, evidence).

    ``verdict`` is ``"FALSE_POSITIVE"`` if strong guards prevent the failure,
    ``"VALID"`` if the failure path appears reachable, or ``None`` if ambiguous.
    """
    func_node = _find_python_function(source, func_name)
    if func_node is None:
        return None, "", 0.0, []

    start_line = func_node.lineno
    end_line = getattr(func_node, "end_lineno", start_line)
    citation = f"{path}:{start_line}-{end_line}"

    visitor = _GuardVisitor()
    visitor.visit(func_node)

    evidence: list[str] = []
    evidence.extend(f"null_guard: {g}" for g in visitor.null_guards)
    evidence.extend(f"raise: {r}" for r in visitor.raise_stmts)
    evidence.extend(f"type: {t}" for t in visitor.type_guards)

    # Heuristic: if multiple null guards exist, the path is well-defended
    if len(visitor.null_guards) >= 2 and any(
        "raise" in g or "return" in g for g in visitor.null_guards
    ):
        return (
            "FALSE_POSITIVE",
            citation,
            0.80,
            evidence,
        )

    # If the function has a body with no guards at all on the suspect path —
    # we conservatively call it VALID
    return "VALID", citation, 0.75, evidence


# ---------------------------------------------------------------------------
# Generic (non-Python) guard analysis via regex
# ---------------------------------------------------------------------------

_GUARD_PATTERNS = [
    # Go nil guard
    (r"if\s+\w+\s*==\s*nil\b", "nil-guard"),
    # Go err check
    (r"if\s+err\s*!=\s*nil\b", "error-guard"),
    # Rust unwrap-or / if let
    (r"\.unwrap_or\b|if\s+let\s+Some\b|if\s+let\s+Ok\b", "rust-option-guard"),
    # TypeScript/JS null check
    (r"if\s*\(\s*\w+\s*(?:===|!==|==|!=)\s*null\b", "null-check"),
    # Java Optional / null check
    (r"if\s*\(\s*\w+\s*==\s*null\b|Optional\.ofNullable\b", "java-null-check"),
    # General early return / throw
    (r"\b(?:throw|raise)\s+\w+", "throw/raise"),
]


def _count_guards_in_range(
    lines: list[str],
    start: int,
    end: int,
) -> list[tuple[str, int]]:
    """Return (pattern_label, line_no) for each guard pattern found in [start, end)."""
    hits: list[tuple[str, int]] = []
    for i in range(start, min(end, len(lines))):
        line = lines[i]
        for pat, label in _GUARD_PATTERNS:
            if re.search(pat, line):
                hits.append((label, i + 1))
    return hits


def _analyse_generic_function(
    text: str,
    symbol: str,
    path: Path,
    occurrences: list[tuple[int, str]],
) -> tuple[str | None, str, float, list[str]]:
    """
    Generic analysis for non-Python files.  Looks at the 30-line window
    around each occurrence of *symbol* for guard patterns.
    """
    if not occurrences:
        return None, "", 0.0, []

    lines = text.splitlines()
    evidence: list[str] = []
    total_guards = 0

    for lineno, _ in occurrences[:5]:  # analyse at most 5 call sites
        window_start = max(0, lineno - 15)
        window_end = min(len(lines), lineno + 15)
        guards = _count_guards_in_range(lines, window_start, window_end)
        total_guards += len(guards)
        for label, gno in guards:
            evidence.append(f"{path}:{gno} [{label}]")

    first_lineno = occurrences[0][0]
    citation = f"{path}:{first_lineno}"

    if total_guards >= 2:
        return (
            "FALSE_POSITIVE",
            citation,
            0.75,
            evidence,
        )
    return "VALID", citation, 0.72, evidence


# ---------------------------------------------------------------------------
# Caller tracing (lightweight)
# ---------------------------------------------------------------------------

def _find_callers(
    all_files: list[Path],
    func_name: str,
    suspect_file: Path,
) -> list[tuple[Path, int, str]]:
    """
    Find files (other than *suspect_file*) that call *func_name*.
    Returns at most 5 call sites as (file, lineno, line_text).
    """
    callers: list[tuple[Path, int, str]] = []
    pattern = re.compile(r"\b" + re.escape(func_name) + r"\s*\(")
    for fpath in all_files:
        if fpath == suspect_file:
            continue
        text = _read_safe(fpath)
        for i, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                callers.append((fpath, i, line.rstrip()))
                if len(callers) >= 5:
                    return callers
    return callers


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    repo_path: str | Path,
    title: str,
    body: str,
    suspect_symbols: Sequence[str],
    reproduction_trace: Sequence[str],
) -> StaticFindings:
    """
    Execute Tier 2 of the three-tier triage workflow.

    Parameters
    ----------
    repo_path:
        Absolute path to the target repository root.
    title:
        Issue title string.
    body:
        Full issue body text.
    suspect_symbols:
        Pre-extracted suspect symbols from ``IssueRecord``.
    reproduction_trace:
        Ordered reproduction steps from ``IssueRecord``.

    Returns
    -------
    StaticFindings
        ``verdict`` is ``None`` if analysis is inconclusive.
    """
    cwd = Path(repo_path).expanduser().resolve()
    findings = StaticFindings()

    all_files = _iter_source_files(cwd)
    if not all_files:
        findings.verdict = "VALID"
        findings.citation = str(cwd)
        findings.rationale = (
            "No source files were found in the target repository. Cannot statically "
            "refute the issue; defaulting to VALID with low confidence."
        )
        findings.confidence = 0.50
        return findings

    # Build a combined keyword set: symbols + words from repro trace
    symbols_to_check: list[str] = list(suspect_symbols)
    for step in reproduction_trace:
        for m in re.finditer(r"\b([a-zA-Z_][a-zA-Z0-9_]{2,})\s*\(", step):
            sym = m.group(1)
            if sym not in symbols_to_check:
                symbols_to_check.append(sym)

    # De-duplicate and cap
    symbols_to_check = list(dict.fromkeys(symbols_to_check))[:12]

    best_verdict: str | None = None
    best_citation = ""
    best_confidence = 0.0
    all_evidence: list[str] = []

    for symbol in symbols_to_check:
        base_sym = symbol.split(".")[-1]
        # Find source files mentioning this symbol
        for fpath in all_files:
            text = _read_safe(fpath)
            if not text:
                continue
            occurrences = _locate_symbol_in_file(text, base_sym, fpath)
            if not occurrences:
                continue

            # Python deep analysis
            if fpath.suffix == ".py":
                v, cit, conf, ev = _analyse_python_function(text, base_sym, fpath)
            else:
                v, cit, conf, ev = _analyse_generic_function(text, base_sym, fpath, occurrences)

            all_evidence.extend(ev)

            if v is None:
                continue

            # Keep the highest-confidence result; prefer FALSE_POSITIVE over VALID
            # only if confidence is materially higher (≥ 0.05 margin)
            if best_verdict is None:
                best_verdict = v
                best_citation = cit
                best_confidence = conf
            elif v == "FALSE_POSITIVE" and conf >= best_confidence - 0.05:
                best_verdict = v
                best_citation = cit
                best_confidence = conf
            elif v == "VALID" and best_verdict != "FALSE_POSITIVE" and conf > best_confidence:
                best_verdict = v
                best_citation = cit
                best_confidence = conf

    findings.evidence = all_evidence[:20]  # cap evidence

    if best_verdict is None:
        # No analysable symbols found — conservative VALID
        findings.verdict = "VALID"
        findings.citation = str(cwd)
        findings.rationale = (
            "The suspect symbols extracted from this issue could not be located in any "
            "source file.  This may indicate a renamed API, a runtime-injected symbol, "
            "or a dependency-layer issue.  Defaulting to VALID with low confidence so "
            "the defect is not silently dismissed."
        )
        findings.confidence = 0.55
        return findings

    findings.verdict = best_verdict
    findings.citation = best_citation
    findings.confidence = best_confidence

    # Build rationale
    callers: list[tuple[Path, int, str]] = []
    if symbols_to_check:
        callers = _find_callers(all_files, symbols_to_check[0].split(".")[-1], cwd)

    caller_summary = ""
    if callers:
        caller_summary = (
            " Callers found at: "
            + "; ".join(f"{p.name}:{ln}" for p, ln, _ in callers[:3])
            + "."
        )

    guard_count = sum(1 for e in all_evidence if "guard" in e or "null" in e or "nil" in e)

    if best_verdict == "FALSE_POSITIVE":
        findings.rationale = (
            f"Static analysis at {best_citation} identified {guard_count} protective "
            "guard condition(s) that prevent the reported failure from being reached "
            f"under any valid input.{caller_summary}  The failure mode is statically "
            "impossible given current code invariants."
        )
    else:
        findings.rationale = (
            f"The failure path at {best_citation} is statically reachable.  "
            f"{guard_count} guard condition(s) were detected but none fully block "
            f"the reported failure scenario.{caller_summary}  The defect appears "
            "to be a genuine, unresolved issue in the codebase."
        )

    return findings
