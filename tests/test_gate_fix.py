"""
tests/test_gate_fix.py
Safety hook verification for .bob/hooks/gate-fix.sh.

Proves that gate-fix.sh:
  - Exits 0  when .bob/verdict.json contains status = "VALID".
  - Exits 2  when status is "DUPLICATE".
  - Exits 2  when status is "OBSOLETE".
  - Exits 2  when status is "FALSE_POSITIVE".
  - Exits 2  when verdict.json is absent.
  - Exits 2  when verdict.json is malformed (empty / invalid JSON).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GATE_SCRIPT = Path(__file__).parent.parent / ".bob" / "hooks" / "gate-fix.sh"


def _run_gate(verdict_path: Path | None, tmp_path: Path) -> subprocess.CompletedProcess:
    """
    Run gate-fix.sh from *tmp_path* (which acts as the fake repo root).
    If *verdict_path* is provided it is copied into tmp_path/.bob/verdict.json.
    """
    # The script reads ".bob/verdict.json" relative to cwd
    bob_dir = tmp_path / ".bob"
    bob_dir.mkdir(parents=True, exist_ok=True)

    if verdict_path is not None:
        dest = bob_dir / "verdict.json"
        dest.write_text(verdict_path.read_text(encoding="utf-8"), encoding="utf-8")

    return subprocess.run(
        ["bash", str(GATE_SCRIPT.resolve())],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )


def _write_verdict(directory: Path, status: str) -> Path:
    """Write a minimal verdict.json with the given *status* to *directory*."""
    vfile = directory / "verdict.json"
    vfile.write_text(
        json.dumps({
            "status": status,
            "citation": "test-citation",
            "rationale": "Automated test rationale string — at least ten chars.",
            "confidence": 0.95,
            "timestamp": "2025-07-15T10:30:00Z",
            "target_repo": "owner/repo",
            "issue_reference": "https://github.com/owner/repo/issues/1",
        }),
        encoding="utf-8",
    )
    return vfile


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGateFixAllowsValid:
    """gate-fix.sh MUST exit 0 when verdict is VALID."""

    def test_valid_verdict_exits_zero(self, tmp_path: Path) -> None:
        vfile = _write_verdict(tmp_path, "VALID")
        result = _run_gate(vfile, tmp_path)
        assert result.returncode == 0, (
            f"Expected exit 0 for VALID verdict, got {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "VALID" in result.stdout


class TestGateFixBlocksNonValid:
    """gate-fix.sh MUST exit 2 for every non-VALID status."""

    @pytest.mark.parametrize("status", ["DUPLICATE", "OBSOLETE", "FALSE_POSITIVE"])
    def test_blocks_with_exit_2(self, status: str, tmp_path: Path) -> None:
        vfile = _write_verdict(tmp_path, status)
        result = _run_gate(vfile, tmp_path)
        assert result.returncode == 2, (
            f"Expected exit 2 for {status} verdict, got {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "BLOCKED" in result.stdout or "CODE MODIFICATION BLOCKED" in result.stdout

    def test_blocks_when_verdict_file_missing(self, tmp_path: Path) -> None:
        result = _run_gate(None, tmp_path)
        assert result.returncode == 2, (
            f"Expected exit 2 when no verdict file, got {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_blocks_on_empty_verdict_file(self, tmp_path: Path) -> None:
        bob_dir = tmp_path / ".bob"
        bob_dir.mkdir(parents=True, exist_ok=True)
        empty_vfile = bob_dir / "verdict.json"
        empty_vfile.write_text("", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(GATE_SCRIPT.resolve())],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, (
            f"Expected exit 2 for empty verdict file, got {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_blocks_on_malformed_verdict_file(self, tmp_path: Path) -> None:
        bob_dir = tmp_path / ".bob"
        bob_dir.mkdir(parents=True, exist_ok=True)
        bad_vfile = bob_dir / "verdict.json"
        bad_vfile.write_text("{not valid json", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(GATE_SCRIPT.resolve())],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, (
            f"Expected exit 2 for malformed verdict, got {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_blocks_on_unknown_status(self, tmp_path: Path) -> None:
        bob_dir = tmp_path / ".bob"
        bob_dir.mkdir(parents=True, exist_ok=True)
        vfile = bob_dir / "verdict.json"
        vfile.write_text(json.dumps({"status": "UNKNOWN_STATUS"}), encoding="utf-8")
        result = subprocess.run(
            ["bash", str(GATE_SCRIPT.resolve())],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, (
            f"Expected exit 2 for unknown status, got {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
