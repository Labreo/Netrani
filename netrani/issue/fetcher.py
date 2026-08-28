"""
netrani/issue/fetcher.py
Issue ingestion engine.

Accepts a GitHub issue number, URL, or a local Markdown/JSON file and returns
a structured IssueRecord.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, TypedDict


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class IssueRecord(TypedDict):
    title: str
    body: str
    author: str
    labels: list[str]
    url: str
    source: str               # "github_cli" | "github_api" | "local_file" | "fixture"
    suspect_symbols: list[str]
    reproduction_trace: list[str]
    reported_version: str
    environment: dict[str, str]


# ---------------------------------------------------------------------------
# URL / identifier patterns
# ---------------------------------------------------------------------------

_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)"
)
_ISSUE_NUMBER_RE = re.compile(r"^\d+$")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _extract_symbols(text: str) -> list[str]:
    """
    Heuristically extract candidate symbol names from issue text.
    Looks for `backtick references`, CamelCase identifiers, and
    function/method call patterns.
    """
    symbols: list[str] = []
    seen: set[str] = set()

    # Backtick spans
    for m in re.finditer(r"`([^`\n]{2,80})`", text):
        candidate = m.group(1).strip()
        # Keep only plausible code references (no spaces unless it looks like a command)
        if re.match(r"^[A-Za-z_][A-Za-z0-9_./\-:]*(\(.*\))?$", candidate):
            _add_unique(symbols, seen, candidate)

    # CamelCase / snake_case identifiers that look like function/method names
    for m in re.finditer(r"\b([A-Z][a-zA-Z0-9]{2,}(?:\.[A-Za-z0-9_]+)*)\b", text):
        _add_unique(symbols, seen, m.group(1))

    return symbols[:20]  # cap to top-20


def _add_unique(lst: list[str], seen: set[str], value: str) -> None:
    if value not in seen:
        seen.add(value)
        lst.append(value)


def _extract_reproduction_trace(text: str) -> list[str]:
    """
    Extracts numbered or bulleted steps from a 'Steps to Reproduce' section.
    """
    steps: list[str] = []
    # Find section header variations
    section_re = re.compile(
        r"(?:steps?\s+to\s+repro(?:duce)?|reproduction\s+steps?|how\s+to\s+repro(?:duce)?)",
        re.IGNORECASE,
    )
    m = section_re.search(text)
    if not m:
        return steps
    section_text = text[m.end():]
    # Grab up to the next section header (##, **bold**, or end of string)
    next_section = re.search(r"\n(?:#{1,3}\s|\*\*[A-Z])", section_text)
    if next_section:
        section_text = section_text[: next_section.start()]

    for line in section_text.splitlines():
        line = line.strip()
        if re.match(r"^(\d+[\.\)]\s+|-\s+|\*\s+)", line):
            step = re.sub(r"^(\d+[\.\)]\s+|-\s+|\*\s+)", "", line).strip()
            if step:
                steps.append(step)
    return steps


def _extract_version(text: str) -> str:
    """Extract version string from issue body."""
    patterns = [
        r"version[:\s]+v?([\d]+\.[\d]+\.[\d]+[^\s]*)",
        r"v([\d]+\.[\d]+\.[\d]+[^\s]*)",
        r"tag[:\s]+v?([\d]+\.[\d]+\.[\d]+[^\s]*)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def _extract_environment(text: str) -> dict[str, str]:
    """Extract key/value pairs from an Environment or System Info section."""
    env: dict[str, str] = {}
    env_re = re.compile(
        r"(?:environment|system\s+info(?:rmation)?|platform|os)[:\s]*\n((?:.+\n)*)",
        re.IGNORECASE,
    )
    m = env_re.search(text)
    if not m:
        # Try flat key: value pairs anywhere in the body
        for line in text.splitlines():
            kv = re.match(r"^\s*[-*]?\s*(\w[\w\s]{1,30})\s*:\s*(.+)$", line)
            if kv:
                key = kv.group(1).strip().lower().replace(" ", "_")
                val = kv.group(2).strip()
                if key in ("os", "platform", "arch", "go_version", "node_version",
                           "python_version", "rust_version", "kernel", "distro"):
                    env[key] = val
        return env
    block = m.group(1)
    for line in block.splitlines():
        kv = re.match(r"^\s*[-*]?\s*(.+?)\s*[:\-]\s*(.+)$", line)
        if kv:
            key = kv.group(1).strip().lower().replace(" ", "_")
            env[key] = kv.group(2).strip()
    return env


def _build_record(
    title: str,
    body: str,
    author: str,
    labels: list[str],
    url: str,
    source: str,
) -> IssueRecord:
    return IssueRecord(
        title=title,
        body=body,
        author=author,
        labels=labels,
        url=url,
        source=source,
        suspect_symbols=_extract_symbols(body),
        reproduction_trace=_extract_reproduction_trace(body),
        reported_version=_extract_version(body),
        environment=_extract_environment(body),
    )


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def _fetch_via_gh_cli(owner: str, repo: str, number: str) -> IssueRecord:
    """Use the `gh` CLI to fetch issue metadata."""
    cmd = [
        "gh", "issue", "view", number,
        "--repo", f"{owner}/{repo}",
        "--json", "title,body,author,labels,url",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise RuntimeError(
            f"gh CLI failed for {owner}/{repo}#{number}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse gh CLI output: {exc}") from exc

    author_login = (data.get("author") or {}).get("login", "")
    label_names = [lbl.get("name", "") for lbl in (data.get("labels") or [])]
    return _build_record(
        title=data.get("title", ""),
        body=data.get("body", ""),
        author=author_login,
        labels=label_names,
        url=data.get("url", ""),
        source="github_cli",
    )


def _fetch_via_rest_api(owner: str, repo: str, number: str) -> IssueRecord:
    """Fallback to GitHub REST API (unauthenticated or via GITHUB_TOKEN)."""
    api_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
    req = urllib.request.Request(api_url)
    req.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"GitHub API returned {exc.code} for {owner}/{repo}#{number}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error fetching {api_url}: {exc}") from exc

    user = (data.get("user") or {}).get("login", "")
    labels = [lbl.get("name", "") for lbl in (data.get("labels") or [])]
    return _build_record(
        title=data.get("title", ""),
        body=data.get("body") or "",
        author=user,
        labels=labels,
        url=data.get("html_url", ""),
        source="github_api",
    )


def _load_local_file(path: Path) -> IssueRecord:
    """Load an issue from a local Markdown or JSON file."""
    if not path.exists():
        raise FileNotFoundError(f"Issue file not found: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
        return _build_record(
            title=data.get("title", path.stem),
            body=data.get("body", ""),
            author=data.get("author", ""),
            labels=data.get("labels", []),
            url=data.get("url", f"file://{path.resolve()}"),
            source="local_file",
        )
    # Markdown: try to parse front-matter title
    title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else path.stem
    return _build_record(
        title=title,
        body=text,
        author="",
        labels=[],
        url=f"file://{path.resolve()}",
        source="local_file",
    )


def _parse_github_ref(issue_ref: str) -> tuple[str, str, str] | None:
    """
    Return (owner, repo, number) if *issue_ref* is a GitHub URL or
    a plain numeric ID (uses the current git remote as owner/repo).
    Returns None if the ref is not recognisable as a GitHub issue.
    """
    url_m = _GITHUB_URL_RE.match(issue_ref.strip())
    if url_m:
        return url_m.group("owner"), url_m.group("repo"), url_m.group("number")
    if _ISSUE_NUMBER_RE.match(issue_ref.strip()):
        # Bare number — attempt to derive owner/repo from git remote
        try:
            remote_url = subprocess.check_output(
                ["git", "remote", "get-url", "origin"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            remote_m = re.search(
                r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$",
                remote_url,
            )
            if remote_m:
                return remote_m.group("owner"), remote_m.group("repo"), issue_ref.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_issue(issue_ref: str, offline: bool = False) -> IssueRecord:
    """
    Fetch and parse an issue from any supported source.

    Parameters
    ----------
    issue_ref:
        One of:
        - A GitHub issue URL  (``https://github.com/owner/repo/issues/123``)
        - A bare issue number (``123``) — owner/repo inferred from git remote
        - A path to a local Markdown or JSON file
    offline:
        When *True*, skip all network/CLI calls and attempt fixture loading
        from ``netrani/fixtures/<issue_ref>.json``.

    Returns
    -------
    IssueRecord
        Fully parsed issue record with extracted symbols, reproduction steps,
        version, and environment.

    Raises
    ------
    ValueError
        If *issue_ref* cannot be resolved to any supported source.
    """
    # Local file shortcut
    local_path = Path(issue_ref)
    if local_path.suffix.lower() in (".md", ".json") or local_path.exists():
        return _load_local_file(local_path)

    github_ref = _parse_github_ref(issue_ref)
    if github_ref is None:
        raise ValueError(
            f"Cannot resolve issue reference: {issue_ref!r}. "
            "Provide a GitHub URL, bare issue number, or local .md/.json file."
        )
    owner, repo, number = github_ref

    if offline:
        fixture = Path(__file__).parent.parent / "fixtures" / f"{number}.json"
        if fixture.exists():
            return _load_local_file(fixture)
        raise FileNotFoundError(
            f"Offline mode requested but fixture not found: {fixture}"
        )

    # Try gh CLI first, fall back to REST API
    errors: list[str] = []
    try:
        return _fetch_via_gh_cli(owner, repo, number)
    except RuntimeError as exc:
        errors.append(str(exc))

    try:
        return _fetch_via_rest_api(owner, repo, number)
    except RuntimeError as exc:
        errors.append(str(exc))

    raise RuntimeError(
        f"All fetch strategies failed for {owner}/{repo}#{number}:\n"
        + "\n".join(f"  - {e}" for e in errors)
    )
