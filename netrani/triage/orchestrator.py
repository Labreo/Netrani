"""
netrani/triage/orchestrator.py
Triage Synthesis Engine — Tier 3 of the verification gate.

Runs History Miner and Static Validator in parallel via a
ThreadPoolExecutor, synthesises their findings into one canonical verdict,
validates the result against ``.bob/verdict.schema.json``, writes
``.bob/verdict.json``, and prints a clean ASCII summary table to stdout.

Canonical verdicts (in priority order):
  OBSOLETE      — history shows the defect was already fixed on main
  DUPLICATE     — history shows an open PR/issue tracks the same root cause
  FALSE_POSITIVE — static analysis proves the failure is unreachable
  VALID          — defect is reachable and unresolved

If neither subagent reaches a confident conclusion the synthesiser
defaults to VALID with an appropriately low confidence so that the defect
is never silently dismissed.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from netrani.subagents import history_miner, static_validator
from netrani.subagents.history_miner import HistoryFindings
from netrani.subagents.static_validator import StaticFindings

# ---------------------------------------------------------------------------
# Schema validation (stdlib-only, no jsonschema dependency required)
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = {
    "status", "citation", "rationale", "confidence",
    "timestamp", "target_repo", "issue_reference",
}
_VALID_STATUSES = {"VALID", "DUPLICATE", "OBSOLETE", "FALSE_POSITIVE"}


def _validate_verdict(obj: dict[str, Any]) -> list[str]:
    """
    Return a list of validation error strings.  An empty list means the
    object is schema-compliant.
    """
    errors: list[str] = []

    missing = _REQUIRED_FIELDS - obj.keys()
    if missing:
        errors.append(f"Missing required fields: {sorted(missing)}")

    status = obj.get("status")
    if status not in _VALID_STATUSES:
        errors.append(
            f"Invalid status {status!r}; expected one of {sorted(_VALID_STATUSES)}"
        )

    confidence = obj.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
        errors.append(f"confidence must be a float in [0.0, 1.0]; got {confidence!r}")

    citation = obj.get("citation", "")
    if not isinstance(citation, str) or len(citation) < 1:
        errors.append("citation must be a non-empty string")

    rationale = obj.get("rationale", "")
    if not isinstance(rationale, str) or len(rationale) < 10:
        errors.append("rationale must be at least 10 characters")

    ts = obj.get("timestamp", "")
    if not isinstance(ts, str) or not re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", ts
    ):
        errors.append(
            f"timestamp must be ISO 8601 UTC (e.g. 2025-07-15T10:30:00Z); got {ts!r}"
        )

    for field in ("target_repo", "issue_reference"):
        val = obj.get(field, "")
        if not isinstance(val, str) or len(val) < 1:
            errors.append(f"{field} must be a non-empty string")

    return errors


# ---------------------------------------------------------------------------
# Target-repo resolution
# ---------------------------------------------------------------------------

def _detect_target_repo(repo_path: Path) -> str:
    """
    Return an ``owner/repo`` string or the remote URL by inspecting the
    git remote.  Falls back to the bare directory name.
    """
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_path),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        # Convert git@ or https:// URL to owner/repo
        m = re.search(
            r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$", url
        )
        if m:
            return f"{m.group('owner')}/{m.group('repo')}"
        return url
    except (subprocess.CalledProcessError, FileNotFoundError):
        return repo_path.name


# ---------------------------------------------------------------------------
# Synthesis logic
# ---------------------------------------------------------------------------

def _synthesise(
    history: HistoryFindings,
    static: StaticFindings,
) -> tuple[str, str, str, float]:
    """
    Merge findings from both subagents into (status, citation, rationale, confidence).

    Priority rules (highest to lowest):
      1. OBSOLETE  — history has an explicit fix-commit link AND confidence >= 0.80.
                     Weaker OBSOLETE (< 0.80) always falls through to Tier 2 synthesis.
      2. DUPLICATE — history confident (conf >= 0.65) and static does not confirm VALID
                     with higher confidence (conf >= 0.70).
      3. FALSE_POSITIVE — static is confident (conf >= 0.75) and history inconclusive
                          or non-VALID.
      4. VALID     — static confirms reachable, or ambiguous fallback.

    If Tier 1 is inconclusive (confidence < 0.80), Tier 2 evidence always
    participates in the synthesis.
    """
    history_is_strong_obsolete = (
        history.verdict == "OBSOLETE"
        and history.confidence >= 0.80
        # Extra guard: citation must look like a real commit SHA or PR reference,
        # not a fallback string from the inconclusive path.
        and bool(history.citation)
        and not history.citation.startswith("No specific")
    )

    # Rule 1: strong OBSOLETE — only accept when history has a direct commit/fix link
    if history_is_strong_obsolete:
        return (
            "OBSOLETE",
            history.citation,
            history.rationale,
            history.confidence,
        )

    # Rule 2: confident DUPLICATE from ongoing work or tracked issue
    if history.verdict == "DUPLICATE" and history.confidence >= 0.65:
        if static.verdict != "VALID" or static.confidence <= 0.75:
            return (
                "DUPLICATE",
                history.citation,
                history.rationale,
                history.confidence,
            )

    # Rule 3: confident FALSE_POSITIVE from static
    # (reached whenever Tier 1 is inconclusive OR weak OBSOLETE < 0.80)
    if static.verdict == "FALSE_POSITIVE" and static.confidence >= 0.75:
        combined_rationale = static.rationale
        if history.rationale:
            combined_rationale = (
                f"Static analysis: {static.rationale}  "
                f"History check: {history.rationale}"
            )
        return (
            "FALSE_POSITIVE",
            static.citation,
            combined_rationale,
            static.confidence,
        )

    # Rule 4: static confirms VALID
    if static.verdict == "VALID":
        combined_rationale = static.rationale
        if history.rationale:
            combined_rationale = (
                f"History check: {history.rationale}  "
                f"Static analysis: {static.rationale}"
            )
        return (
            "VALID",
            static.citation or "see static analysis evidence",
            combined_rationale,
            max(static.confidence, 0.55),
        )

    # Rule 5: confident DUPLICATE fallback
    if history.verdict == "DUPLICATE" and history.confidence >= 0.65:
        return (
            history.verdict,
            history.citation,
            history.rationale,
            history.confidence,
        )

    # Ambiguous: neither subagent reached a conclusion — conservative VALID
    return (
        "VALID",
        "No specific file:line identified; manual investigation required.",
        (
            "Neither tier of analysis produced a definitive conclusion.  "
            "The defect cannot be statically disproven; defaulting to VALID "
            "to ensure it is not silently dismissed."
        ),
        0.55,
    )


# ---------------------------------------------------------------------------
# ASCII summary table renderer
# ---------------------------------------------------------------------------

_TABLE_WIDTH = 61  # inner width (between │ chars)


def _box_line(inner: str, width: int = _TABLE_WIDTH) -> str:
    return f"│  {inner.ljust(width - 2)}│"


def _render_summary_table(verdict: dict[str, Any]) -> str:
    top     = "┌" + "─" * _TABLE_WIDTH + "┐"
    sep     = "├" + "─" * _TABLE_WIDTH + "┤"
    bot     = "└" + "─" * _TABLE_WIDTH + "┘"

    title   = _box_line("NETRANI TRIAGE VERDICT")
    status  = _box_line(f"  Status     : {verdict['status']}")
    conf    = _box_line(f"  Confidence : {verdict['confidence']:.2f}")
    cite    = _box_line(f"  Citation   : {verdict['citation'][:52]}")
    issue   = _box_line(f"  Issue      : {verdict['issue_reference'][:52]}")
    repo    = _box_line(f"  Repo       : {verdict['target_repo'][:52]}")

    # Wrap rationale across multiple lines
    wrapped = textwrap.wrap(verdict["rationale"], width=_TABLE_WIDTH - 15)
    rat_lines = []
    for i, chunk in enumerate(wrapped):
        prefix = "Rationale  : " if i == 0 else "             "
        rat_lines.append(_box_line(f"  {prefix}{chunk}"))
    if not rat_lines:
        rat_lines = [_box_line("  Rationale  : (none)")]

    return "\n".join([top, title, sep, status, conf, cite, issue, repo, sep, *rat_lines, bot])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(
    repo_path: str | Path,
    issue_reference: str,
    title: str,
    body: str,
    suspect_symbols: Sequence[str],
    reproduction_trace: Sequence[str],
    issue_url: str = "",
    verdict_path: str | Path = ".bob/verdict.json",
    quiet: bool = False,
    use_bob: bool = False,
    escalate_to_bob: bool = False,
) -> dict[str, Any]:
    """
    Execute the full three-tier triage workflow and write ``.bob/verdict.json``.

    Parameters
    ----------
    repo_path:
        Absolute or relative path to the repository being triaged.
    issue_reference:
        Human-readable identifier for the issue (URL, ticket ID, descriptor).
    title:
        Issue title.
    body:
        Full issue body.
    suspect_symbols:
        Pre-extracted suspect symbols from ``IssueRecord``.
    reproduction_trace:
        Ordered reproduction steps from ``IssueRecord``.
    issue_url:
        Optional canonical URL for the issue (used by history miner as
        duplicate citation if no better URL is found).
    verdict_path:
        Where to write the verdict JSON (default: ``.bob/verdict.json``).
    quiet:
        When True, suppress the ASCII summary table output.
    use_bob:
        When True, route triage through IBM Bob 2.0 Agent Mode custom personas.
    escalate_to_bob:
        When True, automatically escalate ambiguous/boundary cases to IBM Bob Agent Mode.

    Returns
    -------
    dict
        The verdict object that was written to disk.
    """
    repo = Path(repo_path).expanduser().resolve()
    vpath = Path(verdict_path)

    # ── Tier 1 + Tier 2 in parallel ─────────────────────────────────────────
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        fut_history = executor.submit(
            history_miner.run,
            repo,
            title,
            body,
            suspect_symbols,
            issue_url or issue_reference,
        )
        fut_static = executor.submit(
            static_validator.run,
            repo,
            title,
            body,
            suspect_symbols,
            reproduction_trace,
        )
        history_findings: HistoryFindings = fut_history.result()
        static_findings: StaticFindings = fut_static.result()

    # ── Tier 3: Synthesis ────────────────────────────────────────────────────
    status, citation, rationale, confidence = _synthesise(history_findings, static_findings)

    # ── IBM Bob 2.0 Agent Mode Escalation ────────────────────────────────────
    if use_bob or (escalate_to_bob and confidence < 0.80):
        try:
            from netrani.bob.agent import run_bob_escalation
            bob_res = run_bob_escalation(
                repo_path=repo,
                title=title,
                body=body,
                suspect_symbols=suspect_symbols,
                reproduction_trace=reproduction_trace,
                issue_ref=issue_reference or issue_url,
            )
            if bob_res.get("bob_escalated"):
                status = bob_res["status"]
                confidence = bob_res["confidence"]
                citation = bob_res["citation"] or citation
                rationale = f"{bob_res['rationale']} [Verified via IBM Bob 2.0 Agent Mode]"
        except Exception as exc:
            log.warning("Bob agent escalation encountered an issue: %s", exc)

    target_repo = _detect_target_repo(repo)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    verdict: dict[str, Any] = {
        "status": status,
        "citation": citation,
        "rationale": rationale,
        "confidence": round(float(confidence), 4),
        "timestamp": timestamp,
        "target_repo": target_repo,
        "issue_reference": issue_reference or issue_url or "unknown",
    }

    # ── Schema validation ────────────────────────────────────────────────────
    errors = _validate_verdict(verdict)
    if errors:
        raise ValueError(
            "Synthesised verdict fails schema validation:\n"
            + "\n".join(f"  • {e}" for e in errors)
        )

    # ── Write verdict.json ───────────────────────────────────────────────────
    vpath.parent.mkdir(parents=True, exist_ok=True)
    vpath.write_text(json.dumps(verdict, indent=2), encoding="utf-8")

    # ── Print summary table ──────────────────────────────────────────────────
    if not quiet:
        print(_render_summary_table(verdict))
        print()
        if status == "VALID":
            print(
                "✓ Verdict is VALID.  The surgical-fixer mode may now be invoked "
                "to draft a fix."
            )
        else:
            print(
                f"✗ Verdict is {status}.  No fix will be drafted.  "
                "Review the citation and rationale above."
            )
        print()

    return verdict


# ---------------------------------------------------------------------------
# CLI convenience entry-point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    """
    Minimal CLI for running the orchestrator directly::

        python -m netrani.triage.orchestrator \\
            --repo /path/to/repo \\
            --issue "https://github.com/owner/repo/issues/42" \\
            --title "NullPointerException in Foo.bar()" \\
            --body "..."
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="netrani-triage",
        description="Netrani triage orchestrator — run verification and write verdict.json",
    )
    parser.add_argument("--repo", required=True, help="Path to target repository")
    parser.add_argument("--issue", required=True, help="Issue reference (URL or ID)")
    parser.add_argument("--title", default="", help="Issue title")
    parser.add_argument("--body", default="", help="Issue body text")
    parser.add_argument(
        "--verdict-path",
        default=".bob/verdict.json",
        help="Output path for verdict.json (default: .bob/verdict.json)",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress ASCII table output")
    args = parser.parse_args(argv)

    try:
        run(
            repo_path=args.repo,
            issue_reference=args.issue,
            title=args.title,
            body=args.body,
            suspect_symbols=[],
            reproduction_trace=[],
            issue_url=args.issue if args.issue.startswith("http") else "",
            verdict_path=args.verdict_path,
            quiet=args.quiet,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
