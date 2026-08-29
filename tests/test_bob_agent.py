"""
tests/test_bob_agent.py
Unit and integration tests for IBM Bob 2.0 Agentic Execution Harness (netrani.bob).
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from netrani.bob.agent import (
    BobAgent,
    BobToolCall,
    BobToolResult,
    CustomModePersona,
    load_custom_modes,
    run_bob_escalation,
)


class TestBobCustomModes:
    """Test loading and permission boundaries of Bob custom modes."""

    def test_load_custom_modes_from_repo(self, tmp_path: Path) -> None:
        modes = load_custom_modes(tmp_path)
        assert "history-miner" in modes
        assert "static-validator" in modes
        assert "surgical-fixer" in modes
        assert "test-runner" in modes

        assert modes["history-miner"].can_read() is True
        assert modes["history-miner"].can_execute() is True
        assert modes["history-miner"].can_edit() is False

        assert modes["static-validator"].can_read() is True
        assert modes["static-validator"].can_execute() is False
        assert modes["static-validator"].can_edit() is False

        assert modes["surgical-fixer"].can_read() is True
        assert modes["surgical-fixer"].can_edit() is True
        assert modes["surgical-fixer"].can_execute() is True

    def test_permission_enforcement_on_read_only_mode(self, tmp_path: Path) -> None:
        agent = BobAgent(tmp_path, mode_slug="static-validator")
        test_file = tmp_path / "hello.go"
        test_file.write_text("package main\n\nfunc main() {}\n", encoding="utf-8")

        # Read tool works
        res = agent.read_file("hello.go")
        assert res.success is True
        assert "package main" in res.output

        # Execute tool blocked
        with pytest.raises(PermissionError, match="does not have 'execute' permissions"):
            agent.execute_command("ls -la")

        # Edit tool blocked
        with pytest.raises(PermissionError, match="does not have 'edit' permissions"):
            agent.write_file("hello.go", "package modified")


class TestBobAgentToolExecution:
    """Test agentic tool execution within BobAgent."""

    def test_read_and_grep_tools(self, tmp_path: Path) -> None:
        agent = BobAgent(tmp_path, mode_slug="history-miner")
        sub_dir = tmp_path / "pkg"
        sub_dir.mkdir()
        (sub_dir / "sample.go").write_text("package sample\n\n// SentinelTracerProvider\n", encoding="utf-8")

        list_res = agent.list_directory("pkg")
        assert list_res.success is True
        assert "sample.go" in list_res.output

        read_res = agent.read_file("pkg/sample.go")
        assert read_res.success is True
        assert "SentinelTracerProvider" in read_res.output

    def test_execute_command_triggers_sensor_hook(self, tmp_path: Path) -> None:
        # Set up .bob/hooks/record-verdict.sh in tmp_path
        hooks_dir = tmp_path / ".bob" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook_script = hooks_dir / "record-verdict.sh"
        hook_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook_script.chmod(0o755)

        agent = BobAgent(tmp_path, mode_slug="history-miner")
        res = agent.execute_command("echo 'Hello Bob 2.0'")
        assert res.success is True
        assert "Hello Bob 2.0" in res.output
        assert "execute_command" in agent.tools_invoked

    def test_write_file_gated_by_pretool_hook(self, tmp_path: Path) -> None:
        bob_dir = tmp_path / ".bob"
        bob_dir.mkdir()
        verdict_file = bob_dir / "verdict.json"
        
        # 1. Non-VALID verdict blocks write_file
        verdict_file.write_text(json.dumps({"status": "OBSOLETE"}), encoding="utf-8")
        agent = BobAgent(tmp_path, mode_slug="surgical-fixer")
        
        # Using real Netrani gate-fix.sh
        with pytest.raises(PermissionError, match="GATE-FIX BLOCKED"):
            agent.write_file("fix.go", "package fix")

        # 2. VALID verdict allows write_file
        verdict_file.write_text(json.dumps({"status": "VALID"}), encoding="utf-8")
        res = agent.write_file("fix.go", "package fix")
        assert res.success is True
        assert (tmp_path / "fix.go").exists()

    def test_session_summary_generation(self, tmp_path: Path) -> None:
        agent = BobAgent(tmp_path, mode_slug="static-validator")
        (tmp_path / "test.txt").write_text("content", encoding="utf-8")
        agent.read_file("test.txt")

        summary = agent.generate_session_summary("Test static validator run")
        assert summary.mode == "Static Validator"
        assert summary.workspace == tmp_path.name
        assert summary.bobcoins_cost > 0.0
        assert "read_file" in summary.tools_invoked

        # Check task persistence in .bob/tasks/
        tasks_dir = tmp_path / ".bob" / "tasks"
        assert tasks_dir.exists()
        task_files = list(tasks_dir.glob("*.json"))
        assert len(task_files) == 1
        data = json.loads(task_files[0].read_text(encoding="utf-8"))
        assert data["mode"] == "Static Validator"


class TestBobEscalation:
    """Test run_bob_escalation engine."""

    def test_run_bob_escalation_on_repo(self, tmp_path: Path) -> None:
        (tmp_path / "main.go").write_text(
            "package main\n\nfunc Handle() {\n    defer span.End()\n}\n",
            encoding="utf-8",
        )
        res = run_bob_escalation(
            repo_path=tmp_path,
            title="Panic during handler execution",
            body="Handler panics when span is active",
            suspect_symbols=["Handle", "span.End"],
            reproduction_trace=["Call Handle()"],
            issue_ref="issue-99",
        )
        assert res["bob_escalated"] is True
        assert res["status"] in ("FALSE_POSITIVE", "VALID", "OBSOLETE")
        assert "IBM Bob 2.0" in res["rationale"]
        assert res["confidence"] >= 0.70
