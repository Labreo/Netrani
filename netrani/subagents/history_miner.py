"""
netrani/subagents/history_miner.py
Subagent 1 — Git archaeology & duplicate/obsolete miner.

Operates in read-only mode using Git queries only.  Extracts qualified
symbols (backtick refs, CamelCase identifiers, file names with extensions,
or issue numbers) from the issue payload, runs targeted git-log searches,
and returns a structured ``HistoryFindings`` result indicating whether the
issue is ``DUPLICATE``, ``OBSOLETE``, or requires further static analysis.

Only concludes OBSOLETE when a matching commit on the default branch contains
a fix keyword (fix, bug, resolve, patch, closes) in its subject.

No file modifications are performed here.  This subagent is designed to
be executed in a concurrent.futures.ThreadPoolExecutor alongside
``static_validator.StaticFindings``.
"""

from __future__ import annotations

import re
import subprocess
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
    # Generic English
    "description", "since", "steps", "reproduce", "start", "send",
    "observe", "expected", "behavior", "behaviour", "requests", "request",
    "http", "https", "environment", "ubuntu", "version", "error", "panic",
    "failed", "failure", "crash", "true", "false", "file", "path",
    "issue", "bug", "when", "with", "that", "this", "from", "after",
    "would", "should", "using", "into", "have", "been", "then", "also",
    "can", "cannot", "the", "and", "for", "not", "are", "how", "what",
    "where", "build", "run", "use", "get", "set", "add", "new", "make",
    "does", "did", "all", "any", "nil", "null", "none", "some",
    # Project-domain stop words — too broad for this repo
    "Instrumentation", "instrumentation", "Instrumented", "instrumented",
    "Instrument", "instrument", "Instrumenter", "instrumenter",
    "Compile", "compile", "Span", "span", "Tracer", "tracer",
    "Context", "context", "Handler", "handler", "Server", "server",
    "Client", "client", "SpanName", "spanname",
})

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


def _is_qualified_keyword(kw: str) -> bool:
    """
    Return True only for keywords that are specific enough to search in git:
      - Exact backtick-quoted symbols already extracted by caller
      - CamelCase identifiers (at least two consecutive capitals or a capital
        followed by a lower-case letter run, e.g. ``ConnectionPool``, ``SetupOTel``)
      - File names with recognised extensions
      - Issue number references (#NNN)
      - SCREAMING_SNAKE_CASE env vars with underscores (≥ 2 segments)
      - dotted method paths (e.g. ``context.emptyCtx``)
    Must be ≥ 3 chars and not in STOP_WORDS.
    """
    if len(kw) < 3:
        return False
    if kw.lower() in STOP_WORDS:
        return False
    # Issue reference
    if re.match(r"^#\d+$", kw):
        return True
    # File with extension
    if re.match(r"[\w./\-]+\.(?:py|go|rs|ts|js|java|rb|cs|cpp|h)\b", kw):
        return True
    # SCREAMING_SNAKE_CASE env var (e.g. OTEL_SDK_DISABLED)
    if re.match(r"^[A-Z][A-Z0-9_]{4,}$", kw) and "_" in kw:
        return True
    # CamelCase identifier: must be multi-word (has both upper and lowercase and
    # at least one transition), and long enough (≥ 5 chars) to avoid single common
    # words like "Type", "True", "From".
    if re.match(r"^[A-Z][a-zA-Z0-9]*[a-z][a-zA-Z0-9]*$", kw) and len(kw) >= 5:
        return True
    # Method / dotted path with uppercase component
    if "." in kw and re.search(r"[A-Z]", kw):
        return True
    # snake_case identifiers with underscore (function names like ast_rewriter)
    if "_" in kw and re.match(r"^[a-z][a-z0-9_]{3,}$", kw):
        return True
    return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_git(args: list[str], cwd: str | Path, timeout: int = 30) -> str:
    """Run a git command and return stdout; return '' on any failure."""
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

    Only returns backticked symbols, CamelCase identifiers, file names with
    extensions, issue number references, SCREAMING_SNAKE env vars, or
    snake_case function names.  Generic English words and markdown section
    headers are excluded via STOP_WORDS and _is_qualified_keyword().

    The list is capped at 12 entries to keep git commands fast.
    """
    seen: set[str] = set()
    keywords: list[str] = []

    def _add(kw: str) -> None:
        kw = kw.strip().strip("`")
        if kw and kw not in seen and _is_qualified_keyword(kw):
            seen.add(kw)
            keywords.append(kw)

    # 1. Trusted suspect symbols from parser (backtick refs)
    for sym in suspect_symbols:
        _add(sym)

    full_text = f"{title}\n{body}"

    # 2. Backtick-quoted identifiers from body/title.
    # For short backtick terms (3-6 chars) bypass the qualified-keyword filter
    # since they may be important domain terms like "cgo".
    # For longer terms, also extract the leading word if it looks like a tool/cmd name.
    for m in re.finditer(r"`([^`\n]{2,80})`", full_text):
        raw = m.group(1).strip()
        if 3 <= len(raw) <= 6 and re.match(r"^[a-zA-Z][a-zA-Z0-9_-]+$", raw):
            # Short backtick term — direct add if not in stop words
            if raw.lower() not in STOP_WORDS and raw not in seen:
                seen.add(raw)
                keywords.append(raw)
        else:
            _add(raw)
            # Also try the first word of a backtick error message as a tool name
            first_word = re.match(r"^([a-z][a-z0-9]{2,5}):", raw)
            if first_word:
                fw = first_word.group(1)
                if fw.lower() not in STOP_WORDS and fw not in seen:
                    seen.add(fw)
                    keywords.append(fw)

    # 3. Issue references (#NNN) from body
    for m in re.finditer(r"#(\d{3,})", full_text):
        _add(f"#{m.group(1)}")

    # 4. File paths with Go/code extensions
    for m in re.finditer(r"[\w./\-]+\.(?:py|go|rs|ts|js|java|rb|cs|cpp|h)\b", full_text):
        _add(m.group(0))

    # 5. CamelCase identifiers from title
    for m in re.finditer(r"\b([A-Z][a-zA-Z0-9]{2,})\b", title):
        _add(m.group(1))

    # 6. SCREAMING_SNAKE env vars
    for m in re.finditer(r"\b(OTEL_[A-Z0-9_]{4,})\b", full_text):
        _add(m.group(1))

    # 7. Rare lowercase technical terms from title (≥ 7 chars, not common English).
    # These capture domain-specific terms like "variadic", "toolexec",
    # "goroutine", "deadlock" which are specific enough to be useful git queries.
    # We bypass _is_qualified_keyword for these since they are all-lowercase.
    _TITLE_TECHNICAL = re.compile(r"\b([a-z][a-z0-9]{6,})\b")
    _COMMON_LOWERCASE = frozenset({
        # Generic English verbs/adjectives/nouns (too broad for git queries)
        "function", "package", "parameter", "produces", "generates",
        "multiple", "wrappers", "contains", "incorrect", "silently",
        "triggers", "invocations", "returns", "registered", "concurrent",
        "attribute", "missing", "another", "between", "correctly",
        "produces", "instrumented", "invalid", "identifier", "versions",
        "exporter", "connection", "shutdown", "process", "packages",
        "attributes", "handlers", "invalid", "methods", "functions",
        "returning", "corrupt",
    })
    for m in _TITLE_TECHNICAL.finditer(title):
        w = m.group(1)
        if w not in _COMMON_LOWERCASE and w.lower() not in STOP_WORDS:
            # Direct add without _is_qualified_keyword filter
            w = w.strip()
            if w and w not in seen:
                seen.add(w)
                keywords.append(w)

    return keywords[:12]


def _commit_has_fix_keyword(commit_line: str) -> bool:
    """
    Return True if the commit subject line contains a recognized fix keyword AND
    is not a documentation-only or housekeeping commit (docs:, chore:, test:, ci:).
    """
    if _NON_CODE_PREFIXES.search(commit_line):
        return False
    return bool(_FIX_KEYWORDS.search(commit_line))


def _commit_subject_is_relevant(commit_line: str, keywords: list[str]) -> bool:
    """
    Return True if the commit subject overlaps with at least one of the search
    keywords (case-insensitive).  This prevents accepting a fix commit that merely
    *touched* a file containing the keyword but whose fix addresses a completely
    different problem.

    We match on the lowercased base part of each keyword (stripping paths and
    dotted prefixes) against the lowercased commit subject.
    """
    subject = commit_line.lower()
    for kw in keywords:
        # Strip path prefixes and dotted namespaces to get the bare term
        base = kw.split("/")[-1].split(".")[-1].lower().strip("()*`-")
        if len(base) >= 3 and re.search(r"\b" + re.escape(base) + r"\b", subject):
            return True
    return False


def _search_log_s(keyword: str, cwd: Path) -> list[str]:
    """git log -S "<keyword>" --oneline --all (pickaxe — detects add/remove)."""
    out = _run_git(["log", "-S", keyword, "--oneline", "--all", "--max-count=10"], cwd)
    return [line for line in out.splitlines() if line]


def _search_log_g(pattern: str, cwd: Path) -> list[str]:
    """git log -G "<pattern>" --oneline --all (regex diff search)."""
    out = _run_git(["log", "-G", pattern, "--oneline", "--all", "--max-count=10"], cwd)
    return [line for line in out.splitlines() if line]


def _search_log_grep(keyword: str, cwd: Path) -> list[str]:
    """git log --all --grep="<keyword>" --oneline (commit message search)."""
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
    try:
        rc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", short_sha, default_branch],
            cwd=str(cwd),
            capture_output=True,
            timeout=10,
        ).returncode
        return rc == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _inspect_commit_diff(sha: str, cwd: Path) -> str:
    """Return a condensed diff summary for *sha*."""
    short_sha = sha.split()[0]
    return _run_git(["show", short_sha, "--stat", "--no-patch"], cwd)


def _resolve_full_sha(short_sha: str, cwd: Path) -> str:
    """Resolve a short or partial SHA to the full 40-hex form."""
    out = _run_git(["rev-parse", short_sha], cwd)
    return out if len(out) == 40 else short_sha


def _compute_confidence(
    fix_keywords_in_subject: bool,
    corroborating_keywords: int,
    on_default_branch: bool,
) -> float:
    """
    Compute dynamic confidence for an OBSOLETE verdict:
      - Base: 0.60
      - +0.20 if the commit subject contains a fix keyword
      - +0.05 per additional corroborating keyword (capped at +0.10)
      - +0.05 if commit is confirmed on the default branch

    Maximum: 0.95
    """
    conf = 0.60
    if fix_keywords_in_subject:
        conf += 0.20
    conf += min(0.10, 0.05 * max(0, corroborating_keywords - 1))
    if on_default_branch:
        conf += 0.05
    return min(0.95, conf)


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
    default_branch = _get_default_branch(cwd)

    # Collect all matching commit lines keyed by short SHA.
    # Also track: does this commit have a fix keyword in its message?
    commit_hits: dict[str, list[str]] = {}     # sha_prefix -> [evidence lines]
    commit_has_fix: dict[str, bool] = {}        # sha_prefix -> fix-keyword found
    sha_keyword_count: dict[str, int] = {}      # sha_prefix -> distinct keyword matches

    for kw in keywords:
        for line in _search_log_s(kw, cwd):
            sha = line.split()[0]
            commit_hits.setdefault(sha, []).append(f"pickaxe({kw!r}): {line}")
            commit_has_fix[sha] = commit_has_fix.get(sha, False) or _commit_has_fix_keyword(line)
            sha_keyword_count[sha] = sha_keyword_count.get(sha, 0) + 1

        for line in _search_log_g(kw, cwd):
            sha = line.split()[0]
            commit_hits.setdefault(sha, []).append(f"diff-grep({kw!r}): {line}")
            commit_has_fix[sha] = commit_has_fix.get(sha, False) or _commit_has_fix_keyword(line)
            sha_keyword_count[sha] = sha_keyword_count.get(sha, 0) + 1

        for line in _search_log_grep(kw, cwd):
            sha = line.split()[0]
            commit_hits.setdefault(sha, []).append(f"commit-msg({kw!r}): {line}")
            commit_has_fix[sha] = commit_has_fix.get(sha, False) or _commit_has_fix_keyword(line)
            sha_keyword_count[sha] = sha_keyword_count.get(sha, 0) + 1

        # Changelog scan — if found, treat as a strong OBSOLETE signal
        cl_hit = _search_changelog_for_duplicates(kw, cwd)
        if cl_hit:
            findings.evidence.append(f"changelog({kw!r}): {cl_hit}")

    # Flatten evidence for reporting
    for sha, lines in commit_hits.items():
        findings.evidence.extend(lines[:3])  # keep at most 3 per SHA

    if not commit_hits:
        findings.verdict = None
        findings.rationale = (
            "No commits on any branch reference the qualified symbols or identifiers "
            "extracted from this issue. History check is inconclusive; proceeding "
            "to static code analysis."
        )
        findings.confidence = 0.0
        return findings

    # Evaluate each SHA — only accept OBSOLETE when commit has a fix keyword
    # AND the commit subject is relevant to the issue keywords.
    for sha, evidence_lines in commit_hits.items():
        on_default = _commit_on_default_branch(sha, default_branch, cwd)
        has_fix = commit_has_fix.get(sha, False)

        # Extract the raw commit subject — the "sha subject" part — from the
        # evidence line, which has format:
        #   "pickaxe('context.Background()'): abc1234 fix(tool): ..."
        # We find the first 7-40 hex char SHA and take from there.
        raw_commit_line = ""
        if evidence_lines:
            raw = evidence_lines[0]
            sha_match = re.search(r"\b([0-9a-f]{7,40})\s+\S", raw)
            raw_commit_line = raw[sha_match.start():] if sha_match else raw

        relevant = _commit_subject_is_relevant(raw_commit_line, keywords)

        if on_default and has_fix and relevant:
            full_sha = _resolve_full_sha(sha, cwd)
            diff_stat = _inspect_commit_diff(sha, cwd)
            corroborating = sha_keyword_count.get(sha, 1)
            confidence = _compute_confidence(
                fix_keywords_in_subject=True,
                corroborating_keywords=corroborating,
                on_default_branch=True,
            )
            findings.verdict = "OBSOLETE"
            findings.citation = full_sha if len(full_sha) == 40 else sha
            findings.rationale = (
                f"Commit {findings.citation[:12]} on branch '{default_branch}' contains "
                "a fix keyword in its subject and modifies code paths referenced in this "
                f"issue. The defect appears to have been addressed. "
                f"Diff summary: {diff_stat[:200].strip()}"
            )
            findings.confidence = confidence
            findings.evidence.extend(evidence_lines)
            return findings

    # Commits exist on main but without fix keywords — inconclusive from Tier 1
    # (do not emit OBSOLETE without explicit fix evidence)
    on_main_no_fix = [
        sha for sha, ev in commit_hits.items()
        if _commit_on_default_branch(sha, default_branch, cwd) and not commit_has_fix.get(sha, False)
    ]
    off_branch_shas = [
        sha for sha in commit_hits
        if not _commit_on_default_branch(sha, default_branch, cwd)
    ]

    if on_main_no_fix:
        # Commits on main match keywords but no fix keyword — inconclusive
        findings.verdict = None
        findings.rationale = (
            f"Commits matching the issue keywords exist on '{default_branch}' but none "
            "contain a recognized fix keyword (fix, resolve, closes, patch). Cannot "
            "conclude OBSOLETE without explicit fix evidence; proceeding to static analysis."
        )
        findings.confidence = 0.0
        return findings

    if off_branch_shas:
        # Off-branch only — likely an open PR / duplicate
        sha = off_branch_shas[0]
        full_sha = _resolve_full_sha(sha, cwd)
        findings.verdict = "DUPLICATE"
        findings.citation = (
            issue_url
            if issue_url
            else f"commit {full_sha[:12]} (not yet merged to {default_branch})"
        )
        findings.rationale = (
            f"Commit(s) matching the issue keywords exist on branches other than "
            f"'{default_branch}', suggesting an open PR or unmerged fix already tracks "
            "this root cause. Manual confirmation of the linked PR/issue is recommended."
        )
        findings.confidence = 0.70
        findings.evidence.extend(commit_hits[sha][:3])
        return findings

    # Fallback: inconclusive
    findings.verdict = None
    findings.rationale = (
        "History check produced no definitive fix evidence. Proceeding to static analysis."
    )
    findings.confidence = 0.0
    return findings
