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

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netrani",
        description="Netrani — General-purpose issue triage and verification tool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              netrani run --repo . --issue 973
              netrani run --repo https://github.com/owner/repo --issue 973 --mode triage
              netrani run --repo . --issue /path/to/issue.md --mode full --verbose
              netrani run --repo . --issue 973 --dry-run
            """
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    run_cmd = sub.add_parser(
        "run",
        help="Run Netrani triage/fix on a repository and issue.",
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
        "--dry-run",
        action="store_true",
        help="Discover and display the session context without launching an agent.",
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
        help="Emit session context as JSON to stdout instead of a human-readable table.",
    )
    run_cmd.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print additional details (reproduction trace, environment).",
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

    if args.dry_run:
        print(_yellow("Dry-run complete. No agent session launched."))
        return 0

    # 6. Agent workspace preparation
    _prepare_workspace(ctx, repo_path, verbose)

    return 0


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

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
