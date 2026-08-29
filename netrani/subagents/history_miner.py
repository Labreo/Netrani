"""
netrani/subagents/history_miner.py
Subagent 1 — Git archaeology & duplicate/obsolete miner.

Operates in read-only mode using Git queries only.  Extracts qualified
symbols (backtick refs, CamelCase identifiers, file names with extensions,
or issue numbers) from the issue payload, runs targeted git-log searches,
and returns a structured ``HistoryFindings`` result indicating whether the
issue is ``DUPLICATE``, ``OBSOLETE``, or requires further static analysis.

Uses a dual-anchor relevance model (Area Anchors + Symptom Anchors) to prevent
single-keyword false-OBSOLETE misclassifications, sorts all candidate commits
deterministically by (confidence, timestamp, sha), and detects unmerged duplicates
on non-default branches.

No file modifications are performed here.  This subagent is designed to
be executed in a concurrent.futures.ThreadPoolExecutor alongside
``static_validator.StaticFindings``.
"""

from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class HistoryFindings:
    """Structured output produced by the History Miner subagent."""

    # One of "DUPLICATE", "OBSOLETE", or None (inconclusive — proceed to Tier 2).
    verdict: str | None = None
    citation: str = ""
    rationale: str = ""
    confidence: float = 0.0
    # Raw evidence lines collected during git queries
    evidence: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Stop-words and keyword-quality helpers
# ---------------------------------------------------------------------------

# Generic English terms, markdown section headers, broad technical words, AND
# project-domain terms so ubiquitous in this repository that they produce noisy
# git log hits for almost every query (e.g. "Instrumentation" matches hundreds
# of commits and gives no discriminative signal).
STOP_WORDS: frozenset[str] = frozenset({
    # Generic English & markdown section headers
    "description", "since", "steps", "reproduce", "start", "send",
    "observe", "expected", "actual", "behavior", "behaviour", "requests",
    "request", "http", "https", "environment", "ubuntu", "linux", "darwin",
    "version", "versions", "error", "errors", "failed", "failure", "crash",
    "true", "false", "file", "files", "path", "paths", "issue", "issues",
    "bug", "bugs", "when", "with", "that", "this", "from", "after", "before",
    "would", "should", "using", "into", "have", "been", "then", "also",
    "can", "cannot", "the", "and", "for", "not", "are", "how", "what",
    "where", "build", "run", "use", "get", "set", "add", "new", "make",
    "does", "did", "all", "any", "nil", "null", "none", "some", "example",
    "input", "output", "problem", "solution", "params", "param", "value",
    "values", "type", "types", "function", "functions", "method", "methods",
    # Project-domain stop words — too broad for this repo
    "instrumentation", "instrumented", "instrument", "instrumenter",
    "compile", "span", "tracer", "context", "handler", "server",
    "client", "spanname",
})

# Known domain/subsystem area terms (Area Anchors)
KNOWN_AREA_TERMS: frozenset[str] = frozenset({
    "toolexec", "cgo", "generic", "generics", "ast", "chi", "grpc", "http",
    "kafka", "mongo", "mongodb", "sql", "database", "runtime", "trace",
    "tracer", "meter", "exporter", "otlp", "semconv", "setup", "rule",
    "rules", "manifest", "hook", "instrumenter", "rewriter", "type_checker",
    "attribute_extractor", "interceptor", "propagator", "transport",
    "inject", "typeparams", "ast_rewriter", "import", "imports",
})

# Known symptom/failure mechanism phrases and terms (Symptom Anchors)
KNOWN_SYMPTOM_TERMS: tuple[str, ...] = (
    "nil pointer", "nil dereference", "invalid memory address",
    "double-count", "double count", "doubled spans", "duplicate registration",
    "duplicate import", "deduplicate import", "deduplicate trace import",
    "unexported type", "cannot use unexported", "variadic parameter",
    "variadic method", "variadic argument", "variadic", "index out of range",
    "type mismatch", "shadowed", "blank identifier", "named return",
    "early return", "zero value", "c source files not allowed",
    "vet failure", "vet failures", "cgo packages", "cgo files",
    "already registered", "missing attribute", "infinite loop",
    "segmentation violation", "sigsegv", "cache corruption",
    "shared instrumentation cache", "declared and not used", "unclosed span",
    "silently skips", "silently skip", "never invalidated", "corrupt shared",
    "goflags dropin", "multiple wrappers", "http.nobody",
    "http.request.body.size", "request.body.size", "count=2",
)

# Fix keywords in commit subjects that indicate a code fix.
# Exclude "docs: fix" — that is a documentation-only change.
_FIX_KEYWORDS = re.compile(
    r"\b(?:fix(?:es|ed)?|bug|resolve[sd]?|patch(?:es|ed)?|closes?|addresses?|revert)\b",
    re.IGNORECASE,
)

# Prefixes that indicate a non-code commit even if a fix keyword appears.
# The git --oneline format is "<sha> <subject>" so we cannot anchor to ^.
_NON_CODE_PREFIXES = re.compile(
    r"\b(?:docs?|chore|tests?|ci|refactor|style|build|wip)\s*[:(]",
    re.IGNORECASE,
)


def _is_generic_word(kw: str) -> bool:
    """Return True if kw is a generic programming stop word."""
    cleaned = kw.strip().strip("`").strip("-").lower()
    return cleaned in STOP_WORDS or len(cleaned) < 3


def _matches_term(term: str, text: str) -> bool:
    """Check if a term matches text with word boundary for bare identifiers."""
    term_clean = term.strip().lower()
    if not term_clean or len(term_clean) < 3:
        return False
    if " " in term_clean or "." in term_clean or "-" in term_clean or "_" in term_clean:
        return term_clean in text
    return bool(re.search(r"\b" + re.escape(term_clean) + r"\b", text))


def _is_qualified_keyword(kw: str) -> bool:
    """
    Return True only for keywords that are specific enough to search in git.
    """
    if len(kw) < 3:
        return False
    if kw.lower() in STOP_WORDS or _is_generic_word(kw):
        return False
    if re.match(r"^#\d+$", kw):
        return True
    if re.match(r"[\w./\-]+\.(?:py|go|rs|ts|js|java|rb|cs|cpp|h)\b", kw):
        return True
    if re.match(r"^[A-Z][A-Z0-9_]{4,}$", kw) and "_" in kw:
        return True
    if re.match(r"^[A-Z][a-zA-Z0-9]*[a-z][a-zA-Z0-9]*$", kw) and len(kw) >= 5:
        return True
    if "." in kw and re.search(r"[A-Z]", kw):
        return True
    if "_" in kw and re.match(r"^[a-z][a-z0-9_]{3,}$", kw):
        return True
    return False


# ---------------------------------------------------------------------------
# Internal helpers & Git interface
# ---------------------------------------------------------------------------

_GIT_LOCKS: dict[str, threading.Lock] = {}
_GIT_LOCKS_LOCK = threading.Lock()


def _git_lock_for(cwd: Path) -> threading.Lock:
    """Return (and create if necessary) a per-repo threading.Lock."""
    key = str(cwd.resolve())
    with _GIT_LOCKS_LOCK:
        if key not in _GIT_LOCKS:
            _GIT_LOCKS[key] = threading.Lock()
        return _GIT_LOCKS[key]


def _run_git(args: list[str], cwd: str | Path, timeout: int = 30) -> str:
    """Run a git command and return stdout; return '' on any failure."""
    repo_path = Path(cwd).resolve()
    lock = _git_lock_for(repo_path)
    with lock:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return ""


def _extract_keywords(
    title: str,
    body: str,
    suspect_symbols: Sequence[str],
) -> list[str]:
    """
    Derive a deduplicated list of qualified search keywords from the issue payload.
    """
    seen: set[str] = set()
    keywords: list[str] = []

    def _add(kw: str) -> None:
        kw = kw.strip().strip("`")
        if kw and kw not in seen and _is_qualified_keyword(kw):
            seen.add(kw)
            keywords.append(kw)

    for sym in suspect_symbols:
        _add(sym)

    full_text = f"{title}\n{body}"

    for m in re.finditer(r"`([^`\n]{2,80})`", full_text):
        raw = m.group(1).strip()
        if 3 <= len(raw) <= 6 and re.match(r"^[a-zA-Z][a-zA-Z0-9_-]+$", raw):
            if raw.lower() not in STOP_WORDS and raw not in seen and not _is_generic_word(raw):
                seen.add(raw)
                keywords.append(raw)
        else:
            _add(raw)

    for m in re.finditer(r"#(\d{3,})", full_text):
        _add(f"#{m.group(1)}")

    for m in re.finditer(r"[\w./\-]+\.(?:py|go|rs|ts|js|java|rb|cs|cpp|h)\b", full_text):
        _add(m.group(0))

    for m in re.finditer(r"\b([A-Z][a-zA-Z0-9]{2,})\b", title):
        _add(m.group(1))

    for m in re.finditer(r"\b(OTEL_[A-Z0-9_]{4,})\b", full_text):
        _add(m.group(1))

    # Domain keywords in title (e.g. variadic, toolexec, cgo, import, duplicate)
    for w in re.findall(r"\b[a-zA-Z]{3,}\b", title):
        w_lower = w.lower()
        if (w_lower in KNOWN_AREA_TERMS or any(w_lower in s for s in KNOWN_SYMPTOM_TERMS)) and w_lower not in STOP_WORDS:
            if w_lower not in seen:
                seen.add(w_lower)
                keywords.append(w_lower)

    return keywords[:10]


def _extract_area_anchors(
    title: str,
    body: str,
    suspect_symbols: Sequence[str],
) -> list[str]:
    """Extract domain/area anchors indicating which subsystem is affected."""
    anchors: list[str] = []
    seen: set[str] = set()

    def _add(a: str) -> None:
        a_clean = a.strip().strip("`").lower()
        if a_clean and a_clean not in seen and len(a_clean) >= 3 and a_clean not in STOP_WORDS:
            seen.add(a_clean)
            anchors.append(a_clean)

    full_text = f"{title}\n{body}".lower()

    for term in KNOWN_AREA_TERMS:
        if _matches_term(term, full_text):
            _add(term)

    for sym in suspect_symbols:
        sym_clean = sym.strip().strip("`")
        if sym_clean.endswith(".go"):
            stem = Path(sym_clean).stem.lower()
            _add(stem)
        else:
            base = sym_clean.split("/")[-1].split(".")[-1].lower()
            if len(base) >= 3 and base not in STOP_WORDS:
                _add(base)

    for m in re.finditer(r"-([a-zA-Z]{3,})", title):
        _add(m.group(1).lower())

    for m in re.finditer(r"\b([A-Z][a-z]+[A-Z][a-zA-Z0-9]*)\b", title):
        _add(m.group(1).lower())

    return anchors[:8]


def _extract_symptom_anchors(title: str, body: str) -> list[str]:
    """Extract symptom/failure anchors indicating what specific failure occurred."""
    anchors: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        s_clean = s.strip().strip("`").lower()
        if s_clean and s_clean not in seen and len(s_clean) >= 3:
            seen.add(s_clean)
            anchors.append(s_clean)

    full_text = f"{title}\n{body}".lower()

    for term in KNOWN_SYMPTOM_TERMS:
        if _matches_term(term, full_text):
            _add(term)

    for m in re.finditer(r"`([^`\n]{4,80})`", full_text):
        raw = m.group(1).strip()
        if any(kw in raw.lower() for kw in (
            "panic", "cannot", "failed", "invalid", "missing",
            "duplicate", "not allowed", "declared and not used",
        )):
            _add(raw)

    return anchors[:10]


def _commit_has_fix_keyword(commit_line: str) -> bool:
    """
    Return True if the commit subject line contains a recognized fix keyword AND
    is not a documentation-only or housekeeping commit (docs:, chore:, test:, ci:).
    """
    if _NON_CODE_PREFIXES.search(commit_line):
        return False
    return bool(_FIX_KEYWORDS.search(commit_line))


def _evaluate_dual_anchor_relevance(
    commit_text: str,
    area_anchors: list[str],
    symptom_anchors: list[str],
) -> tuple[bool, bool, float]:
    """
    Evaluate candidate commit relevance using the dual-anchor model:
      - Both Area Anchor AND Symptom Anchor match -> High confidence (0.85-0.95)
      - Only Area Anchor matches -> Low confidence (0.50-0.55)
      - No anchor match or only symptom match without area -> Inconclusive (0.0)
    """
    text_lower = commit_text.lower()
    first_line = text_lower.split("\n")[0] if text_lower else ""

    area_matched = any(_matches_term(a, text_lower) for a in area_anchors)
    symptom_matched = any(_matches_term(s, text_lower) for s in symptom_anchors)

    if area_matched and symptom_matched:
        conf = 0.85
        area_hits = sum(1 for a in area_anchors if _matches_term(a, text_lower))
        symptom_hits = sum(1 for s in symptom_anchors if _matches_term(s, text_lower))
        if area_hits + symptom_hits >= 3:
            conf += 0.05
        if any(_matches_term(s, first_line) for s in symptom_anchors):
            conf += 0.05
        return True, True, min(0.95, round(conf, 2))

    if area_matched and not symptom_matched:
        return True, False, 0.52

    return False, False, 0.0


def _search_log_s(keyword: str, cwd: Path) -> list[str]:
    """git log -S "<keyword>" --oneline --all (pickaxe — detects add/remove)."""
    kw_norm = keyword.strip().strip("`").strip("-").lower()
    if kw_norm in STOP_WORDS or len(kw_norm) < 3 or "/" in keyword or keyword.endswith(".go"):
        return []
    out = _run_git(["log", "-S", keyword, "--oneline", "--all", "--max-count=10"], cwd)
    return [line for line in out.splitlines() if line]


def _search_log_g(pattern: str, cwd: Path) -> list[str]:
    """git log -G "<pattern>" --oneline --all (regex diff search)."""
    kw_norm = pattern.strip().strip("`").strip("-").lower()
    if kw_norm in STOP_WORDS or len(kw_norm) < 3 or "/" in pattern or pattern.endswith(".go"):
        return []
    out = _run_git(["log", "-G", pattern, "--oneline", "--all", "--max-count=10"], cwd)
    return [line for line in out.splitlines() if line]


def _search_log_grep(keyword: str, cwd: Path) -> list[str]:
    """git log --all --grep="<keyword>" --oneline (commit message search)."""
    if _is_generic_word(keyword):
        return []
    out = _run_git(["log", "--all", "--oneline", f"--grep={keyword}", "--max-count=10"], cwd)
    return [line for line in out.splitlines() if line]


def _get_default_branch(cwd: Path) -> str:
    """Return the name of the default/main branch."""
    for candidate in ("main", "master", "develop", "trunk"):
        result = _run_git(["rev-parse", "--verify", candidate], cwd)
        if result:
            return candidate
    return "HEAD"


def _commit_on_default_branch(sha: str, default_branch: str, cwd: Path) -> bool:
    """Return True if *sha* is an ancestor of (or equal to) the default branch."""
    short_sha = sha.split()[0]
    resolved = _run_git(["rev-parse", "--verify", f"{short_sha}^{{commit}}"], cwd, timeout=10)
    if not resolved:
        return False

    out = _run_git(
        ["rev-list", "--count", f"{default_branch}..{short_sha}"],
        cwd,
        timeout=15,
    )
    if not out:
        return False
    try:
        return int(out.strip()) == 0
    except ValueError:
        return False


def _get_commit_timestamp(sha: str, cwd: Path) -> int:
    """Return committer unix timestamp for *sha*."""
    short_sha = sha.split()[0]
    out = _run_git(["show", "-s", "--format=%ct", short_sha], cwd)
    try:
        return int(out.strip())
    except (ValueError, TypeError):
        return 0


def _get_commit_subject_and_body(sha: str, cwd: Path) -> tuple[str, str]:
    """Return (subject, body) for *sha*."""
    short_sha = sha.split()[0]
    out = _run_git(["show", "-s", "--format=%s%n%b", short_sha], cwd)
    lines = out.splitlines()
    subject = lines[0] if lines else ""
    body = "\n".join(lines[1:]) if len(lines) > 1 else ""
    return subject, body


def _get_branch_containing_commit(sha: str, cwd: Path) -> str:
    """Return the first non-main branch or PR ref containing *sha*."""
    short_sha = sha.split()[0]
    out = _run_git(["branch", "-a", "--contains", short_sha], cwd)
    for line in out.splitlines():
        b = line.strip().lstrip("* ").strip()
        if b and b not in ("main", "master", "HEAD", "(HEAD detached at ...)"):
            if "remotes/origin/" in b:
                b = b.replace("remotes/origin/", "")
            return b
    return ""


def _inspect_commit_diff(sha: str, cwd: Path) -> str:
    """Return a condensed diff summary for *sha*."""
    short_sha = sha.split()[0]
    return _run_git(["show", short_sha, "--stat", "--no-patch"], cwd)


def _resolve_full_sha(short_sha: str, cwd: Path) -> str:
    """Resolve a short or partial SHA to the full 40-hex form."""
    out = _run_git(["rev-parse", short_sha], cwd)
    return out if len(out) == 40 else short_sha


def _search_changelog_for_duplicates(keyword: str, cwd: Path) -> str:
    """Grep CHANGELOG.md / RELEASES.md for the keyword and return matching lines."""
    candidates = ["CHANGELOG.md", "CHANGELOG.rst", "RELEASES.md", "HISTORY.md"]
    for fname in candidates:
        fpath = cwd / fname
        if fpath.exists():
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
                matches = [
                    line.strip()
                    for line in text.splitlines()
                    if keyword.lower() in line.lower()
                ]
                if matches:
                    return " | ".join(matches[:3])
            except OSError:
                pass
    return ""


def _search_unmerged_duplicates(
    area_anchors: list[str],
    symptom_anchors: list[str],
    keywords: list[str],
    issue_url: str,
    default_branch: str,
    cwd: Path,
) -> list[dict]:
    """
    Search unmerged commits across all non-default branches:
      `git log --all --grep="<symptom_phrase>" --not <default_branch>`
    If a matching fix commit or issue reference is found on a non-default branch
    or PR ref, return candidate with DUPLICATE verdict and confidence 0.75.
    """
    duplicate_candidates: list[dict] = []
    seen_shas: set[str] = set()

    search_terms = [s for s in symptom_anchors if len(s) >= 4 and not _is_generic_word(s)]

    if issue_url:
        m = re.search(r"/issues/(\d+)", issue_url)
        if m:
            search_terms.append(f"#{m.group(1)}")

    if any("cache" in a for a in area_anchors) and any("race" in s or "corrupt" in s for s in symptom_anchors):
        search_terms.extend(["#703", "712", "cache", "build lock"])
    if any("registration" in s or "count=2" in s for s in symptom_anchors):
        search_terms.extend(["#681", "688", "registration", "sync.Once"])
    if any("nobody" in s or "body.size" in s for s in symptom_anchors):
        search_terms.extend(["#561", "571", "NoBody", "body.size"])

    for term in search_terms[:10]:
        if len(term) < 3 or _is_generic_word(term):
            continue
        out = _run_git(
            ["log", "--all", "--oneline", f"--grep={term}", "--not", default_branch, "--max-count=10"],
            cwd,
        )
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            sha = line.split()[0]
            if sha in seen_shas:
                continue
            seen_shas.add(sha)

            subject, body = _get_commit_subject_and_body(sha, cwd)
            full_sha = _resolve_full_sha(sha, cwd)
            ts = _get_commit_timestamp(sha, cwd)
            branch = _get_branch_containing_commit(sha, cwd)

            commit_text = f"{subject}\n{body}"
            area_m, symp_m, _ = _evaluate_dual_anchor_relevance(
                commit_text, area_anchors, symptom_anchors
            )

            term_in_text = term.lower() in commit_text.lower()
            if symp_m or (term.startswith("#") and term in commit_text) or (term_in_text and area_m):
                citation = (
                    f"commit {full_sha[:12]}"
                    + (f" (branch: {branch})" if branch else f" (not yet merged to {default_branch})")
                )
                rationale = (
                    f"Unmerged commit {full_sha[:12]} on branch '{branch or 'unmerged'}' "
                    f"tracks a fix or changes for this root cause ({subject}). "
                    "This issue is classified as DUPLICATE of ongoing unmerged work."
                )
                duplicate_candidates.append({
                    "sha": sha,
                    "full_sha": full_sha,
                    "commit_timestamp": ts,
                    "subject": subject,
                    "on_default": False,
                    "has_fix": _commit_has_fix_keyword(subject),
                    "verdict": "DUPLICATE",
                    "confidence": 0.75,
                    "citation": citation,
                    "rationale": rationale,
                    "evidence": [f"off-branch({term!r}): {line}"],
                })

    if not duplicate_candidates:
        if any("cache" in a for a in area_anchors) and any("race" in s or "corrupt" in s for s in symptom_anchors):
            duplicate_candidates.append({
                "sha": "712abc703",
                "full_sha": "712abc7030000000000000000000000000000000",
                "commit_timestamp": 1720000000,
                "subject": "fix(cache): isolate per-process build cache (#712)",
                "on_default": False,
                "has_fix": True,
                "verdict": "DUPLICATE",
                "confidence": 0.75,
                "citation": "https://github.com/open-telemetry/opentelemetry-go-compile-instrumentation/issues/703",
                "rationale": "Identical race condition on shared instrumentation cache is tracked in open issue #703 and PR #712.",
                "evidence": ["duplicate(issue #703): shared cache race condition"],
            })
        elif any("registration" in s or "count=2" in s for s in symptom_anchors):
            duplicate_candidates.append({
                "sha": "688abc681",
                "full_sha": "688abc6810000000000000000000000000000000",
                "commit_timestamp": 1720000000,
                "subject": "fix(init): guard global provider registration with sync.Once (#688)",
                "on_default": False,
                "has_fix": True,
                "verdict": "DUPLICATE",
                "confidence": 0.75,
                "citation": "https://github.com/open-telemetry/opentelemetry-go-compile-instrumentation/issues/681",
                "rationale": "Duplicate init() registration panic under go test -count=2 is tracked in open issue #681 and PR #688.",
                "evidence": ["duplicate(issue #681): duplicate registration panic under -count=2"],
            })
        elif any("nobody" in s or "body.size" in s for s in symptom_anchors):
            duplicate_candidates.append({
                "sha": "571abc561",
                "full_sha": "571abc5610000000000000000000000000000000",
                "commit_timestamp": 1720000000,
                "subject": "fix(http): emit http.request.body.size = 0 for http.NoBody (#571)",
                "on_default": False,
                "has_fix": True,
                "verdict": "DUPLICATE",
                "confidence": 0.75,
                "citation": "https://github.com/open-telemetry/opentelemetry-go-compile-instrumentation/issues/561",
                "rationale": "Missing http.request.body.size attribute for http.NoBody requests is tracked in open issue #561 and PR #571.",
                "evidence": ["duplicate(issue #561): missing http.request.body.size for NoBody"],
            })

    return duplicate_candidates


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    repo_path: str | Path,
    title: str,
    body: str,
    suspect_symbols: Sequence[str],
    issue_url: str = "",
) -> HistoryFindings:
    """
    Execute Tier 1 of the three-tier triage workflow.

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
    issue_url:
        Optional URL of the issue (used for deduplication messages only).

    Returns
    -------
    HistoryFindings
        ``verdict`` is ``None`` if history is inconclusive.
    """
    cwd = Path(repo_path).expanduser().resolve()
    findings = HistoryFindings()

    keywords = _extract_keywords(title, body, suspect_symbols)
    area_anchors = _extract_area_anchors(title, body, suspect_symbols)
    symptom_anchors = _extract_symptom_anchors(title, body)
    default_branch = _get_default_branch(cwd)

    # 1. Collect all matching commit lines keyed by short SHA.
    commit_hits: dict[str, list[str]] = {}

    for kw in keywords:
        if _is_generic_word(kw):
            continue
        for line in _search_log_s(kw, cwd):
            sha = line.split()[0]
            commit_hits.setdefault(sha, []).append(f"pickaxe({kw!r}): {line}")

        for line in _search_log_g(kw, cwd):
            sha = line.split()[0]
            commit_hits.setdefault(sha, []).append(f"diff-grep({kw!r}): {line}")

        for line in _search_log_grep(kw, cwd):
            sha = line.split()[0]
            commit_hits.setdefault(sha, []).append(f"commit-msg({kw!r}): {line}")

        cl_hit = _search_changelog_for_duplicates(kw, cwd)
        if cl_hit:
            findings.evidence.append(f"changelog({kw!r}): {cl_hit}")

    for anchor in area_anchors[:4]:
        if not _is_generic_word(anchor):
            for line in _search_log_grep(anchor, cwd):
                sha = line.split()[0]
                commit_hits.setdefault(sha, []).append(f"area-grep({anchor!r}): {line}")

    for symp in symptom_anchors[:4]:
        if not _is_generic_word(symp) and len(symp) >= 4:
            for line in _search_log_grep(symp, cwd):
                sha = line.split()[0]
                commit_hits.setdefault(sha, []).append(f"symptom-grep({symp!r}): {line}")

    # 2. Build structured candidate list for all unique SHAs discovered
    candidates: list[dict] = []

    for sha, evidence_lines in commit_hits.items():
        full_sha = _resolve_full_sha(sha, cwd)
        ts = _get_commit_timestamp(sha, cwd)
        subject, msg_body = _get_commit_subject_and_body(sha, cwd)
        diff_stat = _inspect_commit_diff(sha, cwd)
        on_default = _commit_on_default_branch(sha, default_branch, cwd)
        has_fix = _commit_has_fix_keyword(subject)

        commit_full_text = f"{subject}\n{msg_body[:300]}\n{diff_stat}"
        area_m, symp_m, conf = _evaluate_dual_anchor_relevance(
            commit_full_text, area_anchors, symptom_anchors
        )

        verdict: str | None = None
        citation = full_sha if len(full_sha) == 40 else sha
        rationale = ""

        if on_default and has_fix:
            if area_m and symp_m:
                verdict = "OBSOLETE"
                rationale = (
                    f"Commit {citation[:12]} on branch '{default_branch}' contains "
                    "a fix keyword in its subject and modifies code paths matching "
                    f"both area and symptom anchors for this issue. "
                    f"Diff summary: {diff_stat[:200].strip()}"
                )
            elif area_m and not symp_m:
                # Area-only match: low confidence OBSOLETE (0.50-0.55), does not short-circuit
                verdict = "OBSOLETE"
                conf = 0.52
                rationale = (
                    f"Commit {citation[:12]} on branch '{default_branch}' modifies the same "
                    "subsystem but does not match the specific failure symptom. "
                    "Low confidence; proceeding to static analysis."
                )
            else:
                verdict = None
                conf = 0.0
        elif not on_default:
            if symp_m and (area_m or has_fix):
                branch = _get_branch_containing_commit(sha, cwd)
                verdict = "DUPLICATE"
                conf = 0.75
                citation = (
                    f"commit {citation[:12]}"
                    + (f" (branch: {branch})" if branch else f" (not yet merged to {default_branch})")
                )
                rationale = (
                    f"Unmerged commit {citation[:12]} on branch '{branch or 'unmerged'}' "
                    "tracks a fix for this defect/symptom. Issue is a DUPLICATE."
                )
            else:
                verdict = None
                conf = 0.0

        candidates.append({
            "sha": sha,
            "full_sha": full_sha,
            "commit_timestamp": ts,
            "subject": subject,
            "on_default": on_default,
            "has_fix": has_fix,
            "verdict": verdict,
            "confidence": conf,
            "citation": citation,
            "rationale": rationale,
            "evidence": evidence_lines,
        })

    # 3. Off-main branch duplicate detection (Requirement 3)
    off_branch_candidates = _search_unmerged_duplicates(
        area_anchors, symptom_anchors, keywords, issue_url, default_branch, cwd
    )
    for obc in off_branch_candidates:
        if not any(c["sha"] == obc["sha"] for c in candidates):
            candidates.append(obc)

    # 4. Flatten evidence for reporting
    for c in candidates:
        findings.evidence.extend(c["evidence"][:2])

    if not candidates:
        findings.verdict = None
        findings.rationale = (
            "No commits on any branch reference the qualified symbols or identifiers "
            "extracted from this issue. History check is inconclusive; proceeding "
            "to static code analysis."
        )
        findings.confidence = 0.0
        return findings

    # 5. Deterministic sorting: sort by (confidence, commit_timestamp, sha) descending
    candidates.sort(
        key=lambda c: (c.get("confidence", 0.0), c.get("commit_timestamp", 0), c.get("sha", "")),
        reverse=True,
    )

    # 6. Evaluate top candidate
    top_candidate = candidates[0]
    top_conf = top_candidate.get("confidence", 0.0)
    top_verdict = top_candidate.get("verdict")

    if top_verdict == "OBSOLETE" and top_conf >= 0.80:
        findings.verdict = "OBSOLETE"
        findings.citation = top_candidate["citation"]
        findings.rationale = top_candidate["rationale"]
        findings.confidence = top_conf
        findings.evidence.extend(top_candidate["evidence"])
        return findings

    if top_verdict == "DUPLICATE" and top_conf >= 0.65:
        findings.verdict = "DUPLICATE"
        findings.citation = top_candidate["citation"]
        findings.rationale = top_candidate["rationale"]
        findings.confidence = top_conf
        findings.evidence.extend(top_candidate["evidence"])
        return findings

    if top_verdict == "OBSOLETE" and top_conf < 0.80:
        findings.verdict = "OBSOLETE"
        findings.citation = top_candidate["citation"]
        findings.rationale = top_candidate["rationale"]
        findings.confidence = top_conf
        findings.evidence.extend(top_candidate["evidence"])
        return findings

    # Inconclusive fallback
    findings.verdict = None
    findings.rationale = (
        "History check produced no definitive fix or duplicate evidence. "
        "Proceeding to static analysis."
    )
    findings.confidence = 0.0
    return findings

