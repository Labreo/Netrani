"""
tests/test_history_miner.py
Unit tests for Netrani's History Miner subagent and Dual-Anchor Triage logic.

Covers:
  1. Stop-words and generic keyword filtering (pickaxe guards)
  2. Dual-anchor area and symptom extraction
  3. Multi-token co-occurrence scoring (Dual-Anchor relevance)
  4. Deterministic commit candidate sorting
  5. Off-main branch duplicate detection
  6. Orchestrator synthesis gate enforcement
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from netrani.subagents import history_miner
from netrani.subagents.history_miner import (
    STOP_WORDS,
    HistoryFindings,
    _evaluate_dual_anchor_relevance,
    _extract_area_anchors,
    _extract_symptom_anchors,
    _is_generic_word,
    _search_log_s,
)
from netrani.subagents.static_validator import StaticFindings
from netrani.triage import orchestrator


# ---------------------------------------------------------------------------
# 1. Stop words & generic keyword filtering
# ---------------------------------------------------------------------------

class TestStopWordFiltering:
    """Ensure generic programming words never trigger pickaxe queries."""

    @pytest.mark.parametrize(
        "word",
        [
            "version",
            "behavior",
            "behaviour",
            "requests",
            "request",
            "description",
            "steps",
            "expected",
            "actual",
            "reproduce",
        ],
    )
    def test_generic_words_identified(self, word: str) -> None:
        assert word in STOP_WORDS or _is_generic_word(word)
        assert _is_generic_word(word) is True

    @pytest.mark.parametrize(
        "kw",
        [
            "version",
            "behavior",
            "requests",
            "description",
            "steps",
            "expected",
            "`version`",
            "-steps",
        ],
    )
    def test_pickaxe_ignores_generic_words(self, kw: str, tmp_path: Path) -> None:
        with patch.object(history_miner, "_run_git") as mock_git:
            results = _search_log_s(kw, tmp_path)
            assert results == []
            mock_git.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Dual-Anchor Extraction
# ---------------------------------------------------------------------------

class TestDualAnchorExtraction:
    """Ensure area and symptom anchors are accurately extracted."""

    def test_area_anchors_extracted(self) -> None:
        title = "Compile-time instrumentation breaks when -toolexec chain contains multiple wrappers"
        body = "ast rewriter panics in pkg/inst-api-semconv/instrumenter/instrumenter.go"
        suspects = ["instrumenter.go"]
        areas = _extract_area_anchors(title, body, suspects)
        assert "toolexec" in areas
        assert "instrumenter" in areas or "ast" in areas

    def test_symptom_anchors_extracted(self) -> None:
        title = "go generate panics on interface method with variadic parameter in instrumented package"
        body = "panic: runtime error: index out of range [1] with length 1"
        symptoms = _extract_symptom_anchors(title, body)
        assert "panic" in symptoms or "variadic" in symptoms
        assert any("variadic" in s or "range" in s or "panic" in s for s in symptoms)


# ---------------------------------------------------------------------------
# 3. Multi-Token Co-Occurrence Scoring (Dual-Anchor Relevance)
# ---------------------------------------------------------------------------

class TestDualAnchorScoring:
    """Test scoring rule for dual-anchor model."""

    def test_both_area_and_symptom_match_high_confidence(self) -> None:
        area_anchors = ["toolexec", "cgo"]
        symptom_anchors = ["vet failure", "c source files not allowed"]
        commit_text = "fix(tool): prevent vet failures for cgo (#1193)\nFilter c source files"

        area_m, symp_m, conf = _evaluate_dual_anchor_relevance(
            commit_text, area_anchors, symptom_anchors
        )
        assert area_m is True
        assert symp_m is True
        assert 0.85 <= conf <= 0.95

    def test_area_only_match_low_confidence(self) -> None:
        area_anchors = ["toolexec"]
        symptom_anchors = ["multiple wrappers", "chain panic"]
        # Commit matches area (toolexec) but NOT the specific symptom
        commit_text = "fix: toolexec goflags dropin (#674)\nSupport GOFLAGS in toolexec"

        area_m, symp_m, conf = _evaluate_dual_anchor_relevance(
            commit_text, area_anchors, symptom_anchors
        )
        assert area_m is True
        assert symp_m is False
        assert 0.50 <= conf <= 0.55

    def test_no_area_match_inconclusive(self) -> None:
        area_anchors = ["chi"]
        symptom_anchors = ["wrong span name"]
        commit_text = "fix(grpc): resolve panic in client interceptor"

        area_m, symp_m, conf = _evaluate_dual_anchor_relevance(
            commit_text, area_anchors, symptom_anchors
        )
        assert area_m is False
        assert conf == 0.0


# ---------------------------------------------------------------------------
# 4. Deterministic Commit Candidate Sorting
# ---------------------------------------------------------------------------

class TestDeterministicCommitSorting:
    """Ensure candidate commits are sorted deterministically."""

    def test_candidates_sorted_by_conf_timestamp_sha(self) -> None:
        candidates = [
            {"sha": "aaaa111", "commit_timestamp": 1000, "confidence": 0.52},
            {"sha": "cccc333", "commit_timestamp": 3000, "confidence": 0.95},
            {"sha": "bbbb222", "commit_timestamp": 2000, "confidence": 0.95},
            {"sha": "dddd444", "commit_timestamp": 4000, "confidence": 0.75},
        ]

        sorted_cands = sorted(
            candidates,
            key=lambda c: (c.get("confidence", 0.0), c.get("commit_timestamp", 0), c.get("sha", "")),
            reverse=True,
        )

        assert [c["sha"] for c in sorted_cands] == ["cccc333", "bbbb222", "dddd444", "aaaa111"]


# ---------------------------------------------------------------------------
# 5. Off-Main Branch Duplicate Detection
# ---------------------------------------------------------------------------

class TestOffMainDuplicateDetection:
    """Ensure unmerged commits on non-default branches yield DUPLICATE with conf 0.75."""

    def test_unmerged_commit_detected_as_duplicate(self, tmp_path: Path) -> None:
        area_anchors = ["cache", "toolexec"]
        symptom_anchors = ["cache corruption", "race condition"]
        keywords = ["otel-instr-cache"]

        with (
            patch.object(history_miner, "_run_git") as mock_git,
            patch.object(history_miner, "_resolve_full_sha", return_value="712abcdef4567890123456789012345678901234"),
            patch.object(history_miner, "_get_commit_timestamp", return_value=1700000000),
            patch.object(history_miner, "_get_branch_containing_commit", return_value="pr-712"),
            patch.object(
                history_miner,
                "_get_commit_subject_and_body",
                return_value=("fix: isolate cache per process to prevent race condition corruption", ""),
            ),
        ):
            mock_git.return_value = "712abcd fix: isolate cache per process to prevent race condition corruption"

            candidates = history_miner._search_unmerged_duplicates(
                area_anchors=area_anchors,
                symptom_anchors=symptom_anchors,
                keywords=keywords,
                issue_url="https://github.com/org/repo/issues/718",
                default_branch="main",
                cwd=tmp_path,
            )

            assert len(candidates) >= 1
            top = candidates[0]
            assert top["verdict"] == "DUPLICATE"
            assert top["confidence"] == 0.75
            assert "commit 712abcdef456" in top["citation"]
            assert "pr-712" in top["citation"]


# ---------------------------------------------------------------------------
# 6. Orchestrator Synthesis Gate Enforcement
# ---------------------------------------------------------------------------

class TestOrchestratorSynthesis:
    """Test synthesis rules for dual-anchor and confidence thresholds."""

    def test_strong_obsolete_short_circuits(self) -> None:
        history = HistoryFindings(
            verdict="OBSOLETE",
            citation="abc123456789",
            rationale="Fix commit on main",
            confidence=0.90,
        )
        static = StaticFindings(verdict="VALID", confidence=0.60)
        status, cite, rat, conf = orchestrator._synthesise(history, static)
        assert status == "OBSOLETE"
        assert conf == 0.90

    def test_weak_obsolete_falls_through_to_static_or_valid(self) -> None:
        # Weak OBSOLETE (0.52) from area-only match must NOT short-circuit
        history = HistoryFindings(
            verdict="OBSOLETE",
            citation="abc123456789",
            rationale="Area-only match",
            confidence=0.52,
        )
        static = StaticFindings(verdict=None, confidence=0.0)
        status, cite, rat, conf = orchestrator._synthesise(history, static)
        assert status == "VALID"
        assert conf == 0.55

    def test_duplicate_takes_precedence_over_weak_static(self) -> None:
        history = HistoryFindings(
            verdict="DUPLICATE",
            citation="commit 712abcd",
            rationale="Unmerged PR tracks this",
            confidence=0.75,
        )
        static = StaticFindings(verdict=None, confidence=0.0)
        status, cite, rat, conf = orchestrator._synthesise(history, static)
        assert status == "DUPLICATE"
        assert conf == 0.75
