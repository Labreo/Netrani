"""
netrani/pipeline/orchestrator.py
Full end-to-end Netrani pipeline orchestrator.

Wires all prior components (Issue Ingestion, Document Parser, Triage, Surgical
Fixer, Test Runner, and Git Emitter) into the unified ``netrani run`` command
as a strict eight-stage sequential pipeline:

  Stage 1 — Issue Ingestion         (IssueIngestor / fetcher)
  Stage 2 — Runtime Doc Discovery   (DocumentParser / doc_parser)
  Stage 3 — Triage                  (Subagents 1+2 via triage.orchestrator)
  Stage 4 — Verdict Output          → writes .bob/verdict.json
  Stage 5 — Surgical Fix            (Subagent 3 — SurgicalFixer)   *VALID only*
  Stage 6 — Test Execution          (Subagent 4 — TestRunner)       *VALID only*
  Stage 7 — Branch + PR Artifact    (GitEmitter)                    *VALID only*
  Stage 8 — Final Status Report     → stdout + .bob/run_summary.json

If the verdict is non-VALID (INVALID / DUPLICATE / OBSOLETE / FALSE_POSITIVE /
INCONCLUSIVE), Stages 5–8 are skipped, a human-readable explanation is printed
to stdout, and run_summary.json reflects the early exit.

Each stage catches its own exceptions and packages them into a StageResult.
A failed StageResult halts the pipeline and causes a non-zero exit.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import textwrap
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import netrani.triage.orchestrator as triage_orchestrator
from netrani import config
from netrani.issue.fetcher import fetch_issue
from netrani.parser.doc_parser import build_repo_profile
from netrani.pipeline.git_emitter import emit_pr_artifacts
from netrani.subagents.surgical_fixer import run_surgical_fix
from netrani.subagents.test_runner import run_tests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# StageResult
# ---------------------------------------------------------------------------


@dataclass
class StageResult:
    """Carries the outcome of a single pipeline stage to the next stage."""

    stage_name: str
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage_name,
            "success": self.success,
            "data": self.data,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# ANSI colour helpers (no third-party deps)
# ---------------------------------------------------------------------------

_TTY = sys.stdout.isatty()


def _c(code: str, t: str) -> str:
    return f"\033[{code}m{t}\033[0m" if _TTY else t


def _green(t: str) -> str:
    return _c("32", t)


def _yellow(t: str) -> str:
    return _c("33", t)


def _red(t: str) -> str:
    return _c("31", t)


def _bold(t: str) -> str:
    return _c("1", t)


def _dim(t: str) -> str:
    return _c("2", t)


# ---------------------------------------------------------------------------
# Structured JSON logging helper (--verbose mode)
# ---------------------------------------------------------------------------


def _vlog(verbose: bool, event: str, **kwargs: Any) -> None:
    """Emit a structured JSON log line to stdout when --verbose is active."""
    if not verbose:
        return
    entry = {
        "event": event,
        "timestamp_utc": datetime.now(tz=timezone.utc).isoformat(),
        **kwargs,
    }
    print(json.dumps(entry), flush=True)


# ---------------------------------------------------------------------------
# Profile persistence helper
# ---------------------------------------------------------------------------


def _persist_profile(profile: dict[str, Any], repo_root: Path) -> Path:
    """
    Write the runtime repo profile to ``.bob/repo_profile.json`` so that
    downstream stages can read it from disk without requiring the full
    profile object in memory.
    """
    out = config.bob_dir(repo_root) / "repo_profile.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(profile, indent=2, default=str), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Stage implementations
# ---------------------------------------------------------------------------


def _stage_issue_ingestion(
    issue_ref: str,
    offline: bool,
    verbose: bool,
) -> StageResult:
    """Stage 1: Fetch and parse the issue record."""
    try:
        issue = fetch_issue(issue_ref, offline=offline)
        _vlog(verbose, "stage_complete", stage="issue_ingestion",
              title=issue["title"], source=issue["source"])
        return StageResult(
            stage_name="issue_ingestion",
            success=True,
            data={"issue": dict(issue)},
        )
    except Exception as exc:  # noqa: BLE001
        return StageResult(
            stage_name="issue_ingestion",
            success=False,
            error=str(exc),
        )


def _stage_doc_discovery(
    repo_root: Path,
    verbose: bool,
) -> StageResult:
    """Stage 2: Build the runtime repository profile."""
    try:
        profile = build_repo_profile(repo_root)
        # Persist to disk for subagents that need it
        profile_path = _persist_profile(dict(profile), repo_root)
        _vlog(verbose, "stage_complete", stage="doc_discovery",
              languages=profile["detected_languages"],
              test_commands=profile["test_commands"])
        return StageResult(
            stage_name="doc_discovery",
            success=True,
            data={"profile": dict(profile), "profile_path": str(profile_path)},
        )
    except Exception as exc:  # noqa: BLE001
        return StageResult(
            stage_name="doc_discovery",
            success=False,
            error=str(exc),
        )


def _stage_triage(
    repo_root: Path,
    issue: dict[str, Any],
    verbose: bool,
    verdict_path_override: Path | None = None,
    use_bob: bool = False,
) -> StageResult:
    """Stage 3 + 4: Run triage and write verdict.json."""
    vpath = verdict_path_override or config.verdict_path(repo_root)
    try:
        verdict = triage_orchestrator.run(
            repo_path=str(repo_root),
            issue_reference=issue.get("url") or issue.get("title", "unknown"),
            title=issue.get("title", ""),
            body="\n".join(
                issue.get("reproduction_trace", [])
            ),
            suspect_symbols=issue.get("suspect_symbols", []),
            reproduction_trace=issue.get("reproduction_trace", []),
            issue_url=issue.get("url", ""),
            verdict_path=str(vpath),
            quiet=not verbose,
            use_bob=use_bob,
            escalate_to_bob=True,
        )
        _vlog(verbose, "stage_complete", stage="triage",
              status=verdict["status"], confidence=verdict["confidence"],
              use_bob=use_bob)
        return StageResult(
            stage_name="triage",
            success=True,
            data={"verdict": verdict, "verdict_path": str(vpath)},
        )
    except Exception as exc:  # noqa: BLE001
        return StageResult(
            stage_name="triage",
            success=False,
            error=str(exc),
        )


def _stage_surgical_fix(
    repo_root: Path,
    verdict_path_str: str,
    profile_path_str: str,
    verbose: bool,
) -> StageResult:
    """Stage 5: Apply the minimal surgical patch."""
    try:
        result = run_surgical_fix(
            verdict_path=verdict_path_str,
            profile_path=profile_path_str,
            repo_root=str(repo_root),
        )
        success = result.get("status") == "applied"
        _vlog(verbose, "stage_complete", stage="surgical_fix", **result)
        return StageResult(
            stage_name="surgical_fix",
            success=success,
            data=result,
            error="" if success else result.get("error", "patch status: aborted"),
        )
    except SystemExit as exc:
        # surgical_fixer exits with code 2 on non-VALID verdict — propagate
        return StageResult(
            stage_name="surgical_fix",
            success=False,
            error=f"SurgicalFixer hard-aborted (exit {exc.code}). Verdict gate not met.",
        )
    except Exception as exc:  # noqa: BLE001
        return StageResult(
            stage_name="surgical_fix",
            success=False,
            error=str(exc),
        )


def _stage_test_execution(
    repo_root: Path,
    profile_path_str: str,
    branch: str,
    verdict_path_str: str,
    verbose: bool,
    max_retries: int = 1,
) -> StageResult:
    """
    Stage 6: Run lint + tests with exactly one retry loop on failure.

    If the first run fails, the failure report is included in the StageResult
    so the orchestrator can route it back to Stage 5 for a targeted fix.
    On the second failure, the pipeline halts.
    """
    attempt = 0
    last_result: dict[str, Any] = {}

    while attempt <= max_retries:
        attempt += 1
        _vlog(verbose, "test_attempt", attempt=attempt, branch=branch)

        last_result = run_tests(
            profile_path=profile_path_str,
            branch=branch,
            repo_root=str(repo_root),
        )

        overall = last_result.get("overall_status", "failed")

        if overall in ("passed", "skipped"):
            _vlog(verbose, "stage_complete", stage="test_execution", overall_status=overall)
            return StageResult(
                stage_name="test_execution",
                success=True,
                data=last_result,
            )

        if attempt <= max_retries:
            log.warning(
                "Test run %d failed (%d failure(s)). "
                "Attempting one targeted refinement via SurgicalFixer…",
                attempt,
                len(last_result.get("failures", [])),
            )
            _vlog(verbose, "test_retry", attempt=attempt,
                  failures=last_result.get("failures", []))

            # Refinement pass: re-run surgical fixer with failure context
            profile_path_extended = str(
                _inject_failure_context(
                    profile_path_str, last_result.get("failures", []), repo_root
                )
            )
            refinement = _stage_surgical_fix(
                repo_root=repo_root,
                verdict_path_str=verdict_path_str,
                profile_path_str=profile_path_extended,
                verbose=verbose,
            )
            if not refinement.success:
                log.warning("Refinement patch also failed: %s", refinement.error)
                # Continue to second test attempt regardless

    # Both runs failed — halt
    return StageResult(
        stage_name="test_execution",
        success=False,
        data=last_result,
        error=(
            f"Tests failed after {attempt} attempt(s). "
            "Full diagnostics in audit.log."
        ),
    )


def _inject_failure_context(
    profile_path: str, failures: list[dict[str, Any]], repo_root: Path
) -> Path:
    """
    Produce an augmented profile path that includes the failure context so
    the refinement surgical fix pass can target the precise failure site.
    Writes a temporary ``_retry_profile.json`` to ``.bob/``.
    """
    out_path = config.bob_dir(repo_root) / "_retry_profile.json"
    try:
        profile: dict[str, Any] = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        profile = {}
    profile["_failure_context"] = failures
    out_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    return out_path


def _stage_git_emit(
    repo_root: Path,
    verdict_path_str: str,
    audit_log_path_str: str,
    verbose: bool,
    dry_run: bool,
    dry_run_dir: str,
    create_pr: bool = False,
    base_branch: str = "main",
) -> StageResult:
    """Stage 7: Create the Git commit and write the PR draft."""
    try:
        result = emit_pr_artifacts(
            verdict_path=verdict_path_str,
            audit_log_path=audit_log_path_str,
            repo_root=str(repo_root),
            dry_run=dry_run,
            dry_run_dir=dry_run_dir,
            create_pr=create_pr,
            base_branch=base_branch,
        )
        success = result.get("status") == "ready"
        _vlog(verbose, "stage_complete", stage="git_emit", **result)
        return StageResult(
            stage_name="git_emit",
            success=success,
            data=result,
            error="" if success else result.get("error", "GitEmitter status: failed"),
        )
    except Exception as exc:  # noqa: BLE001
        return StageResult(
            stage_name="git_emit",
            success=False,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Run summary writer
# ---------------------------------------------------------------------------


def _write_run_summary(
    repo_root: Path,
    run_id: str,
    stages: list[StageResult],
    verdict_status: str,
    dry_run: bool,
    dry_run_dir: str,
) -> Path:
    """
    Serialise the full pipeline result into ``.bob/run_summary.json``.
    """
    if dry_run:
        out_path = (
            Path(dry_run_dir) if Path(dry_run_dir).is_absolute()
            else repo_root / dry_run_dir
        ) / "run_summary.json"
    else:
        out_path = config.run_summary_path(repo_root)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    summary = {
        "run_id": run_id,
        "timestamp_utc": datetime.now(tz=timezone.utc).isoformat(),
        "verdict_status": verdict_status,
        "dry_run": dry_run,
        "stages": [s.to_dict() for s in stages],
        "overall_success": all(s.success for s in stages),
    }
    out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# Non-VALID early-exit messaging
# ---------------------------------------------------------------------------

_NON_VALID_MESSAGES: dict[str, str] = {
    "DUPLICATE": (
        "The triage verdict is DUPLICATE — this issue is already tracked by an open PR "
        "or issue.  No fix will be generated.  Refer to the citation in verdict.json for "
        "the linked duplicate."
    ),
    "OBSOLETE": (
        "The triage verdict is OBSOLETE — the defect described in this issue was resolved "
        "in a prior commit.  No fix is required.  See the citation in verdict.json for the "
        "relevant commit SHA."
    ),
    "FALSE_POSITIVE": (
        "The triage verdict is FALSE_POSITIVE — static analysis proves the reported failure "
        "cannot occur under the current codebase invariants.  No fix will be generated."
    ),
    "INCONCLUSIVE": (
        "The triage verdict is INCONCLUSIVE — the analysis did not reach a definitive "
        "conclusion.  Manual investigation is recommended before any code modification."
    ),
}


def _print_early_exit(verdict_status: str) -> None:
    """Print a human-readable explanation for a non-VALID verdict."""
    message = _NON_VALID_MESSAGES.get(
        verdict_status,
        f"Verdict is '{verdict_status}' — Stages 5–8 are skipped.",
    )
    print()
    print(_bold("━" * 60))
    print(_yellow(f"  Netrani — Pipeline Early Exit ({verdict_status})"))
    print(_bold("━" * 60))
    print()
    for line in textwrap.wrap(message, width=58):
        print(f"  {line}")
    print()
    print(_dim("  See .bob/verdict.json for full triage details."))
    print(_bold("━" * 60))
    print()


# ---------------------------------------------------------------------------
# Core pipeline runner
# ---------------------------------------------------------------------------


def run_pipeline(
    issue_ref: str,
    repo_root: str,
    verbose: bool = False,
    dry_run: bool = False,
    offline: bool = False,
    dry_run_dir: str = ".bob/dry_run",
    create_pr: bool = False,
    base_branch: str = "main",
    use_bob: bool = False,
) -> int:
    """
    Execute the full eight-stage Netrani pipeline.

    Parameters
    ----------
    issue_ref:
        GitHub issue URL, issue number, or local file path.
    repo_root:
        Absolute path to the target repository root.
    verbose:
        If True, stream structured JSON log lines to stdout.
    dry_run:
        If True, skip Git operations and write artefacts to *dry_run_dir*.
    offline:
        If True, skip network calls and use local issue fixtures.
    dry_run_dir:
        Output directory for dry-run artefacts.
    create_pr:
        If True and not dry-run, push branch and submit PR via gh CLI.
    base_branch:
        Target base branch for PR (default: main).
    use_bob:
        If True, orchestrate triage, fix, and verification via IBM Bob 2.0 Agent Mode.

    Returns
    -------
    int
        Exit code: 0 for success, 1 for pipeline failure.
    """
    run_id = str(uuid.uuid4())
    repo = Path(repo_root).expanduser().resolve()
    completed_stages: list[StageResult] = []
    verdict_status = "UNKNOWN"

    _vlog(verbose, "pipeline_start", run_id=run_id, repo=str(repo),
          issue_ref=issue_ref, dry_run=dry_run, use_bob=use_bob)

    # ── Stage 1: Issue Ingestion ──────────────────────────────────────────────
    s1 = _stage_issue_ingestion(issue_ref, offline, verbose)
    completed_stages.append(s1)
    if not s1.success:
        log.error("[Stage 1] Issue ingestion failed: %s", s1.error)
        print(_red(f"Error [Stage 1 — Issue Ingestion]: {s1.error}"), file=sys.stderr)
        _write_run_summary(repo, run_id, completed_stages, verdict_status, dry_run, dry_run_dir)
        return 1

    issue: dict[str, Any] = s1.data["issue"]

    # ── Stage 2: Runtime Doc Discovery ───────────────────────────────────────
    s2 = _stage_doc_discovery(repo, verbose)
    completed_stages.append(s2)
    if not s2.success:
        log.error("[Stage 2] Doc discovery failed: %s", s2.error)
        print(_red(f"Error [Stage 2 — Doc Discovery]: {s2.error}"), file=sys.stderr)
        _write_run_summary(repo, run_id, completed_stages, verdict_status, dry_run, dry_run_dir)
        return 1

    profile_path_str: str = s2.data["profile_path"]

    # ── Stage 3 + 4: Triage + Verdict Output ─────────────────────────────────
    s3 = _stage_triage(repo, issue, verbose, use_bob=use_bob)
    completed_stages.append(s3)
    if not s3.success:
        log.error("[Stage 3] Triage failed: %s", s3.error)
        print(_red(f"Error [Stage 3 — Triage]: {s3.error}"), file=sys.stderr)
        _write_run_summary(repo, run_id, completed_stages, verdict_status, dry_run, dry_run_dir)
        return 1

    verdict: dict[str, Any] = s3.data["verdict"]
    verdict_status = verdict.get("status", "UNKNOWN")
    verdict_path_str: str = s3.data["verdict_path"]

    # ── Gate: only proceed to Stages 5–8 if verdict is VALID ─────────────────
    if verdict_status != "VALID":
        _print_early_exit(verdict_status)
        _write_run_summary(repo, run_id, completed_stages, verdict_status, dry_run, dry_run_dir)
        return 0  # Non-VALID is not a pipeline failure

    # ── Stage 5: Surgical Fix ────────────────────────────────────────────────
    s5 = _stage_surgical_fix(repo, verdict_path_str, profile_path_str, verbose)
    completed_stages.append(s5)
    if not s5.success:
        log.error("[Stage 5] Surgical fix failed: %s", s5.error)
        print(_red(f"Error [Stage 5 — Surgical Fix]: {s5.error}"), file=sys.stderr)
        _write_run_summary(repo, run_id, completed_stages, verdict_status, dry_run, dry_run_dir)
        return 1

    fix_branch: str = s5.data.get("branch", "")
    audit_log_path_str: str = str(config.audit_log_path(repo))

    # ── Stage 6: Test Execution ───────────────────────────────────────────────
    s6 = _stage_test_execution(
        repo_root=repo,
        profile_path_str=profile_path_str,
        branch=fix_branch,
        verdict_path_str=verdict_path_str,
        verbose=verbose,
    )
    completed_stages.append(s6)
    if not s6.success:
        log.error("[Stage 6] Test execution failed: %s", s6.error)
        print(_red(f"Error [Stage 6 — Test Execution]: {s6.error}"), file=sys.stderr)
        _write_run_summary(repo, run_id, completed_stages, verdict_status, dry_run, dry_run_dir)
        return 1

    # ── Stage 7: Branch + PR Artifact ────────────────────────────────────────
    s7 = _stage_git_emit(
        repo_root=repo,
        verdict_path_str=verdict_path_str,
        audit_log_path_str=audit_log_path_str,
        verbose=verbose,
        dry_run=dry_run,
        dry_run_dir=dry_run_dir,
        create_pr=create_pr,
        base_branch=base_branch,
    )
    completed_stages.append(s7)
    if not s7.success:
        log.error("[Stage 7] Git emit failed: %s", s7.error)
        print(_red(f"Error [Stage 7 — Git Emit]: {s7.error}"), file=sys.stderr)
        _write_run_summary(repo, run_id, completed_stages, verdict_status, dry_run, dry_run_dir)
        return 1

    # ── Stage 8: Final Status Report ──────────────────────────────────────────
    summary_path = _write_run_summary(
        repo, run_id, completed_stages, verdict_status, dry_run, dry_run_dir
    )

    pr_path = s7.data.get("pr_draft_path", "")
    commit_sha = s7.data.get("commit_sha", "")
    branch = s7.data.get("branch", fix_branch)
    pr_url = s7.data.get("pr_url", "")
    pr_command = s7.data.get("pr_command", "")

    _print_final_report(
        verdict=verdict,
        branch=branch,
        commit_sha=commit_sha,
        pr_path=pr_path,
        summary_path=str(summary_path),
        test_result=s6.data,
        dry_run=dry_run,
        pr_url=pr_url,
        pr_command=pr_command,
    )

    _vlog(verbose, "pipeline_complete", run_id=run_id, verdict_status=verdict_status,
          branch=branch, commit_sha=commit_sha, pr_url=pr_url)

    return 0


# ---------------------------------------------------------------------------
# Final report printer
# ---------------------------------------------------------------------------


def _print_final_report(
    verdict: dict[str, Any],
    branch: str,
    commit_sha: str,
    pr_path: str,
    summary_path: str,
    test_result: dict[str, Any],
    dry_run: bool,
    pr_url: str = "",
    pr_command: str = "",
) -> None:
    """Print the Stage 8 human-readable final status report to stdout."""
    print()
    print(_bold("━" * 60))
    print(_bold("  Netrani — Pipeline Complete"))
    print(_bold("━" * 60))
    print()

    rows = [
        ("Verdict", _green("VALID")),
        ("Confidence", f"{verdict.get('confidence', 0):.2f}"),
        ("Citation", verdict.get("citation", "")[:55]),
        ("Branch", branch),
        ("Commit", commit_sha[:12] if commit_sha else _dim("(dry-run — skipped)")),
        ("PR Draft", pr_path),
        ("Tests", (
            _green(f"✓ {test_result.get('commands_run', 0)} command(s) passed")
            if test_result.get("overall_status") in ("passed", "skipped")
            else _red(f"✗ {len(test_result.get('failures', []))} failure(s)")
        )),
        ("Run Summary", summary_path),
    ]
    if pr_url:
        rows.append(("GitHub PR", _green(pr_url)))
    elif pr_command:
        rows.append(("PR Command", _dim(pr_command)))

    if dry_run:
        rows.insert(0, ("Mode", _yellow("DRY-RUN — Git operations skipped")))

    max_key = max(len(k) for k, _ in rows) + 2
    for key, value in rows:
        print(f"  {key.ljust(max_key)}  {value}")

    print()
    print(_bold("━" * 60))
    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netrani-run",
        description=(
            "Netrani full pipeline: ingest issue → triage → fix → test → PR draft."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              netrani-run --issue-url https://github.com/owner/repo/issues/42 \\
                          --repo-root /path/to/repo
              netrani-run --issue-file /tmp/issue.json --repo-root . --dry-run
              netrani-run --issue-url 42 --repo-root . --create-pr
            """
        ),
    )

    issue_group = parser.add_mutually_exclusive_group(required=True)
    issue_group.add_argument(
        "--issue-url",
        metavar="<url-or-id>",
        help="GitHub issue URL, number, or local .md/.json path.",
    )
    issue_group.add_argument(
        "--issue-file",
        metavar="<file>",
        help="Local .md or .json file describing the issue.",
    )

    parser.add_argument(
        "--repo-root",
        required=True,
        metavar="<path>",
        help="Absolute or relative path to the target repository root.",
    )
    parser.add_argument(
        "--create-pr",
        action="store_true",
        help="Push branch to origin and create a GitHub pull request via gh CLI.",
    )
    parser.add_argument(
        "--base-branch",
        default="main",
        metavar="<branch>",
        help="Base branch for the pull request (default: main).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Git operations; write artefacts to .bob/dry_run/ instead.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Stream structured JSON log lines to stdout during execution.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip network calls; use local fixtures for issue fetching.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the orchestrator."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(name)s  %(message)s",
        stream=sys.stderr,
    )

    parser = _build_parser()
    args = parser.parse_args(argv)

    issue_ref: str = args.issue_url or args.issue_file

    return run_pipeline(
        issue_ref=issue_ref,
        repo_root=args.repo_root,
        verbose=args.verbose,
        dry_run=args.dry_run,
        offline=args.offline,
        create_pr=args.create_pr,
        base_branch=args.base_branch,
    )


if __name__ == "__main__":
    sys.exit(main())

