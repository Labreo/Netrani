"""
netrani/subagents/test_runner.py
Subagent 4 — Dynamic Test Execution and Sensor Auditor.

Reads lint and test commands from the runtime profile produced by the
Document Parser, executes them in order against the modified fix branch,
and emits structured telemetry entries into ``.bob/audit.log`` in
newline-delimited JSON format.

Supports a single targeted refinement loop: if any command fails the
failure report is returned to the orchestrator so that Subagent 3 can
attempt a focused patch correction, then the tests are re-run once.  A
second failure halts the pipeline without further retries.

Design constraints
------------------
- All external process calls use subprocess.run with explicit timeout,
  capture_output=True, and check=False — never shell=True.
- ``audit.log`` is append-only; each run is opened with a ``run_start``
  header entry.
- Timeouts are configurable via ``config.DEFAULT_COMMAND_TIMEOUT``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from netrani import config

# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Telemetry entry schema
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    """Mirrors the newline-delimited JSON schema in audit.log."""

    command: str
    timestamp_utc: str
    exit_code: int
    stdout_summary: str
    stderr_summary: str
    duration_seconds: float
    status: str  # "passed" | "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "timestamp_utc": self.timestamp_utc,
            "exit_code": self.exit_code,
            "stdout_summary": self.stdout_summary,
            "stderr_summary": self.stderr_summary,
            "duration_seconds": round(self.duration_seconds, 3),
            "status": self.status,
        }


# ---------------------------------------------------------------------------
# Audit log helpers
# ---------------------------------------------------------------------------


def _audit_log_path(repo_root: Path) -> Path:
    """Resolve the absolute path to audit.log for *repo_root*."""
    return config.audit_log_path(repo_root)


def _write_audit_entry(log_path: Path, entry: dict[str, Any]) -> None:
    """Append a single newline-delimited JSON entry to *log_path*."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _emit_run_start(log_path: Path, run_id: str) -> None:
    """Append the mandatory run-start header entry that demarcates each run."""
    entry = {
        "event": "run_start",
        "run_id": run_id,
        "timestamp_utc": datetime.now(tz=timezone.utc).isoformat(),
    }
    _write_audit_entry(log_path, entry)
    log.debug("Audit run_start emitted: run_id=%s", run_id)


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------


def _split_command(cmd: str) -> list[str]:
    """
    Split a command string into a list suitable for subprocess.run.

    Uses shlex.split to handle quoted arguments correctly.  Never invokes
    a shell — the list form is passed directly to the OS.
    """
    import shlex
    return shlex.split(cmd)


def _run_command(
    cmd: str,
    cwd: Path,
    timeout: int,
) -> CommandResult:
    """
    Execute *cmd* in *cwd* with a hard timeout.

    Returns a :class:`CommandResult` regardless of exit code.
    A timeout is recorded as ``exit_code=-1`` and ``status="failed"``.
    """
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    start = time.monotonic()

    try:
        proc = subprocess.run(
            _split_command(cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        duration = time.monotonic() - start
        exit_code = proc.returncode
        stdout_summary = proc.stdout[:500]
        stderr_summary = proc.stderr[:500]
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        exit_code = -1
        stdout_summary = (exc.stdout or b"")[:500]
        stderr_summary = f"Command timed out after {timeout}s"
        if isinstance(stdout_summary, bytes):
            stdout_summary = stdout_summary.decode(errors="replace")

    status = "passed" if exit_code == 0 else "failed"
    return CommandResult(
        command=cmd,
        timestamp_utc=timestamp,
        exit_code=exit_code,
        stdout_summary=stdout_summary,
        stderr_summary=stderr_summary,
        duration_seconds=duration,
        status=status,
    )


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------


def _load_profile(profile_path: str) -> dict[str, Any]:
    """Load the runtime profile JSON, returning an empty dict on failure."""
    ppath = Path(profile_path)
    if not ppath.exists():
        log.warning("Runtime profile not found at %s.", profile_path)
        return {}
    try:
        return json.loads(ppath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not parse runtime profile at %s: %s", profile_path, exc)
        return {}


def _collect_commands(profile: dict[str, Any]) -> list[str]:
    """
    Return the ordered list of commands to execute: lint first, then tests.

    Priority: profile["lint_commands"] → profile["test_commands"].
    Commands sourced from a RepoProfile TypedDict produced by doc_parser.
    """
    lint = profile.get("lint_commands", [])
    tests = profile.get("test_commands", [])
    return list(lint) + list(tests)


# ---------------------------------------------------------------------------
# Post-run hook
# ---------------------------------------------------------------------------


def _invoke_record_verdict_hook(
    aggregate_exit_code: int,
    repo_root: Path,
    hook_path: Path | None = None,
) -> None:
    """
    Invoke ``.bob/hooks/record-verdict.sh`` (if it exists and is executable),
    passing *aggregate_exit_code* as ``$1``.

    Failures in the hook are logged but never propagate — the hook must not
    disrupt the pipeline.
    """
    hook = hook_path or config.RECORD_VERDICT_HOOK
    # Resolve relative paths against repo_root
    if not hook.is_absolute():
        hook = repo_root / hook

    if not hook.exists() or not os.access(str(hook), os.X_OK):
        log.debug("record-verdict hook not found or not executable at %s; skipping.", hook)
        return

    try:
        subprocess.run(
            [str(hook), str(aggregate_exit_code)],
            cwd=str(repo_root),
            capture_output=True,
            timeout=30,
            check=False,
        )
        log.info("record-verdict hook invoked with exit_code=%d.", aggregate_exit_code)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("record-verdict hook failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_tests(
    profile_path: str,
    branch: str,
    repo_root: str = ".",
    timeout: int | None = None,
) -> dict[str, Any]:
    """
    Execute all lint and test commands found in the runtime profile against
    the current working tree (assumed to be on *branch*).

    Parameters
    ----------
    profile_path:
        Path to the runtime profile JSON (output of Document Parser).
    branch:
        Name of the Git branch currently under test — used for log labelling.
    repo_root:
        Absolute path to the target repository root.
    timeout:
        Per-command timeout in seconds.  Defaults to
        ``config.DEFAULT_COMMAND_TIMEOUT`` (300 s).

    Returns
    -------
    dict
        ``{ "overall_status": "passed" | "failed" | "skipped",
            "commands_run": int,
            "failures": list[dict],
            "audit_log_path": str }``
    """
    cmd_timeout = timeout or config.DEFAULT_COMMAND_TIMEOUT
    repo = Path(repo_root).expanduser().resolve()
    run_id = str(uuid.uuid4())
    alog_path = _audit_log_path(repo)

    # ── Emit run-start demarcation entry ─────────────────────────────────────
    _emit_run_start(alog_path, run_id)
    log.info("TestRunner: run_id=%s  branch=%s  repo=%s", run_id, branch, repo)

    # ── Load profile and collect commands ────────────────────────────────────
    profile = _load_profile(profile_path)
    commands = _collect_commands(profile)

    if not commands:
        log.warning(
            "No test or lint commands found in profile at %s. "
            "Returning overall_status='skipped'.",
            profile_path,
        )
        _write_audit_entry(alog_path, {
            "event": "no_commands",
            "run_id": run_id,
            "timestamp_utc": datetime.now(tz=timezone.utc).isoformat(),
            "message": "No test/lint commands found in runtime profile.",
        })
        return {
            "overall_status": "skipped",
            "commands_run": 0,
            "failures": [],
            "audit_log_path": str(alog_path),
        }

    # ── Execute each command and record telemetry ─────────────────────────────
    failures: list[dict[str, Any]] = []
    commands_run = 0

    for cmd in commands:
        log.info("Running: %s", cmd)
        result = _run_command(cmd, repo, cmd_timeout)
        commands_run += 1

        entry = result.to_dict()
        entry["run_id"] = run_id
        _write_audit_entry(alog_path, entry)
        log.info(
            "  → %s  (exit=%d  %.1fs)",
            result.status.upper(),
            result.exit_code,
            result.duration_seconds,
        )

        if result.status == "failed":
            failures.append({
                "command": cmd,
                "exit_code": result.exit_code,
                "stdout_summary": result.stdout_summary,
                "stderr_summary": result.stderr_summary,
                "duration_seconds": result.duration_seconds,
            })

    # ── Determine aggregate exit code ─────────────────────────────────────────
    aggregate_exit_code = 0 if not failures else 1
    overall_status = "passed" if not failures else "failed"

    # ── Invoke post-run hook ───────────────────────────────────────────────────
    _invoke_record_verdict_hook(aggregate_exit_code, repo)

    # ── Append run-end entry ──────────────────────────────────────────────────
    _write_audit_entry(alog_path, {
        "event": "run_end",
        "run_id": run_id,
        "timestamp_utc": datetime.now(tz=timezone.utc).isoformat(),
        "overall_status": overall_status,
        "commands_run": commands_run,
        "failures": len(failures),
    })

    return {
        "overall_status": overall_status,
        "commands_run": commands_run,
        "failures": failures,
        "audit_log_path": str(alog_path),
    }
