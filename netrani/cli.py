"""
netrani/cli.py
CLI orchestrator for Netrani.

Entry point for `netrani run` — discovers repository profile, fetches issue,
and emits a structured session context payload to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

from netrani.parser.doc_parser import build_repo_profile, RepoProfile
from netrani.issue.fetcher import fetch_issue, IssueRecord


# ---------------------------------------------------------------------------
# ANSI colour helpers (no third-party dependency)
# ---------------------------------------------------------------------------

_COLOUR_ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    if not _COLOUR_ENABLED:
        return text
    return f"\033[{code}m{text}\033[0m"


def _bold(t: str) -> str:
    return _c("1", t)

def _green(t: str) -> str:
    return _c("32", t)

def _yellow(t: str) -> str:
    return _c("33", t)

def _cyan(t: str) -> str:
    return _c("36", t)

def _dim(t: str) -> str:
    return _c("2", t)

def _red(t: str) -> str:
    return _c("31", t)


# ---------------------------------------------------------------------------
# Summary table renderer
# ---------------------------------------------------------------------------

def _render_table(rows: list[tuple[str, str]], title: str = "") -> str:
    if title:
        lines = [_bold(title), ""]
    else:
        lines = []
    max_key = max((len(k) for k, _ in rows), default=0) + 2
    for key, value in rows:
        wrapped_val = textwrap.fill(
            str(value),
            width=max(40, 78 - max_key),
            subsequent_indent=" " * (max_key + 3),
        )
        lines.append(f"  {_cyan(key.ljust(max_key))}  {wrapped_val}")
    return "\n".join(lines)


def _list_or_none(items: list[str]) -> str:
    return ", ".join(items) if items else _dim("(none detected)")


# ---------------------------------------------------------------------------
# Session context builder
# ---------------------------------------------------------------------------

def _build_session_context(
    profile: RepoProfile,
    issue: IssueRecord,
    repo_path: str,
    mode: str,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "repo": str(Path(repo_path).resolve()),
        "repo_profile": {
            "detected_languages": profile["detected_languages"],
            "test_commands": profile["test_commands"],
            "lint_commands": profile["lint_commands"],
            "has_contribution_guidelines": bool(profile["contribution_guidelines"]),
            "issue_template_fields": profile["issue_template_schema"].get("fields", []),
        },
        "issue": {
            "title": issue["title"],
            "url": issue["url"],
            "author": issue["author"],
            "labels": issue["labels"],
            "source": issue["source"],
            "suspect_symbols": issue["suspect_symbols"],
            "reproduction_trace": issue["reproduction_trace"],
            "reported_version": issue["reported_version"],
            "environment": issue["environment"],
        },
    }


# ---------------------------------------------------------------------------
# Pretty-print session summary
# ---------------------------------------------------------------------------

def _print_summary(ctx: dict[str, Any], verbose: bool = False) -> None:
    rp = ctx["repo_profile"]
    iss = ctx["issue"]

    print()
    print(_bold("━" * 60))
    print(_bold("  Netrani — Session Context"))
    print(_bold("━" * 60))
    print()

    # Repo section
    print(
        _render_table(
            [
                ("Mode", _green(ctx["mode"])),
                ("Repository", ctx["repo"]),
                ("Languages", _list_or_none(rp["detected_languages"])),
                ("Test commands", _list_or_none(rp["test_commands"])),
                ("Lint commands", _list_or_none(rp["lint_commands"])),
                ("Contrib guide", _green("✓ found") if rp["has_contribution_guidelines"] else _yellow("✗ not found")),
            ],
            title="Repository Profile",
        )
    )
    print()

    # Issue section
    label_str = ", ".join(iss["labels"]) if iss["labels"] else _dim("(none)")
    print(
        _render_table(
            [
                ("Title", iss["title"]),
                ("URL", iss["url"] or _dim("(local)")),
                ("Author", iss["author"] or _dim("unknown")),
                ("Labels", label_str),
                ("Source", iss["source"]),
                ("Version", iss["reported_version"] or _dim("not found")),
                ("Symbols", _list_or_none(iss["suspect_symbols"])),
                ("Repro steps",
                 str(len(iss["reproduction_trace"])) + " step(s) found"
                 if iss["reproduction_trace"] else _dim("none parsed")),
            ],
            title="Issue Record",
        )
    )

    if verbose and iss["reproduction_trace"]:
        print()
        print(_bold("  Reproduction Trace:"))
        for i, step in enumerate(iss["reproduction_trace"], 1):
            print(f"    {_dim(str(i) + '.')} {step}")

    if verbose and iss["environment"]:
        print()
        print(_bold("  Reported Environment:"))
        for k, v in iss["environment"].items():
            print(f"    {_cyan(k)}: {v}")

    print()
    print(_bold("━" * 60))
    print()


# ---------------------------------------------------------------------------
# Repository resolution (supports local paths and GitHub URLs)
# ---------------------------------------------------------------------------

def _resolve_repo(repo_ref: str, verbose: bool) -> str:
    """
    Return a local path to the repository.
    If *repo_ref* is a GitHub URL, clone it to a temp directory.
    """
    if repo_ref.startswith("https://") or repo_ref.startswith("git@"):
        import tempfile
        import subprocess
        tmpdir = tempfile.mkdtemp(prefix="netrani_clone_")
        if verbose:
            print(_dim(f"Cloning {repo_ref} → {tmpdir}"), file=sys.stderr)
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", repo_ref, tmpdir],
                check=True,
                capture_output=not verbose,
            )
        except subprocess.CalledProcessError as exc:
            print(_red(f"Error: git clone failed: {exc}"), file=sys.stderr)
            sys.exit(1)
        return tmpdir

    path = Path(repo_ref).expanduser().resolve()
    if not path.is_dir():
        print(_red(f"Error: repository path does not exist: {path}"), file=sys.stderr)
        sys.exit(1)
    return str(path)


# ---------------------------------------------------------------------------
# CLI definition
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CLI definition
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netrani",
        description="Netrani — General-purpose issue triage and verification tool built on IBM Bob 2.0.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              netrani run --repo . --issue 973
              netrani run --repo https://github.com/owner/repo --issue 973 --mode triage
              netrani run --repo . --issue 42 --mode full --offline
              netrani run --repo . --issue 42 --mode full --create-pr
              netrani triage --repo . --issue 973
              netrani pr --repo . --create-pr
            """
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # ── Command: run ──────────────────────────────────────────────────────────
    run_cmd = sub.add_parser(
        "run",
        help="Run Netrani triage, fix, and verification pipeline on a repository and issue.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_cmd.add_argument(
        "--repo",
        required=True,
        metavar="<path-or-url>",
        help="Local repository path or GitHub HTTPS URL to clone.",
    )
    run_cmd.add_argument(
        "--issue",
        required=True,
        metavar="<id-or-file>",
        help="GitHub issue number, URL, or path to a local .md/.json file.",
    )
    run_cmd.add_argument(
        "--mode",
        choices=["triage", "fix", "full"],
        default="triage",
        metavar="<mode>",
        help="Operation mode: triage (default), fix, or full.",
    )
    run_cmd.add_argument(
        "--use-bob", "--bob",
        dest="use_bob",
        action="store_true",
        help="Orchestrate triage, fix, and verification via IBM Bob 2.0 Agent Mode and custom modes.",
    )
    run_cmd.add_argument(
        "--bob-mode",
        default="auto",
        choices=["auto", "history-miner", "static-validator", "surgical-fixer", "test-runner"],
        help="Select IBM Bob 2.0 custom persona (default: auto).",
    )
    run_cmd.add_argument(
        "--create-pr",
        action="store_true",
        help="Push branch to origin and create a GitHub pull request via gh CLI (VALID only).",
    )
    run_cmd.add_argument(
        "--base-branch",
        default="main",
        metavar="<branch>",
        help="Base branch for the pull request (default: main).",
    )
    run_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute in dry-run mode without modifying git branch or remote.",
    )
    run_cmd.add_argument(
        "--offline",
        action="store_true",
        help="Skip network calls; use local fixtures for issue fetching.",
    )
    run_cmd.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Emit session context as JSON to stdout instead of human-readable table.",
    )
    run_cmd.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print additional details and structured execution logs.",
    )

    # ── Command: triage ───────────────────────────────────────────────────────
    triage_cmd = sub.add_parser(
        "triage",
        help="Run three-tier verification triage on an issue without modifying code.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    triage_cmd.add_argument(
        "--repo",
        required=True,
        metavar="<path-or-url>",
        help="Target repository path.",
    )
    triage_cmd.add_argument(
        "--issue",
        required=True,
        metavar="<id-or-file>",
        help="Issue URL, number, or path.",
    )
    triage_cmd.add_argument(
        "--title",
        default="",
        help="Optional explicit issue title override.",
    )
    triage_cmd.add_argument(
        "--use-bob", "--bob",
        dest="use_bob",
        action="store_true",
        help="Route triage verification through IBM Bob 2.0 Agent Mode custom modes.",
    )
    triage_cmd.add_argument(
        "--offline",
        action="store_true",
        help="Use local fixtures / offline issue parsing.",
    )
    triage_cmd.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress ASCII summary table output.",
    )

    # ── Command: pr ───────────────────────────────────────────────────────────
    pr_cmd = sub.add_parser(
        "pr",
        help="Emit PR draft and optionally create GitHub pull request from current fix.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    pr_cmd.add_argument(
        "--repo",
        default=".",
        metavar="<path>",
        help="Repository root path (default: .).",
    )
    pr_cmd.add_argument(
        "--create-pr",
        action="store_true",
        help="Push branch to origin and create a GitHub pull request via gh CLI.",
    )
    pr_cmd.add_argument(
        "--base-branch",
        default="main",
        metavar="<branch>",
        help="Base branch for the pull request (default: main).",
    )
    pr_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Git commit/push operations; write PR draft to .bob/dry_run/.",
    )

    return parser


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _cmd_run(args: argparse.Namespace) -> int:
    verbose: bool = args.verbose

    # 1. Resolve repository
    if verbose:
        print(_dim("→ Resolving repository…"), file=sys.stderr)
    repo_path = _resolve_repo(args.repo, verbose)

    # 2. Build repo profile
    if verbose:
        print(_dim("→ Building repository profile…"), file=sys.stderr)
    try:
        profile = build_repo_profile(repo_path)
    except FileNotFoundError as exc:
        print(_red(f"Error: {exc}"), file=sys.stderr)
        return 1

    # 3. Fetch issue
    if verbose:
        print(_dim(f"→ Fetching issue: {args.issue}"), file=sys.stderr)
    try:
        issue = fetch_issue(args.issue, offline=args.offline)
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(_red(f"Error fetching issue: {exc}"), file=sys.stderr)
        return 1

    # 4. Build session context
    ctx = _build_session_context(profile, issue, repo_path, args.mode)

    # 5. Output
    if args.output_json:
        json.dump(ctx, sys.stdout, indent=2, default=str)
        print()
    else:
        _print_summary(ctx, verbose=verbose)

    # 6. Agent workspace preparation
    _prepare_workspace(ctx, repo_path, verbose)

    # 7. Execute mode
    use_bob = getattr(args, "use_bob", False)
    if args.mode in ("full", "fix"):
        from netrani.pipeline.orchestrator import run_pipeline
        return run_pipeline(
            issue_ref=args.issue,
            repo_root=repo_path,
            verbose=verbose,
            dry_run=args.dry_run,
            offline=args.offline,
            create_pr=args.create_pr,
            base_branch=args.base_branch,
            use_bob=use_bob,
        )

    if args.mode == "triage":
        if args.dry_run:
            print(_yellow("Dry-run complete. No agent session launched."))
            return 0
        from netrani.triage import orchestrator as triage_orchestrator
        try:
            triage_orchestrator.run(
                repo_path=repo_path,
                issue_reference=issue.get("url") or issue.get("title", args.issue),
                title=issue.get("title", ""),
                body="\n".join(issue.get("reproduction_trace", [])),
                suspect_symbols=issue.get("suspect_symbols", []),
                reproduction_trace=issue.get("reproduction_trace", []),
                issue_url=issue.get("url", ""),
                quiet=not verbose,
                use_bob=use_bob,
                escalate_to_bob=True,
            )
            return 0
        except Exception as exc:  # noqa: BLE001
            print(_red(f"Error during triage: {exc}"), file=sys.stderr)
            return 1

    return 0


def _cmd_triage(args: argparse.Namespace) -> int:
    repo_path = _resolve_repo(args.repo, verbose=False)
    issue = fetch_issue(args.issue, offline=args.offline)
    title = args.title or issue.get("title", "")
    use_bob = getattr(args, "use_bob", False)

    from netrani.triage import orchestrator as triage_orchestrator
    try:
        triage_orchestrator.run(
            repo_path=repo_path,
            issue_reference=issue.get("url") or title or args.issue,
            title=title,
            body="\n".join(issue.get("reproduction_trace", [])),
            suspect_symbols=issue.get("suspect_symbols", []),
            reproduction_trace=issue.get("reproduction_trace", []),
            issue_url=issue.get("url", ""),
            quiet=args.quiet,
            use_bob=use_bob,
            escalate_to_bob=True,
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(_red(f"Error during triage: {exc}"), file=sys.stderr)
        return 1


def _cmd_pr(args: argparse.Namespace) -> int:
    from netrani import config
    from netrani.pipeline.git_emitter import emit_pr_artifacts

    repo = Path(args.repo).expanduser().resolve()
    vpath = str(config.verdict_path(repo))
    alog = str(config.audit_log_path(repo))

    res = emit_pr_artifacts(
        verdict_path=vpath,
        audit_log_path=alog,
        repo_root=str(repo),
        dry_run=args.dry_run,
        create_pr=args.create_pr,
        base_branch=args.base_branch,
    )

    if res.get("status") == "ready":
        print(_green(f"✓ PR draft generated: {res['pr_draft_path']}"))
        if res.get("pr_url"):
            print(_green(f"✓ GitHub PR opened: {res['pr_url']}"))
        elif res.get("pr_command"):
            print(_cyan(f"→ To submit PR manually, run:\n  {res['pr_command']}"))
        return 0
    else:
        print(_red(f"✗ Failed to generate PR artifacts: {res.get('error')}"))
        return 1


def _prepare_workspace(ctx: dict[str, Any], repo_path: str, verbose: bool) -> None:
    """Write the session context payload to .netrani/ for downstream agent use."""
    workspace = Path(repo_path) / ".netrani"
    workspace.mkdir(exist_ok=True)
    context_file = workspace / "session_context.json"
    context_file.write_text(json.dumps(ctx, indent=2, default=str), encoding="utf-8")
    if verbose:
        print(_dim(f"→ Session context written to {context_file}"), file=sys.stderr)
    print(_green(f"✓ Workspace prepared: {context_file}"), file=sys.stderr)
    print(_green(f"✓ Ready for Agent Mode ({ctx['mode']})"), file=sys.stderr)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "run":
        return _cmd_run(args)
    if args.command == "triage":
        return _cmd_triage(args)
    if args.command == "pr":
        return _cmd_pr(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
