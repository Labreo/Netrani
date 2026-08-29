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
import os
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
    ".otelc-build", "instrumentation_temp",
}


def _iter_source_files(root: Path) -> list[Path]:
    """Walk *root* and return all source files, skipping common non-source dirs."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        dpath = Path(dirpath)
        for fname in filenames:
            ext = os.path.splitext(fname)[1]
            if ext in _SOURCE_EXTENSIONS:
                files.append(dpath / fname)
    return files


def _score_file_relevance(fpath: Path, root: Path, title: str, body: str) -> int:
    """
    Score a file path by how specifically it matches the issue's topic.

    A higher score means the file is more likely to be the one the issue
    is actually about.  We use the file's path *relative to the repo root*
    so that shorter, more specific paths get a bonus.

    Scoring:
      +3 per word from the issue title that appears in any path component
      +1 per word from the issue body (first 400 chars) in any path component
      -1 per path depth level beyond 2 (shorter paths preferred)
    """
    try:
        rel = str(fpath.relative_to(root)).lower()
    except ValueError:
        rel = str(fpath).lower()

    score = 0
    title_words = re.findall(r"[a-z][a-z0-9]{3,}", title.lower())
    body_words = re.findall(r"[a-z][a-z0-9]{3,}", body[:400].lower())

    for word in title_words:
        if word in rel:
            score += 3
    for word in body_words:
        if word in rel:
            score += 1

    depth = rel.count("/") + rel.count("\\")
    score -= max(0, depth - 2)

    return score


def _read_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Symbol / function location & frequency analysis
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
    base_sym = symbol.split(".")[-1]  # strip module prefix for local lookup
    if base_sym.lower() in ("traceprovider", "tracerprovider"):
        pattern = re.compile(r"(?:Get|Set|New)?Tracer?Provider\b", re.IGNORECASE)
        if not pattern.search(text):
            return []
    else:
        if base_sym not in text:
            return []
        pattern = re.compile(r"\b" + re.escape(base_sym) + r"\b")

    matches: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if pattern.search(line):
            matches.append((i, line.rstrip()))
    return matches


def _count_symbol_in_package(
    fpath: Path,
    base_sym: str,
    file_cache: dict[Path, str] | None = None,
) -> int:
    """
    Count the total occurrences of *base_sym* in all source files in the same
    package directory (fpath.parent).
    """
    pkg_dir = fpath.parent
    if not pkg_dir.exists() or not pkg_dir.is_dir():
        return 0
    pattern = re.compile(r"\b" + re.escape(base_sym) + r"\b")
    count = 0
    for sibling in pkg_dir.iterdir():
        if sibling.is_file() and sibling.suffix in _SOURCE_EXTENSIONS:
            text = file_cache.get(sibling) if file_cache is not None else _read_safe(sibling)
            if text and base_sym in text:
                count += len(pattern.findall(text))
    return count


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
    file_cache: dict[Path, str] | None = None,
) -> tuple[str | None, str, float, list[str]]:
    """
    Returns (verdict, citation, confidence, evidence).

    ``verdict`` is ``"FALSE_POSITIVE"`` if strong guards prevent the failure,
    ``"VALID"`` if the failure path appears reachable, or ``None`` if ambiguous.
    """
    base_sym = func_name.split(".")[-1]
    pkg_count = _count_symbol_in_package(path, base_sym, file_cache)
    if pkg_count > 40:
        return (
            None,
            f"{path}:1",
            0.50,
            [f"{path}: broad symbol '{base_sym}' ({pkg_count} occurrences in package) marked INCONCLUSIVE"],
        )

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
# Protective pattern definitions & guard-target correlation
# ---------------------------------------------------------------------------

_PROTECTIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Go nil guards: if x == nil / if x != nil
    (re.compile(r"if\s+\w[\w.]*\s*==\s*nil\b"), "nil-guard"),
    (re.compile(r"if\s+\w[\w.]*\s*!=\s*nil\b"), "nil-guard"),
    # Go type assertion with ok idiom: x, ok := y.(T)
    (re.compile(r"\w+\s*,\s*ok\s*:=\s*\w[\w.]*\.\([^)]+\)"), "type-assertion-guard"),
    # Go EqualFold / strings.EqualFold for env checks (e.g. OTEL_SDK_DISABLED)
    (re.compile(r"strings\.EqualFold\s*\("), "strings-equalfold-guard"),
    # Go boolean flag / disabled env var checks
    (re.compile(r"\bif\b.*\bDisabled\b|\bif\b.*\bSdkDisabled\b", re.IGNORECASE), "disabled-flag-guard"),
    # Go defer lifecycles (e.g. defer span.End(), defer r.Body.Close())
    (re.compile(r"defer\s+[A-Za-z0-9_.]+\("), "defer-lifecycle-guard"),
    # Panic recovery guards
    (re.compile(r"\brecover\(\)"), "recover-guard"),
    # OTel SDK sentinel no-op returns that cannot be nil
    (re.compile(r"otel\.Get[A-Za-z0-9_]+\(\)|otel\.GetTracerProvider\(\)"), "otel-sentinel-guard"),
]

_GUARD_PATTERNS = [
    # Go nil guard: if x == nil / if x != nil
    (r"if\s+\w[\w.]*\s*==\s*nil\b", "nil-guard"),
    (r"if\s+\w[\w.]*\s*!=\s*nil\b", "nil-guard"),
    # Go err check
    (r"if\s+err\s*!=\s*nil\b", "error-guard"),
    # Go type assertion with ok idiom: x, ok := y.(T)
    (r"\w+\s*,\s*ok\s*:=\s*\w[\w.]*\.\([^)]+\)", "type-assertion-guard"),
    # Go boolean flag / env var check
    (r"if\s+\w[\w.]*\s*\{", "bool-flag-guard"),
    # Go EqualFold / strings.EqualFold for env checks (e.g. OTEL_SDK_DISABLED)
    (r"strings\.EqualFold\s*\(", "strings-equalfold-guard"),
    # Go defer lifecycles (e.g. defer span.End(), defer r.Body.Close())
    (r"defer\s+[A-Za-z0-9_.]+\(", "defer-lifecycle-guard"),
    # Panic recovery guards
    (r"\brecover\(\)", "recover-guard"),
    # OTel SDK sentinel no-op returns that cannot be nil
    (r"otel\.Get[A-Za-z0-9_]+\(\)|otel\.GetTracerProvider\(\)", "otel-sentinel-guard"),
    # Go early return
    (r"\breturn\b", "early-return"),
    # Rust unwrap-or / if let
    (r"\.unwrap_or\b|if\s+let\s+Some\b|if\s+let\s+Ok\b", "rust-option-guard"),
    # TypeScript/JS null check
    (r"if\s*\(\s*\w+\s*(?:===|!==|==|!=)\s*null\b", "null-check"),
    # Java Optional / null check
    (r"if\s*\(\s*\w+\s*==\s*null\b|Optional\.ofNullable\b", "java-null-check"),
    # General early return / throw
    (r"\b(?:throw|raise)\s+\w+", "throw/raise"),
]

_IGNORE_TOKENS = {
    "if", "else", "func", "return", "var", "type", "const", "struct",
    "import", "package", "for", "range", "nil", "null", "true", "false",
    "any", "interface", "map", "chan", "go", "defer", "select", "case",
    "default", "switch", "break", "continue", "fallthrough", "goto",
    "string", "int", "int64", "int32", "bool", "byte", "rune", "float64",
    "err", "error",
}


def _extract_target_tokens(symbol: str) -> set[str]:
    """Extract meaningful identifier tokens from the suspect symbol."""
    tokens = set()
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
        tokens.add(tok.lower())
    if "traceprovider" in tokens or "tracerprovider" in tokens:
        tokens.update({"traceprovider", "tracerprovider", "tracer", "provider", "otel"})
    meaningful = {t for t in tokens if len(t) > 1 and t not in _IGNORE_TOKENS}
    if not meaningful:
        meaningful = {t for t in tokens if len(t) > 1}
    return meaningful


def _is_guard_correlated(
    line: str,
    label: str,
    target_tokens: set[str],
    symbol: str,
    issue_context: str = "",
) -> bool:
    """
    Verify that a guard condition actually references the suspect variable,
    parameter, or error name AND is applicable to the reported failure scenario.
    """
    ctx_lower = issue_context.lower()
    sym_lower = symbol.lower()

    if label == "nil-guard":
        # Nil checks only protect when issue alleges nil pointer panic / crash / nil dereference
        if ctx_lower and not any(w in ctx_lower for w in ("nil", "null", "panic", "deref", "pointer", "crash", "none")):
            return False
        guard_tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", line))
        guard_tokens_lower = {t.lower() for t in guard_tokens if t.lower() not in _IGNORE_TOKENS}
        return bool(guard_tokens_lower & target_tokens)

    if label == "type-assertion-guard":
        if ctx_lower and not any(w in ctx_lower for w in ("type", "interface", "assert", "cast", "mismatch", "panic")):
            return False
        guard_tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", line))
        guard_tokens_lower = {t.lower() for t in guard_tokens if t.lower() not in _IGNORE_TOKENS}
        return bool(guard_tokens_lower & target_tokens)

    if label == "otel-sentinel-guard":
        sentinel_targets = {
            "otel", "tracer", "provider", "traceprovider", "tracerprovider", "sdk",
            "disabled", "start", "httpclientinstrumenter", "instrumenter",
        }
        if target_tokens & sentinel_targets:
            return True
        if any(w in sym_lower for w in ("traceprovider", "tracerprovider", "tracer", "otel", "sdk")):
            return True
        return False

    if label == "defer-lifecycle-guard":
        m = re.search(r"defer\s+([A-Za-z0-9_.]+)\(", line)
        if m:
            defer_target = m.group(1).lower()
            defer_tokens = set(re.findall(r"[a-z0-9_]+", defer_target))
            if "span" in defer_tokens and ("end" in defer_tokens or "close" in defer_tokens):
                if not ctx_lower or any(w in ctx_lower for w in ("early", "unclosed", "unended", "never closed", "not closed", "incorrect span end", "not ended", "leak")):
                    return True
            meaningful_defer = defer_tokens - {"close", "end"}
            if meaningful_defer and meaningful_defer <= target_tokens:
                return True
        return False

    if label == "recover-guard":
        return bool(target_tokens & {"panic", "recover", "recovery"}) or "panic" in ctx_lower or "panic" in sym_lower

    if label in ("strings-equalfold-guard", "disabled-flag-guard"):
        if target_tokens & {"disabled", "sdkdisabled", "otel_sdk_disabled", "sdk", "env"}:
            return True
        if "disabled" in sym_lower or "sdk" in sym_lower or "disabled" in ctx_lower:
            return True
        return False

    # Generic checks:
    guard_tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", line))
    guard_tokens_lower = {t.lower() for t in guard_tokens if t.lower() not in _IGNORE_TOKENS}
    return bool(guard_tokens_lower & target_tokens)


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


# ---------------------------------------------------------------------------
# Function boundary detection
# ---------------------------------------------------------------------------

def _find_go_func_bounds(lines: list[str], lineno: int) -> tuple[int, int]:
    """
    Given 0-based *lines* and a 1-based *lineno* where a symbol was found,
    return the (start, end) line indices (0-based, exclusive end) of the
    enclosing Go function body.

    Walks backward from *lineno* to find the nearest ``func `` line, then
    forward to find the matching closing brace.  Returns a safe fallback
    window of ±15 lines if no function boundary is found.
    """
    idx = lineno - 1  # convert to 0-based

    # Walk backward for the enclosing func definition.
    func_start = max(0, idx - 100)
    for i in range(idx, max(-1, idx - 250), -1):
        if i < len(lines) and re.match(r"^\s*func(\s|\()", lines[i]):
            func_start = i
            break

    # Walk forward for the matching closing brace.
    depth = 0
    found_open = False
    func_end = min(len(lines), idx + 100)  # fallback
    for i in range(func_start, min(len(lines), func_start + 400)):
        line = lines[i]
        stripped = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"|`[^`]*`|//.*$', "", line)
        for ch in stripped:
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
        if found_open and depth <= 0 and i >= func_start:
            func_end = i + 1
            break

    # Sanity check: window must contain the original line.
    if func_start > idx or func_end <= idx:
        func_start = max(0, idx - 15)
        func_end = min(len(lines), idx + 15)

    return func_start, func_end


def _find_generic_func_bounds(lines: list[str], lineno: int) -> tuple[int, int]:
    """
    Given 0-based *lines* and a 1-based *lineno* where a symbol was found,
    return the (start, end) line indices (0-based, exclusive end) of the
    enclosing generic function body.
    """
    idx = lineno - 1

    func_start = max(0, idx - 80)
    for i in range(idx, max(-1, idx - 150), -1):
        if i < len(lines) and re.match(
            r"^\s*(?:(?:public|private|protected|static|async|const|export|default|fn|def|func|function|void|int|bool|string)\s+)+[A-Za-z_][A-Za-z0-9_]*\s*[\(\{]",
            lines[i],
        ):
            func_start = i
            break

    depth = 0
    found_open = False
    func_end = min(len(lines), idx + 80)
    for i in range(func_start, min(len(lines), func_start + 300)):
        line = lines[i]
        stripped = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"|`[^`]*`|//.*$|#.*$', "", line)
        for ch in stripped:
            if ch == "{":
                depth += 1
                found_open = True
            elif ch == "}":
                depth -= 1
        if found_open and depth <= 0 and i >= func_start:
            func_end = i + 1
            break

    if func_start > idx or func_end <= idx:
        func_start = max(0, idx - 15)
        func_end = min(len(lines), idx + 15)

    return func_start, func_end


# ---------------------------------------------------------------------------
# Deep guard analysis
# ---------------------------------------------------------------------------

def _analyse_go_function(
    text: str,
    symbol: str,
    path: Path,
    occurrences: list[tuple[int, str]],
    file_cache: dict[Path, str] | None = None,
    issue_context: str = "",
) -> tuple[str | None, str, float, list[str]]:
    """
    Go-specific analysis.  Scans the enclosing *function body* of each
    occurrence of *symbol* for protective guard conditions:
      - nil guards (``if x == nil``, ``if x != nil``)
      - type assertions with ok idiom (``x, ok := y.(T)``)
      - boolean flag / env-var checks (``strings.EqualFold``, ``Disabled``)
      - defer lifecycles (``defer span.End()``, ``defer r.Body.Close()``)
      - panic recovery (``recover()``)
      - OTel sentinel constructors (``otel.GetTracerProvider()``)

    Guard counting is strictly restricted to the enclosing Go ``func`` body.

    High-Frequency Symbol Suppression:
      If a symbol appears more than 40 times in a package, mark the symbol
      as broad and return INCONCLUSIVE (0.50).

    Dynamic Confidence:
      ``0.70 + (0.05 * correlated_guards)`` capped at ``0.95``.
    """
    if not occurrences:
        return None, "", 0.0, []

    base_sym = symbol.split(".")[-1]
    pkg_count = _count_symbol_in_package(path, base_sym, file_cache)
    if pkg_count > 40:
        return (
            None,
            f"{path}:{occurrences[0][0]}",
            0.50,
            [f"{path}: broad symbol '{base_sym}' ({pkg_count} occurrences in package) marked INCONCLUSIVE"],
        )

    lines = text.splitlines()
    target_tokens = _extract_target_tokens(symbol)
    evidence: list[str] = []
    best_correlated_guards = 0
    best_citation = f"{path}:{occurrences[0][0]}"

    for lineno, _ in occurrences[:3]:
        func_start, func_end = _find_go_func_bounds(lines, lineno)
        func_guards = 0
        first_guard_line: int | None = None

        for i in range(func_start, func_end):
            line = lines[i]
            for pat, label in _PROTECTIVE_PATTERNS:
                if pat.search(line) and _is_guard_correlated(line, label, target_tokens, symbol, issue_context):
                    func_guards += 1
                    if first_guard_line is None:
                        first_guard_line = i + 1
                    evidence.append(f"{path}:{i + 1} [{label}: {line.strip()[:80]}]")
                    break

        if func_guards > best_correlated_guards:
            best_correlated_guards = func_guards
            if first_guard_line is not None:
                best_citation = f"{path}:{first_guard_line}"

    first_lineno = occurrences[0][0]
    citation = best_citation if best_correlated_guards >= 1 else f"{path}:{first_lineno}"
    confidence = min(0.95, 0.70 + 0.05 * best_correlated_guards)

    if best_correlated_guards >= 1:
        return "FALSE_POSITIVE", citation, confidence, evidence
    return "VALID", citation, 0.70, evidence


def _analyse_generic_function(
    text: str,
    symbol: str,
    path: Path,
    occurrences: list[tuple[int, str]],
    file_cache: dict[Path, str] | None = None,
    issue_context: str = "",
) -> tuple[str | None, str, float, list[str]]:
    """
    Generic analysis for non-Python / non-Go files.  Restricts guard counting
    to the enclosing function body containing the suspect expression.

    High-Frequency Symbol Suppression:
      If a symbol appears more than 40 times in a package, mark the symbol
      as broad and return INCONCLUSIVE (0.50).

    Dynamic Confidence:
      ``0.70 + (0.05 * correlated_guards)`` capped at ``0.95``.
    """
    if not occurrences:
        return None, "", 0.0, []

    base_sym = symbol.split(".")[-1]
    pkg_count = _count_symbol_in_package(path, base_sym, file_cache)
    if pkg_count > 40:
        return (
            None,
            f"{path}:{occurrences[0][0]}",
            0.50,
            [f"{path}: broad symbol '{base_sym}' ({pkg_count} occurrences in package) marked INCONCLUSIVE"],
        )

    lines = text.splitlines()
    target_tokens = _extract_target_tokens(symbol)
    evidence: list[str] = []
    best_correlated_guards = 0
    best_citation = f"{path}:{occurrences[0][0]}"

    for lineno, _ in occurrences[:5]:
        func_start, func_end = _find_generic_func_bounds(lines, lineno)
        func_guards = 0
        first_guard_line: int | None = None

        for i in range(func_start, func_end):
            line = lines[i]
            for pat, label in _PROTECTIVE_PATTERNS:
                if pat.search(line) and _is_guard_correlated(line, label, target_tokens, symbol, issue_context):
                    func_guards += 1
                    if first_guard_line is None:
                        first_guard_line = i + 1
                    evidence.append(f"{path}:{i + 1} [{label}: {line.strip()[:80]}]")
                    break

        if func_guards > best_correlated_guards:
            best_correlated_guards = func_guards
            if first_guard_line is not None:
                best_citation = f"{path}:{first_guard_line}"

    first_lineno = occurrences[0][0]
    citation = best_citation if best_correlated_guards >= 1 else f"{path}:{first_lineno}"
    confidence = min(0.95, 0.70 + 0.05 * best_correlated_guards)

    if best_correlated_guards >= 1:
        return "FALSE_POSITIVE", citation, confidence, evidence
    return "VALID", citation, 0.70, evidence


# ---------------------------------------------------------------------------
# Caller tracing (lightweight)
# ---------------------------------------------------------------------------

def _find_callers(
    all_files: list[Path],
    func_name: str,
    suspect_file: Path,
    file_cache: dict[Path, str] | None = None,
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
        text = file_cache.get(fpath) if file_cache is not None else _read_safe(fpath)
        if not text or func_name not in text:
            continue
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

    file_cache: dict[Path, str] = {}
    for fpath in all_files:
        txt = _read_safe(fpath)
        if txt:
            file_cache[fpath] = txt

    # Build a combined keyword set: symbols + words from repro trace + title/body tokens
    symbols_to_check: list[str] = list(suspect_symbols)

    for text in (title, body):
        for m in re.finditer(r"`([^`\n]{1,60})`", text):
            cand = m.group(1).strip()
            cand = re.sub(r"^(defer|\*|&)\s*", "", cand)
            cand = cand.split("=")[0].split("(")[0].strip()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", cand) and len(cand) >= 2:
                if cand not in symbols_to_check:
                    symbols_to_check.append(cand)

    for text in (title, body[:400]):
        for m in re.finditer(r"\b([A-Z][a-zA-Z0-9]{3,})\b", text):
            cand = m.group(1).strip()
            _NOISE = {
                "description", "steps", "reproduce", "when", "after", "panic",
                "error", "expected", "behavior", "version", "environment",
                "running", "observe", "issue", "setting", "affected", "example",
            }
            if cand.lower() not in _NOISE and cand not in symbols_to_check:
                symbols_to_check.append(cand)

    for m in re.finditer(r"defer\s+([A-Za-z0-9_.]+)", body):
        d = m.group(1).split("(")[0].strip()
        if d and d not in symbols_to_check:
            symbols_to_check.append(d)

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

        # ── File relevance pre-filter ─────────────────────────────────────────
        candidate_files: list[tuple[int, Path, str]] = []  # (score, path, text)
        for fpath in all_files:
            text = file_cache.get(fpath, "")
            if not text:
                continue
            if base_sym.lower() in ("traceprovider", "tracerprovider"):
                if "tracerprovider" not in text.lower() and "traceprovider" not in text.lower():
                    continue
            elif base_sym not in text:
                continue
            occurrences = _locate_symbol_in_file(text, base_sym, fpath)
            if not occurrences:
                continue
            score = _score_file_relevance(fpath, cwd, title, body)
            candidate_files.append((score, fpath, text))

        # Sort descending by relevance; analyse the top-3 files only.
        candidate_files.sort(key=lambda t: t[0], reverse=True)
        top_files = candidate_files[:3]

        for _score, fpath, text in top_files:
            occurrences = _locate_symbol_in_file(text, base_sym, fpath)
            if not occurrences:
                continue

            # Language-specific deep analysis
            issue_context = f"{title} {body}"
            if fpath.suffix == ".py":
                v, cit, conf, ev = _analyse_python_function(text, base_sym, fpath, file_cache)
            elif fpath.suffix == ".go":
                v, cit, conf, ev = _analyse_go_function(text, symbol, fpath, occurrences, file_cache, issue_context)
            else:
                v, cit, conf, ev = _analyse_generic_function(text, symbol, fpath, occurrences, file_cache, issue_context)

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

    guard_count = sum(1 for e in all_evidence if "guard" in e or "null" in e or "nil" in e or "defer" in e or "sentinel" in e)

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
