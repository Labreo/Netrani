"""
netrani/subagents/history_miner.py
Subagent 1 — Git archaeology & duplicate/obsolete miner.

Operates in read-only mode using Git queries only.  Extracts keywords
(symbol names, error strings, file paths) from the issue payload, runs
targeted git-log searches, and returns a structured ``HistoryFindings``
result indicating whether the issue is ``DUPLICATE``, ``OBSOLETE``, or
requires further static analysis.

No file modifications are performed here.  This subagent is designed to
be executed in a concurrent.futures.ThreadPoolExecutor alongside
``static_validator.HistoryFindings``.
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
    Derive a deduplicated, ranked list of search keywords from the issue payload.

    Priority order:
      1. Suspect symbols extracted by the issue fetcher (backtick refs, CamelCase identifiers).
      2. Function / method call patterns from the raw body (``foo(``).
      3. Error-string fragments: capitalised words following "Error:", "Exception:", "panic:".
      4. File-path fragments (contain ``/`` or ``.py``, ``.go``, ``.rs``, etc.).
      5. Title words (≥ 5 chars, no stop-words).

    The list is capped at 15 entries to keep git commands fast.
    """
    seen: set[str] = set()
    keywords: list[str] = []

    def _add(kw: str) -> None:
        kw = kw.strip()
        if kw and kw not in seen and len(kw) >= 3:
            seen.add(kw)
            keywords.append(kw)

    # 1. Trusted suspect symbols from parser
    for sym in suspect_symbols:
        _add(sym)

    full_text = f"{title}\n{body}"

    # 2. Explicit function-call patterns: word(
    for m in re.finditer(r"\b([a-zA-Z_][a-zA-Z0-9_]{2,})\s*\(", full_text):
        _add(m.group(1))

    # 3. Error strings
    for m in re.finditer(
        r"(?:Error|Exception|panic|FATAL|failed?|crash)\s*[:\s]+([^\n.]{3,60})",
        full_text,
        re.IGNORECASE,
    ):
        fragment = m.group(1).strip().strip('"').strip("'")
        if fragment:
            _add(fragment[:60])

    # 4. File paths
    for m in re.finditer(r"[\w./\-]+\.(?:py|go|rs|ts|js|java|rb|cs|cpp|h)\b", full_text):
        _add(m.group(0))

    # 5. Significant title words
    _STOP = {
        "the", "and", "for", "with", "that", "this", "from", "not", "when",
        "are", "bug", "issue", "error", "using", "into", "after", "would",
    }
    for word in re.split(r"\W+", title):
        if len(word) >= 5 and word.lower() not in _STOP:
            _add(word)

    return keywords[:15]


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
    # git merge-base --is-ancestor exits 0 if ancestor, 1 if not, '' stdout either way
    # We run with subprocess directly to capture the return code
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


def _search_changelog_for_duplicates(
    keyword: str, cwd: Path
) -> str:
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

    # Collect all matching commit lines keyed by short SHA
    commit_hits: dict[str, list[str]] = {}  # sha_prefix -> [evidence lines]

    for kw in keywords:
        for line in _search_log_s(kw, cwd):
            sha = line.split()[0]
            commit_hits.setdefault(sha, []).append(f"pickaxe({kw!r}): {line}")

        for line in _search_log_g(kw, cwd):
            sha = line.split()[0]
            commit_hits.setdefault(sha, []).append(f"diff-grep({kw!r}): {line}")

        for line in _search_log_grep(kw, cwd):
            sha = line.split()[0]
            commit_hits.setdefault(sha, []).append(f"commit-msg({kw!r}): {line}")

        # Changelog scan — if found, treat as a strong OBSOLETE signal
        cl_hit = _search_changelog_for_duplicates(kw, cwd)
        if cl_hit:
            findings.evidence.append(f"changelog({kw!r}): {cl_hit}")

    # Flatten evidence for reporting
    for sha, lines in commit_hits.items():
        findings.evidence.extend(lines[:3])  # keep at most 3 per SHA

    if not commit_hits:
        # No history matches — Tier 1 inconclusive
        findings.verdict = None
        findings.rationale = (
            "No commits on any branch reference the suspect symbols or keywords "
            "extracted from this issue. History check is inconclusive; proceeding "
            "to static code analysis."
        )
        findings.confidence = 0.0
        return findings

    # Evaluate each SHA
    for sha, evidence_lines in commit_hits.items():
        on_default = _commit_on_default_branch(sha, default_branch, cwd)
        if on_default:
            full_sha = _resolve_full_sha(sha, cwd)
            diff_stat = _inspect_commit_diff(sha, cwd)
            findings.verdict = "OBSOLETE"
            findings.citation = full_sha if len(full_sha) == 40 else sha
            findings.rationale = (
                f"Commit {findings.citation[:12]} on branch '{default_branch}' modifies "
                "the same code paths referenced in this issue. The defect appears to have "
                f"been addressed in that commit. Diff summary: {diff_stat[:200].strip()}"
            )
            findings.confidence = 0.85
            findings.evidence.extend(evidence_lines)
            return findings

    # Commits exist but only off the default branch — likely an open PR / duplicate
    sha = next(iter(commit_hits))
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
