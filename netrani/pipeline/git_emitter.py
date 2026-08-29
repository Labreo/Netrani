"""
netrani/pipeline/git_emitter.py
Submittable Artifact and Git Branch / PR Draft Generator.

Responsibilities
----------------
1. Create a signed-off semantic commit on the ``netrani/fix-issue-<id>``
   branch using all metadata sourced from ``.bob/verdict.json`` and
   ``.bob/patch.diff`` — no hardcoded strings.
2. Write a ready-to-submit Markdown PR draft to ``.bob/pr_draft.md``
   containing five mandatory sections sourced from verdict.json, patch.diff,
   audit.log, and the repository's CONTRIBUTING.md.

Design constraints
------------------
- All file paths via ``netrani.config``.
- All external process calls use subprocess.run with explicit timeout,
  capture_output=True, and check=False — never shell=True.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from netrani import config

# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run a git sub-command in *cwd*. Never uses shell=True."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _current_branch(repo_root: Path) -> str:
    """Return the name of the currently checked-out branch."""
    result = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    return result.stdout.strip() if result.returncode == 0 else "HEAD"


def _head_sha(repo_root: Path) -> str:
    """Return the current HEAD commit SHA (full 40-hex)."""
    result = _git(["rev-parse", "HEAD"], repo_root)
    return result.stdout.strip() if result.returncode == 0 else ""


# ---------------------------------------------------------------------------
# Commit message building
# ---------------------------------------------------------------------------


def _derive_scope(files_modified: list[str]) -> str:
    """
    Derive the primary module / directory scope from the list of modified
    files for use in the semantic commit message.

    Takes the first common directory component shared by the majority of
    modified files, falling back to the top-level directory of the first file.
    """
    if not files_modified:
        return "fix"

    parts_list = [Path(f).parts for f in files_modified if f]
    if not parts_list:
        return "fix"

    # Use the topmost directory of the first modified file
    first_parts = parts_list[0]
    if len(first_parts) > 1:
        return first_parts[0]  # e.g. "netrani" or "src"

    # Single component — strip extension
    return Path(files_modified[0]).stem or "fix"


def _build_commit_message(
    verdict: dict[str, Any],
    patch_summary: str,
    files_modified: list[str],
) -> str:
    """
    Construct the exact semantic commit message required by the spec.

    Format::

        fix(<scope>): resolve <issue-title>

        Root cause: <rationale>
        Resolution: <patch_summary>
        Citation: <citation>

        Assisted-by: IBM Bob 2.0 / Netrani
    """
    issue_title = verdict.get("issue_reference", "unknown issue")
    # Trim long URLs to a readable title fragment
    if issue_title.startswith("http"):
        # e.g. https://github.com/owner/repo/issues/42  → #42
        m = re.search(r"/issues/(\d+)$", issue_title)
        issue_title = f"#{m.group(1)}" if m else issue_title

    scope = _derive_scope(files_modified)
    rationale = verdict.get("rationale", "(see triage verdict)").strip()
    citation = verdict.get("citation", "(see verdict.json)").strip()

    # Truncate long rationale for the commit body — keep it readable
    if len(rationale) > 200:
        rationale = rationale[:197] + "..."

    resolution = patch_summary.strip() or "Minimal surgical patch applied per triage citation."

    return (
        f"fix({scope}): resolve {issue_title}\n"
        "\n"
        f"Root cause: {rationale}\n"
        f"Resolution: {resolution}\n"
        f"Citation: {citation}\n"
        "\n"
        "Assisted-by: IBM Bob 2.0 / Netrani\n"
    )


# ---------------------------------------------------------------------------
# Patch diff summary extraction
# ---------------------------------------------------------------------------


def _summarise_patch(patch_text: str) -> str:
    """
    Derive a human-readable one-line summary from a unified diff.

    Extracts the first ``---``/``+++`` filenames and counts hunks added.
    """
    lines = patch_text.splitlines()
    files: list[str] = []
    additions = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    deletions = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))

    for ln in lines:
        if ln.startswith("+++ b/"):
            files.append(ln[6:].strip())

    file_list = ", ".join(files[:3]) if files else "unknown file(s)"
    return (
        f"Modified {file_list}: +{additions} line(s), -{deletions} line(s)."
    )


# ---------------------------------------------------------------------------
# PR draft Markdown generation
# ---------------------------------------------------------------------------


def _load_audit_log(audit_log_path: Path) -> list[dict[str, Any]]:
    """
    Read *audit_log_path* and return a list of parsed JSON objects.

    Only entries that contain a ``command`` field are returned (i.e.
    run_start / run_end meta-entries are excluded from the table).
    """
    entries: list[dict[str, Any]] = []
    if not audit_log_path.exists():
        return entries
    for raw_line in audit_log_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
            if "command" in obj and "exit_code" in obj:
                entries.append(obj)
        except json.JSONDecodeError:
            continue
    return entries


def _parse_contributing_checklist(repo_root: Path) -> list[str]:
    """
    Extract every ``- [ ]`` checkbox item from the repository's
    CONTRIBUTING.md and return the raw text of each item.

    If no CONTRIBUTING.md exists, return a default checklist.
    """
    contrib_candidates = [
        "CONTRIBUTING.md", "CONTRIBUTING.rst",
        "DEVELOPING.md", "docs/CONTRIBUTING.md",
    ]
    for candidate in contrib_candidates:
        fpath = repo_root / candidate
        if fpath.exists():
            text = fpath.read_text(encoding="utf-8", errors="replace")
            # Match GitHub-flavoured Markdown checkbox patterns
            items = re.findall(r"^[-*]\s+\[ \]\s+(.+)$", text, re.MULTILINE)
            if items:
                return items

    # Default checklist if CONTRIBUTING.md absent or has no checkboxes
    return [
        "Tests added or updated to cover the change",
        "Documentation updated (if applicable)",
        "No unrelated changes included in this PR",
        "Commit message follows the project format",
    ]


def _build_pr_draft(
    verdict: dict[str, Any],
    patch_text: str,
    audit_entries: list[dict[str, Any]],
    repo_root: Path,
    files_modified: list[str],
    branch: str,
    commit_sha: str,
) -> str:
    """Build the complete PR draft Markdown document."""

    issue_ref = verdict.get("issue_reference", "unknown")
    citation = verdict.get("citation", "(none)")
    rationale = verdict.get("rationale", "(none)")
    patch_summary = _summarise_patch(patch_text) if patch_text else "(no patch data)"
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Section 1: Issue Reference ────────────────────────────────────────────
    if issue_ref.startswith("http"):
        issue_link = f"[{issue_ref}]({issue_ref})"
    else:
        issue_link = issue_ref

    section_issue = (
        "## Issue Reference\n\n"
        f"- **Issue:** {issue_link}\n"
        f"- **Triage Citation:** `{citation}`\n"
        f"- **Branch:** `{branch}`\n"
        f"- **Commit:** `{commit_sha[:12] if commit_sha else 'n/a'}`\n"
        f"- **Generated:** {timestamp}\n"
    )

    # ── Section 2: Root Cause Summary ────────────────────────────────────────
    section_root_cause = (
        "## Root Cause Summary\n\n"
        f"{rationale}\n"
    )

    # ── Section 3: Change Summary ─────────────────────────────────────────────
    modified_list = (
        "\n".join(f"- `{f}`" for f in files_modified)
        if files_modified
        else "_(none detected)_"
    )
    section_changes = (
        "## Change Summary\n\n"
        f"{patch_summary}\n\n"
        "**Files modified:**\n\n"
        f"{modified_list}\n"
    )

    # ── Section 4: Verification Summary ──────────────────────────────────────
    if audit_entries:
        header = (
            "| Command | Exit Code | Duration (s) | Status |\n"
            "|---------|-----------|-------------|--------|\n"
        )
        rows = ""
        for entry in audit_entries:
            cmd = entry.get("command", "")[:60]
            ec = entry.get("exit_code", "?")
            dur = f"{entry.get('duration_seconds', 0):.1f}"
            st = entry.get("status", "unknown")
            badge = "✅" if st == "passed" else "❌"
            rows += f"| `{cmd}` | `{ec}` | {dur} | {badge} {st} |\n"
        table = header + rows
    else:
        table = "_No test commands were recorded in audit.log._\n"

    section_verification = "## Verification Summary\n\n" + table

    # ── Section 5: Compliance Checklist ───────────────────────────────────────
    checklist_items = _parse_contributing_checklist(repo_root)
    checklist = "\n".join(f"- [ ] {item}" for item in checklist_items)
    section_checklist = (
        "## Compliance Checklist\n\n"
        "_Complete the following before requesting review:_\n\n"
        f"{checklist}\n"
    )

    # ── Assemble document ─────────────────────────────────────────────────────
    title = f"# Fix: {issue_ref}\n\n> Auto-generated by IBM Bob 2.0 / Netrani\n"

    return "\n".join([
        title,
        section_issue,
        section_root_cause,
        section_changes,
        section_verification,
        section_checklist,
    ])


# ---------------------------------------------------------------------------
# GitHub PR submission helpers
# ---------------------------------------------------------------------------


def check_gh_cli_status() -> dict[str, Any]:
    """Check if GitHub CLI (gh) is installed and authenticated."""
    try:
        ver = subprocess.run(
            ["gh", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if ver.returncode != 0:
            return {"installed": False, "authenticated": False, "error": "gh command not found"}
        auth = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        is_auth = auth.returncode == 0
        return {
            "installed": True,
            "authenticated": is_auth,
            "error": "" if is_auth else auth.stderr.strip() or auth.stdout.strip(),
        }
    except FileNotFoundError:
        return {"installed": False, "authenticated": False, "error": "gh not found in PATH"}
    except Exception as exc:  # noqa: BLE001
        return {"installed": False, "authenticated": False, "error": str(exc)}


def create_github_pr(
    repo_root: Path,
    branch: str,
    pr_draft_path: Path,
    title: str = "",
    base: str = "main",
    push: bool = True,
) -> dict[str, Any]:
    """
    Push the fix branch to remote and create a GitHub PR using gh CLI.

    If gh is not authenticated or not installed, returns a structured result
    containing the exact CLI command to run manually.
    """
    if push:
        log.info("Pushing branch %s to origin...", branch)
        push_res = _git(["push", "-u", "origin", branch], repo_root, timeout=60)
        if push_res.returncode != 0:
            log.warning("git push failed: %s", push_res.stderr.strip())

    cli_status = check_gh_cli_status()
    pr_cmd_args = [
        "gh", "pr", "create",
        "--title", title or f"fix: resolve {branch}",
        "--body-file", str(pr_draft_path),
        "--head", branch,
        "--base", base,
    ]
    pr_cmd_str = " ".join(f'"{a}"' if " " in a else a for a in pr_cmd_args)

    if not cli_status["installed"]:
        return {
            "success": False,
            "pr_url": "",
            "command": pr_cmd_str,
            "error": "GitHub CLI (gh) is not installed.",
            "manual_instructions": f"Install gh and run: {pr_cmd_str}",
        }

    if not cli_status["authenticated"]:
        return {
            "success": False,
            "pr_url": "",
            "command": pr_cmd_str,
            "error": "GitHub CLI is not authenticated. Run 'gh auth login' first.",
            "manual_instructions": f"1. Run 'gh auth login'\n2. Run: {pr_cmd_str}",
        }

    try:
        proc = subprocess.run(
            pr_cmd_args,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode == 0:
            pr_url = proc.stdout.strip()
            log.info("Successfully created GitHub PR: %s", pr_url)
            return {
                "success": True,
                "pr_url": pr_url,
                "command": pr_cmd_str,
                "error": "",
                "manual_instructions": "",
            }
        err = proc.stderr.strip() or proc.stdout.strip()
        log.error("gh pr create failed: %s", err)
        return {
            "success": False,
            "pr_url": "",
            "command": pr_cmd_str,
            "error": f"gh pr create failed: {err}",
            "manual_instructions": f"Execute manually: {pr_cmd_str}",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "pr_url": "",
            "command": pr_cmd_str,
            "error": str(exc),
            "manual_instructions": f"Execute manually: {pr_cmd_str}",
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def emit_pr_artifacts(
    verdict_path: str,
    audit_log_path: str,
    repo_root: str = ".",
    dry_run: bool = False,
    dry_run_dir: str | None = None,
    create_pr: bool = False,
    base_branch: str = "main",
) -> dict[str, Any]:
    """
    Create the semantic Git commit, write the PR draft Markdown file,
    and optionally publish the pull request to GitHub.

    Parameters
    ----------
    verdict_path:
        Path to ``.bob/verdict.json``.
    audit_log_path:
        Path to ``.bob/audit.log``.
    repo_root:
        Absolute path to the target repository root.
    dry_run:
        When True, skip all Git operations and write artefacts to
        *dry_run_dir* instead of ``.bob/``.
    dry_run_dir:
        Output directory override used when *dry_run* is True.
    create_pr:
        When True and not dry-run, push branch and invoke ``gh pr create``.
    base_branch:
        Target base branch for the PR (default: ``main``).

    Returns
    -------
    dict
        ``{ "branch": str, "commit_sha": str, "pr_draft_path": str,
            "status": "ready" | "failed", "pr_url": str, "pr_command": str }``
    """
    repo = Path(repo_root).expanduser().resolve()

    # ── Load verdict ──────────────────────────────────────────────────────────
    vpath = Path(verdict_path)
    if not vpath.exists():
        log.error("Verdict file not found at %s.", vpath)
        return {"branch": "", "commit_sha": "", "pr_draft_path": "", "status": "failed",
                "error": f"Verdict file not found: {vpath}"}
    try:
        raw_obj: Any = json.loads(vpath.read_text(encoding="utf-8"))
        if isinstance(raw_obj, list):
            valid_items = [item for item in raw_obj if isinstance(item, dict) and item.get("status") == "VALID"]
            verdict = valid_items[0] if valid_items else (raw_obj[0] if raw_obj and isinstance(raw_obj[0], dict) else {})
            if "issue" in verdict and "issue_reference" not in verdict:
                verdict["issue_reference"] = verdict["issue"]
        elif isinstance(raw_obj, dict):
            verdict = raw_obj
        else:
            verdict = {}
    except (json.JSONDecodeError, OSError) as exc:
        return {"branch": "", "commit_sha": "", "pr_draft_path": "", "status": "failed",
                "error": f"Cannot read verdict file: {exc}"}

    # ── Determine branch name ─────────────────────────────────────────────────
    branch = _current_branch(repo)

    # ── Load patch diff ───────────────────────────────────────────────────────
    patch_path = config.patch_diff_path(repo)
    patch_text = patch_path.read_text(encoding="utf-8") if patch_path.exists() else ""

    patch_summary = _summarise_patch(patch_text) if patch_text else ""

    # Derive modified files from the diff header lines
    files_modified: list[str] = re.findall(r"^\+\+\+ b/(.+)$", patch_text, re.MULTILINE)

    # ── Load audit log entries ────────────────────────────────────────────────
    alog = Path(audit_log_path)
    audit_entries = _load_audit_log(alog)

    # ── Create commit (unless dry-run) ────────────────────────────────────────
    commit_sha = ""

    if not dry_run:
        commit_msg = _build_commit_message(verdict, patch_summary, files_modified)

        # Stage all modified files (already indexed by git apply --index)
        stage_result = _git(["diff", "--cached", "--name-only"], repo)
        if stage_result.stdout.strip():
            # Files are already staged; commit them
            commit_result = _git(
                ["commit", "--signoff", "-m", commit_msg],
                repo,
                timeout=30,
            )
            if commit_result.returncode != 0:
                log.error("Git commit failed: %s", commit_result.stderr.strip())
                return {
                    "branch": branch,
                    "commit_sha": "",
                    "pr_draft_path": "",
                    "status": "failed",
                    "error": f"Git commit failed: {commit_result.stderr.strip()}",
                }
            commit_sha = _head_sha(repo)
            log.info("Committed fix: %s", commit_sha[:12])
        else:
            log.warning(
                "No staged changes found — skipping commit.  "
                "Patch may not have been applied via 'git apply --index'."
            )

    # ── Resolve output directory ───────────────────────────────────────────────
    if dry_run:
        out_dir = Path(dry_run_dir) if dry_run_dir else config.DRY_RUN_DIR
        if not out_dir.is_absolute():
            out_dir = repo / out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        pr_out_path = out_dir / "pr_draft.md"
    else:
        pr_out_path = config.pr_draft_path(repo)
        pr_out_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Build PR draft ────────────────────────────────────────────────────────
    pr_content = _build_pr_draft(
        verdict=verdict,
        patch_text=patch_text,
        audit_entries=audit_entries,
        repo_root=repo,
        files_modified=files_modified,
        branch=branch,
        commit_sha=commit_sha,
    )
    pr_out_path.write_text(pr_content, encoding="utf-8")
    log.info("PR draft written to %s", pr_out_path)

    # ── Create Live GitHub PR (if requested) ──────────────────────────────────
    pr_url = ""
    pr_command = ""
    pr_error = ""

    if create_pr and not dry_run:
        issue_title = verdict.get("issue_reference", "issue")
        if issue_title.startswith("http"):
            m = re.search(r"/issues/(\d+)$", issue_title)
            issue_title = f"#{m.group(1)}" if m else issue_title
        scope = _derive_scope(files_modified)
        pr_title = f"fix({scope}): resolve {issue_title}"

        pr_res = create_github_pr(
            repo_root=repo,
            branch=branch,
            pr_draft_path=pr_out_path,
            title=pr_title,
            base=base_branch,
            push=True,
        )
        pr_url = pr_res.get("pr_url", "")
        pr_command = pr_res.get("command", "")
        pr_error = pr_res.get("error", "")

    return {
        "branch": branch,
        "commit_sha": commit_sha,
        "pr_draft_path": str(pr_out_path),
        "status": "ready",
        "pr_url": pr_url,
        "pr_command": pr_command,
        "pr_error": pr_error,
    }
