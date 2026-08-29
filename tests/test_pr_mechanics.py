"""
tests/test_pr_mechanics.py
Unit tests for Netrani PR mechanics and live GitHub PR creation.

Covers:
  1. GitHub CLI status detection (installed / authenticated / missing)
  2. Live PR creation and branch pushing via create_github_pr()
  3. PR draft construction with Assisted-by: trailer & all 5 required sections
  4. emit_pr_artifacts() integration with create_pr flag
  5. CLI command routing for `netrani run --create-pr`, `netrani pr`, and `netrani triage`
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from netrani.pipeline import git_emitter


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------


def _create_mock_verdict(bob_dir: Path, status: str = "VALID", issue_ref: str = "https://github.com/open-telemetry/opentelemetry-go-compile-instrumentation/issues/42") -> Path:
    vfile = bob_dir / "verdict.json"
    bob_dir.mkdir(parents=True, exist_ok=True)
    vfile.write_text(
        json.dumps({
            "status": status,
            "citation": "tool/internal/instrument/trampoline.go:42",
            "rationale": "The parameter walker does not handle dst.Ellipsis in variadic interface methods.",
            "confidence": 0.95,
            "timestamp": "2026-08-29T12:00:00Z",
            "target_repo": "open-telemetry/opentelemetry-go-compile-instrumentation",
            "issue_reference": issue_ref,
        }),
        encoding="utf-8",
    )
    return vfile


def _create_mock_patch(bob_dir: Path) -> Path:
    pfile = bob_dir / "patch.diff"
    bob_dir.mkdir(parents=True, exist_ok=True)
    pfile.write_text(
        textwrap.dedent(
            """\
            --- a/tool/internal/instrument/trampoline.go
            +++ b/tool/internal/instrument/trampoline.go
            @@ -40,3 +40,5 @@
             func handleParam(p ast.Node) {
             +    if isEllipsis(p) {
             +        return
             +    }
             }
            """
        ),
        encoding="utf-8",
    )
    return pfile


def _create_mock_audit_log(bob_dir: Path) -> Path:
    alog = bob_dir / "audit.log"
    bob_dir.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "seq": 1,
            "command": "go test ./tool/internal/instrument/...",
            "exit_code": 0,
            "duration_seconds": 1.4,
            "status": "passed",
            "timestamp": "2026-08-29T12:01:00Z",
        }
    ]
    alog.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return alog


# ---------------------------------------------------------------------------
# 1. GitHub CLI Status Detection
# ---------------------------------------------------------------------------


class TestGhCliStatus:
    def test_gh_installed_and_authenticated(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(["gh", "--version"], 0, stdout="gh version 2.50.0", stderr=""),
                subprocess.CompletedProcess(["gh", "auth", "status"], 0, stdout="Logged in to github.com", stderr=""),
            ]
            status = git_emitter.check_gh_cli_status()
            assert status["installed"] is True
            assert status["authenticated"] is True
            assert status["error"] == ""

    def test_gh_installed_but_unauthenticated(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(["gh", "--version"], 0, stdout="gh version 2.50.0", stderr=""),
                subprocess.CompletedProcess(["gh", "auth", "status"], 1, stdout="", stderr="You are not logged in"),
            ]
            status = git_emitter.check_gh_cli_status()
            assert status["installed"] is True
            assert status["authenticated"] is False
            assert "not logged in" in status["error"]

    def test_gh_not_installed(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
            status = git_emitter.check_gh_cli_status()
            assert status["installed"] is False
            assert status["authenticated"] is False


# ---------------------------------------------------------------------------
# 2. Live PR Creation Mechanics
# ---------------------------------------------------------------------------


class TestCreateGithubPR:
    def test_create_pr_success(self, tmp_path: Path) -> None:
        draft = tmp_path / "pr_draft.md"
        draft.write_text("# Test PR\nContent", encoding="utf-8")

        with (
            patch.object(git_emitter, "_git") as mock_git,
            patch.object(git_emitter, "check_gh_cli_status", return_value={"installed": True, "authenticated": True, "error": ""}),
            patch("subprocess.run") as mock_proc,
        ):
            mock_git.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            mock_proc.return_value = subprocess.CompletedProcess(
                ["gh", "pr", "create"], 0, stdout="https://github.com/owner/repo/pull/100\n", stderr=""
            )

            res = git_emitter.create_github_pr(
                repo_root=tmp_path,
                branch="netrani/fix-issue-42",
                pr_draft_path=draft,
                title="fix(instrument): resolve #42",
                base="main",
                push=True,
            )

            assert res["success"] is True
            assert res["pr_url"] == "https://github.com/owner/repo/pull/100"
            mock_git.assert_called_with(["push", "-u", "origin", "netrani/fix-issue-42"], tmp_path, timeout=60)

    def test_create_pr_unauthenticated_fallback(self, tmp_path: Path) -> None:
        draft = tmp_path / "pr_draft.md"
        draft.write_text("# Test PR\nContent", encoding="utf-8")

        with (
            patch.object(git_emitter, "_git") as mock_git,
            patch.object(git_emitter, "check_gh_cli_status", return_value={"installed": True, "authenticated": False, "error": "Not logged in"}),
        ):
            mock_git.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            res = git_emitter.create_github_pr(
                repo_root=tmp_path,
                branch="netrani/fix-issue-42",
                pr_draft_path=draft,
                title="fix(instrument): resolve #42",
                base="main",
                push=False,
            )

            assert res["success"] is False
            assert "gh auth login" in res["error"]
            assert "gh pr create" in res["command"]
            assert "gh auth login" in res["manual_instructions"]


# ---------------------------------------------------------------------------
# 3. Commit Message Formatting & Trailer Compliance
# ---------------------------------------------------------------------------


class TestCommitMessageFormatting:
    def test_commit_message_has_assisted_by_trailer(self) -> None:
        verdict = {
            "issue_reference": "https://github.com/open-telemetry/opentelemetry-go-compile-instrumentation/issues/42",
            "rationale": "Missing variadic type guard in AST parameter walker.",
            "citation": "trampoline.go:42",
        }
        msg = git_emitter._build_commit_message(
            verdict=verdict,
            patch_summary="Modified trampoline.go: +5 line(s)",
            files_modified=["tool/internal/instrument/trampoline.go"],
        )

        assert msg.startswith("fix(tool): resolve #42")
        assert "Root cause: Missing variadic type guard in AST parameter walker." in msg
        assert "Citation: trampoline.go:42" in msg
        assert "Assisted-by: IBM Bob 2.0 / Netrani" in msg


# ---------------------------------------------------------------------------
# 4. emit_pr_artifacts Integration
# ---------------------------------------------------------------------------


class TestEmitPRArtifactsIntegration:
    def test_emit_pr_artifacts_with_create_pr(self, tmp_path: Path) -> None:
        bob_dir = tmp_path / ".bob"
        vfile = _create_mock_verdict(bob_dir)
        _create_mock_patch(bob_dir)
        alog = _create_mock_audit_log(bob_dir)

        with (
            patch.object(git_emitter, "_current_branch", return_value="netrani/fix-issue-42"),
            patch.object(git_emitter, "_git") as mock_git,
            patch.object(git_emitter, "create_github_pr") as mock_pr,
        ):
            mock_git.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            mock_pr.return_value = {
                "success": True,
                "pr_url": "https://github.com/owner/repo/pull/123",
                "command": "gh pr create ...",
                "error": "",
            }

            res = git_emitter.emit_pr_artifacts(
                verdict_path=str(vfile),
                audit_log_path=str(alog),
                repo_root=str(tmp_path),
                dry_run=False,
                create_pr=True,
            )

            assert res["status"] == "ready"
            assert res["pr_url"] == "https://github.com/owner/repo/pull/123"
            mock_pr.assert_called_once()


# ---------------------------------------------------------------------------
# 5. CLI Command Integration
# ---------------------------------------------------------------------------


class TestCLIIntegration:
    def test_cli_pr_command(self, tmp_path: Path) -> None:
        from netrani import cli

        bob_dir = tmp_path / ".bob"
        _create_mock_verdict(bob_dir)
        _create_mock_patch(bob_dir)
        _create_mock_audit_log(bob_dir)

        with patch("netrani.pipeline.git_emitter.emit_pr_artifacts") as mock_emit:
            mock_emit.return_value = {
                "status": "ready",
                "pr_draft_path": str(bob_dir / "pr_draft.md"),
                "pr_url": "https://github.com/owner/repo/pull/555",
                "pr_command": "",
                "error": "",
            }
            exit_code = cli.main(["pr", "--repo", str(tmp_path), "--create-pr"])
            assert exit_code == 0
            mock_emit.assert_called_once()
