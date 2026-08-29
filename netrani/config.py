"""
netrani/config.py
Central configuration for all Netrani file paths and runtime constants.

All path references used by subagents and pipeline modules must be sourced
from this module.  No path strings are permitted to be hardcoded elsewhere.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Base directories
# ---------------------------------------------------------------------------

#: Repository root for the *Netrani tool itself* (not the target repo).
NETRANI_ROOT: Path = Path(__file__).parent.parent.resolve()

#: The .bob workspace directory — relative to cwd at runtime.
BOB_DIR: Path = Path(".bob")

# ---------------------------------------------------------------------------
# Verdict / triage artefacts
# ---------------------------------------------------------------------------

#: Triage verdict written by the triage orchestrator.
VERDICT_PATH: Path = BOB_DIR / "verdict.json"

#: Unified audit log — append-only, never truncated between runs.
AUDIT_LOG_PATH: Path = BOB_DIR / "audit.log"

#: Applied patch diff written after a surgical fix.
PATCH_DIFF_PATH: Path = BOB_DIR / "patch.diff"

#: Pull-request draft Markdown article.
PR_DRAFT_PATH: Path = BOB_DIR / "pr_draft.md"

#: Final run summary written at the end of every pipeline execution.
RUN_SUMMARY_PATH: Path = BOB_DIR / "run_summary.json"

# ---------------------------------------------------------------------------
# Dry-run output directory
# ---------------------------------------------------------------------------

#: All artefacts are redirected here when --dry-run is active.
DRY_RUN_DIR: Path = BOB_DIR / "dry_run"

# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

#: Shell hook invoked after all tests complete; receives the aggregate
#: exit code as its first positional argument.
RECORD_VERDICT_HOOK: Path = BOB_DIR / "hooks" / "record-verdict.sh"

# ---------------------------------------------------------------------------
# Branch naming
# ---------------------------------------------------------------------------

#: Prefix for fix branches created by the surgical fixer.
FIX_BRANCH_PREFIX: str = "netrani/fix-issue-"

# ---------------------------------------------------------------------------
# Test execution defaults
# ---------------------------------------------------------------------------

#: Default per-command timeout in seconds.
DEFAULT_COMMAND_TIMEOUT: int = int(os.environ.get("NETRANI_CMD_TIMEOUT", "300"))

# ---------------------------------------------------------------------------
# Verdict schema
# ---------------------------------------------------------------------------

#: JSON Schema file for verdict validation (stdlib-validated inline; file
#: kept for documentation purposes).
VERDICT_SCHEMA_PATH: Path = BOB_DIR / "verdict.schema.json"

# ---------------------------------------------------------------------------
# Helper: resolve a path relative to an arbitrary repo root
# ---------------------------------------------------------------------------


def bob_dir(repo_root: str | Path) -> Path:
    """Return the absolute .bob directory path for *repo_root*."""
    return Path(repo_root).resolve() / ".bob"


def verdict_path(repo_root: str | Path) -> Path:
    """Return the absolute verdict.json path for *repo_root*."""
    return bob_dir(repo_root) / "verdict.json"


def audit_log_path(repo_root: str | Path) -> Path:
    """Return the absolute audit.log path for *repo_root*."""
    return bob_dir(repo_root) / "audit.log"


def patch_diff_path(repo_root: str | Path) -> Path:
    """Return the absolute patch.diff path for *repo_root*."""
    return bob_dir(repo_root) / "patch.diff"


def pr_draft_path(repo_root: str | Path) -> Path:
    """Return the absolute pr_draft.md path for *repo_root*."""
    return bob_dir(repo_root) / "pr_draft.md"


def run_summary_path(repo_root: str | Path) -> Path:
    """Return the absolute run_summary.json path for *repo_root*."""
    return bob_dir(repo_root) / "run_summary.json"
