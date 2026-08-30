"""
tests/test_execution_gate.py
Unit tests for the Execution Gate and Submittable PR Generator pipeline.

Covers:
  1. Verdict gate enforcement (SurgicalFixer hard-exits on non-VALID status)
  2. Patch abort on non-VALID status — zero file modifications
  3. Audit log schema validation — CommandResult entries are well-formed
  4. PR draft section completeness — all five required sections present
  5. Orchestrator stage sequencing — stages fire in correct order,
     non-VALID skips Stages 5–8

All tests are self-contained and do not modify the real repository.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------


def _write_verdict(directory: Path, status: str, **extra: Any) -> Path:
    """Write a minimal schema-valid verdict.json to *directory*."""
    vfile = directory / "verdict.json"
    vfile.write_text(
        json.dumps({
            "status": status,
            "citation": "netrani/subagents/static_validator.py:45-60",
            "rationale": "The failure path is statically reachable with no guard.",
            "confidence": 0.88,
            "timestamp": "2025-07-15T10:30:00Z",
            "target_repo": "owner/repo",
            "issue_reference": "https://github.com/owner/repo/issues/42",
            **extra,
        }),
        encoding="utf-8",
    )
    return vfile


def _write_profile(directory: Path) -> Path:
    """Write a minimal runtime profile JSON to *directory*."""
    pfile = directory / "repo_profile.json"
    pfile.write_text(
        json.dumps({
            "test_commands": ["pytest tests/"],
            "lint_commands": ["ruff check ."],
            "contribution_guidelines": "- [ ] Tests added\n- [ ] Docs updated\n",
            "detected_languages": ["Python"],
            "issue_template_schema": {"fields": []},
        }),
        encoding="utf-8",
    )
    return pfile


# ---------------------------------------------------------------------------
# 1. Verdict gate enforcement
# ---------------------------------------------------------------------------


class TestVerdictGateEnforcement:
    """SurgicalFixer must hard-exit (SystemExit(2)) on any non-VALID status."""

    @pytest.mark.parametrize(
        "status", ["DUPLICATE", "OBSOLETE", "FALSE_POSITIVE", "INCONCLUSIVE"]
    )
    def test_non_valid_raises_system_exit_2(
        self, status: str, tmp_path: Path
    ) -> None:
        """
        surgical_fixer._load_verdict must call sys.exit(2) for non-VALID verdicts.
        """
        from netrani.subagents.surgical_fixer import _load_verdict

        bob_dir = tmp_path / ".bob"
        bob_dir.mkdir(parents=True, exist_ok=True)
        vfile = _write_verdict(bob_dir, status)

        with pytest.raises(SystemExit) as exc_info:
            _load_verdict(str(vfile))

        assert exc_info.value.code == 2, (
            f"Expected exit code 2 for status={status}, "
            f"got {exc_info.value.code}"
        )

    def test_valid_status_returns_verdict_dict(self, tmp_path: Path) -> None:
        """_load_verdict must return the parsed verdict dict for VALID status."""
        from netrani.subagents.surgical_fixer import _load_verdict

        bob_dir = tmp_path / ".bob"
        bob_dir.mkdir(parents=True, exist_ok=True)
        vfile = _write_verdict(bob_dir, "VALID")

        result = _load_verdict(str(vfile))
        assert result["status"] == "VALID"
        assert result["confidence"] == 0.88

    def test_missing_verdict_file_raises_system_exit_2(self, tmp_path: Path) -> None:
        """_load_verdict must exit(2) when the verdict file is absent."""
        from netrani.subagents.surgical_fixer import _load_verdict

        with pytest.raises(SystemExit) as exc_info:
            _load_verdict(str(tmp_path / ".bob" / "verdict.json"))

        assert exc_info.value.code == 2

    def test_malformed_json_raises_system_exit_2(self, tmp_path: Path) -> None:
        """_load_verdict must exit(2) when the file contains invalid JSON."""
        from netrani.subagents.surgical_fixer import _load_verdict

        bob_dir = tmp_path / ".bob"
        bob_dir.mkdir()
        bad = bob_dir / "verdict.json"
        bad.write_text("{not valid json", encoding="utf-8")

        with pytest.raises(SystemExit) as exc_info:
            _load_verdict(str(bad))

        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# 2. Patch abort on non-VALID status — zero file modifications
# ---------------------------------------------------------------------------


class TestPatchAbortOnNonValid:
    """run_surgical_fix must never modify files when the verdict is non-VALID."""

    @pytest.mark.parametrize("status", ["DUPLICATE", "OBSOLETE", "FALSE_POSITIVE"])
    def test_no_files_modified_on_non_valid(
        self, status: str, tmp_path: Path
    ) -> None:
        """
        When the verdict is not VALID, run_surgical_fix must exit(2) before
        performing any filesystem writes.  We verify that no files under
        tmp_path change after the call attempt.
        """
        from netrani.subagents import surgical_fixer

        bob_dir = tmp_path / ".bob"
        bob_dir.mkdir(parents=True, exist_ok=True)
        vfile = _write_verdict(bob_dir, status)
        pfile = _write_profile(bob_dir)

        # Record the set of files before the call
        before = set(tmp_path.rglob("*"))

        with pytest.raises(SystemExit) as exc_info:
            surgical_fixer.run_surgical_fix(
                verdict_path=str(vfile),
                profile_path=str(pfile),
                repo_root=str(tmp_path),
            )

        assert exc_info.value.code == 2

        # No new files must have appeared
        after = set(tmp_path.rglob("*"))
        new_files = after - before
        # Allow only .bob/ directory creation, not actual source files
        source_new = [f for f in new_files if not str(f).startswith(str(bob_dir))]
        assert not source_new, (
            f"Unexpected files created during non-VALID run: {source_new}"
        )

    def test_aborted_result_structure(self, tmp_path: Path) -> None:
        """
        When a patch cannot be generated (citation-less verdict), run_surgical_fix
        should return status='aborted' without raising.
        """
        from netrani.subagents import surgical_fixer

        bob_dir = tmp_path / ".bob"
        bob_dir.mkdir(parents=True, exist_ok=True)
        # Valid verdict but no useful citation → patch generation fails
        vfile = _write_verdict(bob_dir, "VALID", citation="no-file-reference")
        pfile = _write_profile(bob_dir)

        # We need a real git repo for branch operations to not raise
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=False)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path), capture_output=True, check=False,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), capture_output=True, check=False,
        )

        result = surgical_fixer.run_surgical_fix(
            verdict_path=str(vfile),
            profile_path=str(pfile),
            repo_root=str(tmp_path),
        )

        assert result["status"] == "aborted"
        assert "branch" in result
        assert isinstance(result["files_modified"], list)


# ---------------------------------------------------------------------------
# 3. Audit log schema validation
# ---------------------------------------------------------------------------


class TestAuditLogSchemaValidation:
    """CommandResult entries written to audit.log must conform to the schema."""

    def test_command_result_to_dict_has_all_required_fields(self) -> None:
        """CommandResult.to_dict() must contain all seven mandatory fields."""
        from netrani.subagents.test_runner import CommandResult

        cr = CommandResult(
            command="pytest tests/",
            timestamp_utc="2025-07-15T10:30:00+00:00",
            exit_code=0,
            stdout_summary="1 passed",
            stderr_summary="",
            duration_seconds=1.234,
            status="passed",
        )
        d = cr.to_dict()

        required_fields = {
            "command", "timestamp_utc", "exit_code",
            "stdout_summary", "stderr_summary", "duration_seconds", "status",
        }
        assert required_fields == set(d.keys()), (
            f"Missing fields: {required_fields - set(d.keys())}"
        )

    def test_status_is_failed_on_nonzero_exit(self, tmp_path: Path) -> None:
        """
        _run_command must record status='failed' and the correct exit_code for
        a command that returns a non-zero exit code.
        """
        from netrani.subagents.test_runner import _run_command

        cmd = f'"{sys.executable}" -c "import sys; sys.exit(1)"'
        result = _run_command(cmd, tmp_path, timeout=10)

        assert result.exit_code == 1
        assert result.status == "failed"

    def test_status_is_passed_on_zero_exit(self, tmp_path: Path) -> None:
        """_run_command must record status='passed' for a successful command."""
        from netrani.subagents.test_runner import _run_command

        result = _run_command(f'"{sys.executable}" -c "pass"', tmp_path, timeout=10)

        assert result.exit_code == 0
        assert result.status == "passed"

    def test_timeout_records_exit_code_minus_one(self, tmp_path: Path) -> None:
        """A timed-out command must be recorded as exit_code=-1, status='failed'."""
        from netrani.subagents.test_runner import _run_command

        # Sleep for 60 seconds but timeout after 1 second
        result = _run_command(
            f'"{sys.executable}" -c "import time; time.sleep(60)"',
            tmp_path,
            timeout=1,
        )

        assert result.exit_code == -1
        assert result.status == "failed"

    def test_audit_log_is_append_only(self, tmp_path: Path) -> None:
        """
        run_tests must append to audit.log rather than truncating it.
        A pre-existing entry must survive across multiple run_tests calls.
        """
        from netrani import config
        from netrani.subagents import test_runner

        bob_dir = tmp_path / ".bob"
        bob_dir.mkdir(parents=True, exist_ok=True)
        alog = config.audit_log_path(tmp_path)
        # Pre-seed with a sentinel entry
        sentinel = json.dumps({"event": "sentinel", "value": "must-survive"}) + "\n"
        alog.write_text(sentinel, encoding="utf-8")

        profile_path = _write_profile(bob_dir)

        with (
            patch("netrani.subagents.test_runner._run_command") as mock_run,
            patch("netrani.subagents.test_runner._invoke_record_verdict_hook"),
        ):
            from netrani.subagents.test_runner import CommandResult
            mock_run.return_value = CommandResult(
                command="pytest tests/",
                timestamp_utc="2025-07-15T10:30:00+00:00",
                exit_code=0,
                stdout_summary="ok",
                stderr_summary="",
                duration_seconds=0.1,
                status="passed",
            )
            test_runner.run_tests(
                profile_path=str(profile_path),
                branch="main",
                repo_root=str(tmp_path),
            )

        content = alog.read_text(encoding="utf-8")
        assert "must-survive" in content, (
            "Audit log sentinel was overwritten — log is not append-only."
        )

    def test_no_commands_returns_skipped(self, tmp_path: Path) -> None:
        """
        run_tests must return overall_status='skipped' (not fail) when no
        test commands are found in the profile.
        """
        from netrani.subagents import test_runner

        bob_dir = tmp_path / ".bob"
        bob_dir.mkdir(parents=True, exist_ok=True)
        empty_profile = bob_dir / "empty_profile.json"
        empty_profile.write_text(
            json.dumps({"test_commands": [], "lint_commands": []}),
            encoding="utf-8",
        )

        result = test_runner.run_tests(
            profile_path=str(empty_profile),
            branch="main",
            repo_root=str(tmp_path),
        )

        assert result["overall_status"] == "skipped"
        assert result["commands_run"] == 0


# ---------------------------------------------------------------------------
# 4. PR draft section completeness
# ---------------------------------------------------------------------------


class TestPRDraftSectionCompleteness:
    """The generated PR draft must contain all five mandatory sections."""

    _REQUIRED_SECTIONS = [
        "## Issue Reference",
        "## Root Cause Summary",
        "## Change Summary",
        "## Verification Summary",
        "## Compliance Checklist",
    ]

    def _make_audit_log(self, tmp_path: Path) -> Path:
        """Write a single passed command entry to audit.log."""
        alog = tmp_path / ".bob" / "audit.log"
        alog.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "command": "pytest tests/",
            "timestamp_utc": "2025-07-15T10:30:00+00:00",
            "exit_code": 0,
            "stdout_summary": "1 passed",
            "stderr_summary": "",
            "duration_seconds": 1.0,
            "status": "passed",
        }
        alog.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        return alog

    def test_all_five_sections_present(self, tmp_path: Path) -> None:
        """emit_pr_artifacts must produce a PR draft with all five sections."""
        from netrani.pipeline import git_emitter

        bob_dir = tmp_path / ".bob"
        bob_dir.mkdir(parents=True, exist_ok=True)
        vfile = _write_verdict(bob_dir, "VALID")
        alog = self._make_audit_log(tmp_path)

        # Write a dummy patch.diff so the emitter has something to summarise
        patch_file = bob_dir / "patch.diff"
        patch_file.write_text(
            textwrap.dedent(
                """\
                --- a/netrani/config.py
                +++ b/netrani/config.py
                @@ -1,3 +1,4 @@
                +# NETRANI-FIX
                 line1
                 line2
                 line3
                """
            ),
            encoding="utf-8",
        )

        result = git_emitter.emit_pr_artifacts(
            verdict_path=str(vfile),
            audit_log_path=str(alog),
            repo_root=str(tmp_path),
            dry_run=True,
            dry_run_dir=str(bob_dir / "dry_run"),
        )

        assert result["status"] == "ready", f"Emitter returned: {result}"
        pr_path = Path(result["pr_draft_path"])
        assert pr_path.exists(), "PR draft file was not created."

        content = pr_path.read_text(encoding="utf-8")
        for section in self._REQUIRED_SECTIONS:
            assert section in content, (
                f"Required section '{section}' missing from PR draft."
            )

    def test_contributing_checklist_uses_default_when_no_file(
        self, tmp_path: Path
    ) -> None:
        """
        _parse_contributing_checklist must return the default four-item list
        when CONTRIBUTING.md does not exist.
        """
        from netrani.pipeline.git_emitter import _parse_contributing_checklist

        items = _parse_contributing_checklist(tmp_path)
        assert len(items) == 4
        assert any("test" in i.lower() for i in items)
        assert any("doc" in i.lower() for i in items)

    def test_contributing_checklist_extracts_from_file(self, tmp_path: Path) -> None:
        """
        _parse_contributing_checklist must extract checkbox items from
        CONTRIBUTING.md.
        """
        from netrani.pipeline.git_emitter import _parse_contributing_checklist

        contrib = tmp_path / "CONTRIBUTING.md"
        contrib.write_text(
            "# Contributing\n\n"
            "- [ ] Write unit tests\n"
            "- [ ] Update changelog\n"
            "- [x] Read the docs\n",  # checked items should be ignored
            encoding="utf-8",
        )

        items = _parse_contributing_checklist(tmp_path)
        assert "Write unit tests" in items
        assert "Update changelog" in items
        # Checked items (- [x]) must not be included
        assert "Read the docs" not in items

    def test_verification_table_rendered_for_audit_entries(
        self, tmp_path: Path
    ) -> None:
        """
        The Verification Summary section must contain a Markdown table when
        audit entries are present.
        """
        from netrani.pipeline.git_emitter import _build_pr_draft

        verdict = {
            "status": "VALID",
            "citation": "netrani/config.py:10",
            "rationale": "The path is reachable.",
            "confidence": 0.9,
            "issue_reference": "https://github.com/owner/repo/issues/1",
        }
        audit_entries = [
            {
                "command": "pytest tests/",
                "exit_code": 0,
                "duration_seconds": 1.5,
                "status": "passed",
            }
        ]

        draft = _build_pr_draft(
            verdict=verdict,
            patch_text="",
            audit_entries=audit_entries,
            repo_root=tmp_path,
            files_modified=[],
            branch="netrani/fix-issue-1",
            commit_sha="abc123",
        )

        assert "| Command |" in draft, "Verification table header missing."
        assert "pytest tests/" in draft
        assert "✅" in draft or "passed" in draft


# ---------------------------------------------------------------------------
# 5. Orchestrator stage sequencing
# ---------------------------------------------------------------------------


class TestOrchestratorStageSequencing:
    """
    Pipeline stages must fire in the correct order.
    A non-VALID verdict must skip Stages 5–8.
    A stage failure must halt the pipeline immediately.
    """

    def _mock_issue(self) -> dict[str, Any]:
        return {
            "title": "Test issue",
            "url": "https://github.com/owner/repo/issues/42",
            "author": "tester",
            "labels": [],
            "source": "local",
            "suspect_symbols": [],
            "reproduction_trace": [],
            "reported_version": None,
            "environment": {},
        }

    def test_non_valid_verdict_skips_stages_5_to_8(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """
        When triage returns DUPLICATE, the pipeline must exit 0 without
        calling SurgicalFixer, TestRunner, or GitEmitter.
        """
        from netrani.pipeline import orchestrator

        issue = self._mock_issue()

        duplicate_verdict = {
            "status": "DUPLICATE",
            "citation": "sha:abc123",
            "rationale": "Already tracked in open PR #41.",
            "confidence": 0.75,
            "timestamp": "2025-07-15T10:30:00Z",
            "target_repo": "owner/repo",
            "issue_reference": issue["url"],
        }

        with (
            patch.object(orchestrator, "_stage_issue_ingestion") as mock_ingest,
            patch.object(orchestrator, "_stage_doc_discovery") as mock_docs,
            patch.object(orchestrator, "_stage_triage") as mock_triage,
            patch.object(orchestrator, "_stage_surgical_fix") as mock_fix,
            patch.object(orchestrator, "_stage_test_execution") as mock_tests,
            patch.object(orchestrator, "_stage_git_emit") as mock_emit,
            patch.object(orchestrator, "_write_run_summary") as mock_summary,
        ):
            mock_ingest.return_value = orchestrator.StageResult(
                "issue_ingestion", True, {"issue": issue}
            )
            mock_docs.return_value = orchestrator.StageResult(
                "doc_discovery", True,
                {"profile": {}, "profile_path": str(tmp_path / "profile.json")},
            )
            mock_triage.return_value = orchestrator.StageResult(
                "triage", True,
                {"verdict": duplicate_verdict,
                 "verdict_path": str(tmp_path / "verdict.json")},
            )
            mock_summary.return_value = tmp_path / "run_summary.json"

            exit_code = orchestrator.run_pipeline(
                issue_ref="https://github.com/owner/repo/issues/42",
                repo_root=str(tmp_path),
            )

        assert exit_code == 0
        mock_fix.assert_not_called()
        mock_tests.assert_not_called()
        mock_emit.assert_not_called()

        captured = capsys.readouterr()
        assert "DUPLICATE" in captured.out

    def test_failed_stage_halts_pipeline(
        self, tmp_path: Path
    ) -> None:
        """
        If Stage 1 (issue ingestion) fails, the pipeline must return exit
        code 1 and not proceed to Stage 2.
        """
        from netrani.pipeline import orchestrator

        with (
            patch.object(orchestrator, "_stage_issue_ingestion") as mock_ingest,
            patch.object(orchestrator, "_stage_doc_discovery") as mock_docs,
            patch.object(orchestrator, "_write_run_summary") as mock_summary,
        ):
            mock_ingest.return_value = orchestrator.StageResult(
                "issue_ingestion", False, error="Network error"
            )
            mock_summary.return_value = tmp_path / "run_summary.json"

            exit_code = orchestrator.run_pipeline(
                issue_ref="https://github.com/owner/repo/issues/99",
                repo_root=str(tmp_path),
            )

        assert exit_code == 1
        mock_docs.assert_not_called()

    def test_valid_verdict_calls_all_stages(
        self, tmp_path: Path
    ) -> None:
        """
        For a VALID verdict, all eight stages must be invoked in sequence.
        """
        from netrani.pipeline import orchestrator

        issue = self._mock_issue()
        valid_verdict = {
            "status": "VALID",
            "citation": "netrani/config.py:10",
            "rationale": "The failure path is reachable with no guard.",
            "confidence": 0.88,
            "timestamp": "2025-07-15T10:30:00Z",
            "target_repo": "owner/repo",
            "issue_reference": issue["url"],
        }

        call_order: list[str] = []

        def _make_stage(name: str, data: dict[str, Any]) -> MagicMock:
            def side_effect(*a: Any, **kw: Any) -> orchestrator.StageResult:
                call_order.append(name)
                return orchestrator.StageResult(name, True, data)
            m = MagicMock(side_effect=side_effect)
            return m

        with (
            patch.object(
                orchestrator, "_stage_issue_ingestion",
                _make_stage("issue_ingestion", {"issue": issue}),
            ),
            patch.object(
                orchestrator, "_stage_doc_discovery",
                _make_stage("doc_discovery", {
                    "profile": {},
                    "profile_path": str(tmp_path / "profile.json"),
                }),
            ),
            patch.object(
                orchestrator, "_stage_triage",
                _make_stage("triage", {
                    "verdict": valid_verdict,
                    "verdict_path": str(tmp_path / "verdict.json"),
                }),
            ),
            patch.object(
                orchestrator, "_stage_surgical_fix",
                _make_stage("surgical_fix", {
                    "branch": "netrani/fix-issue-42",
                    "patch_path": "",
                    "files_modified": [],
                    "status": "applied",
                }),
            ),
            patch.object(
                orchestrator, "_stage_test_execution",
                _make_stage("test_execution", {
                    "overall_status": "passed",
                    "commands_run": 1,
                    "failures": [],
                    "audit_log_path": str(tmp_path / ".bob" / "audit.log"),
                }),
            ),
            patch.object(
                orchestrator, "_stage_git_emit",
                _make_stage("git_emit", {
                    "branch": "netrani/fix-issue-42",
                    "commit_sha": "abc123def456",
                    "pr_draft_path": str(tmp_path / ".bob" / "pr_draft.md"),
                    "status": "ready",
                }),
            ),
            patch.object(
                orchestrator, "_write_run_summary",
                return_value=tmp_path / "run_summary.json",
            ),
        ):
            exit_code = orchestrator.run_pipeline(
                issue_ref="https://github.com/owner/repo/issues/42",
                repo_root=str(tmp_path),
            )

        assert exit_code == 0
        assert call_order == [
            "issue_ingestion",
            "doc_discovery",
            "triage",
            "surgical_fix",
            "test_execution",
            "git_emit",
        ], f"Unexpected stage order: {call_order}"

    def test_stage_result_serialisation(self) -> None:
        """StageResult.to_dict() must serialise all fields correctly."""
        from netrani.pipeline.orchestrator import StageResult

        sr = StageResult(
            stage_name="surgical_fix",
            success=True,
            data={"branch": "netrani/fix-issue-42", "status": "applied"},
            error="",
        )
        d = sr.to_dict()

        assert d["stage"] == "surgical_fix"
        assert d["success"] is True
        assert d["data"]["branch"] == "netrani/fix-issue-42"
        assert d["error"] == ""
