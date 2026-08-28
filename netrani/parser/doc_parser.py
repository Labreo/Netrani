"""
netrani/parser/doc_parser.py
Runtime document understanding engine.

Inspects a repository root and returns a structured RepoProfile containing
test commands, lint commands, contribution guidelines, detected languages,
and the parsed issue template schema.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, TypedDict


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class RepoProfile(TypedDict):
    test_commands: list[str]
    lint_commands: list[str]
    contribution_guidelines: str
    detected_languages: list[str]
    issue_template_schema: dict[str, Any]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    """Return file text or empty string if unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _glob(root: Path, pattern: str) -> list[Path]:
    """Glob relative to root; swallow errors."""
    try:
        return sorted(root.glob(pattern))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

_LANGUAGE_MARKERS: list[tuple[str, str]] = [
    ("go.mod",        "Go"),
    ("go.sum",        "Go"),
    ("Cargo.toml",    "Rust"),
    ("package.json",  "JavaScript/TypeScript"),
    ("pyproject.toml","Python"),
    ("setup.cfg",     "Python"),
    ("setup.py",      "Python"),
    ("tox.ini",       "Python"),
    ("pom.xml",       "Java"),
    ("build.gradle",  "Java/Kotlin"),
    ("Gemfile",       "Ruby"),
    ("Makefile",      "Make"),
]


def _detect_languages(root: Path) -> list[str]:
    langs: list[str] = []
    seen: set[str] = set()
    for filename, lang in _LANGUAGE_MARKERS:
        if (root / filename).exists() and lang not in seen:
            langs.append(lang)
            seen.add(lang)
    # Fallback: scan for common source extensions
    ext_map = {
        ".py": "Python", ".go": "Go", ".rs": "Rust",
        ".ts": "JavaScript/TypeScript", ".js": "JavaScript/TypeScript",
        ".java": "Java", ".rb": "Ruby", ".cs": "C#", ".cpp": "C++",
    }
    for ext, lang in ext_map.items():
        if lang not in seen and any(root.rglob(f"*{ext}")):
            langs.append(lang)
            seen.add(lang)
    return langs


# ---------------------------------------------------------------------------
# Test/lint command extraction
# ---------------------------------------------------------------------------

def _commands_from_makefile(root: Path) -> tuple[list[str], list[str]]:
    text = _read(root / "Makefile")
    if not text:
        return [], []
    test_cmds: list[str] = []
    lint_cmds: list[str] = []
    # Collect targets and their recipe lines
    current_target: str | None = None
    for line in text.splitlines():
        target_match = re.match(r"^([a-zA-Z0-9_\-]+)\s*:", line)
        if target_match:
            current_target = target_match.group(1).lower()
            continue
        if current_target and line.startswith("\t"):
            recipe = line.strip()
            if current_target in ("test", "tests", "check"):
                test_cmds.append(recipe)
            elif current_target in ("lint", "vet", "fmt", "format", "staticcheck"):
                lint_cmds.append(recipe)
    # Also add `make test` / `make lint` if those targets exist
    targets = re.findall(r"^([a-zA-Z0-9_\-]+)\s*:", text, re.MULTILINE)
    for t in targets:
        tl = t.lower()
        if tl in ("test", "tests", "check") and f"make {t}" not in test_cmds:
            test_cmds.insert(0, f"make {t}")
        elif tl in ("lint", "vet", "fmt", "format") and f"make {t}" not in lint_cmds:
            lint_cmds.insert(0, f"make {t}")
    return test_cmds, lint_cmds


def _commands_from_go_mod(root: Path) -> tuple[list[str], list[str]]:
    if not (root / "go.mod").exists():
        return [], []
    return ["go test ./..."], ["go vet ./..."]


def _commands_from_package_json(root: Path) -> tuple[list[str], list[str]]:
    pkg_path = root / "package.json"
    if not pkg_path.exists():
        return [], []
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], []
    scripts: dict[str, str] = pkg.get("scripts", {})
    test_cmds: list[str] = []
    lint_cmds: list[str] = []
    # Detect package manager preference
    manager = "yarn" if (root / "yarn.lock").exists() else "npm"
    for script_name, script_body in scripts.items():
        if script_name in ("test", "test:unit", "test:ci"):
            test_cmds.append(f"{manager} run {script_name}")
        elif script_name in ("lint", "lint:ci", "eslint", "tslint"):
            lint_cmds.append(f"{manager} run {script_name}")
    return test_cmds, lint_cmds


def _commands_from_python(root: Path) -> tuple[list[str], list[str]]:
    has_pyproject = (root / "pyproject.toml").exists()
    has_setup_cfg = (root / "setup.cfg").exists()
    has_tox = (root / "tox.ini").exists()
    if not (has_pyproject or has_setup_cfg or has_tox or (root / "setup.py").exists()):
        return [], []
    test_cmds: list[str] = []
    lint_cmds: list[str] = []
    if has_tox:
        test_cmds.append("tox")
    else:
        test_cmds.append("pytest")
    # Check pyproject.toml for ruff/flake8/black
    if has_pyproject:
        text = _read(root / "pyproject.toml")
        if "ruff" in text:
            lint_cmds.append("ruff check .")
        if "flake8" in text:
            lint_cmds.append("flake8 .")
        if "mypy" in text:
            lint_cmds.append("mypy .")
    if not lint_cmds:
        lint_cmds.append("flake8 .")
    return test_cmds, lint_cmds


def _commands_from_cargo(root: Path) -> tuple[list[str], list[str]]:
    if not (root / "Cargo.toml").exists():
        return [], []
    return ["cargo test"], ["cargo clippy -- -D warnings"]


def _commands_from_ci(root: Path) -> tuple[list[str], list[str]]:
    """Mine .github/workflows/*.yml for test/lint steps."""
    test_cmds: list[str] = []
    lint_cmds: list[str] = []
    workflow_dir = root / ".github" / "workflows"
    for wf in _glob(workflow_dir, "*.yml") + _glob(workflow_dir, "*.yaml"):
        text = _read(wf)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("run:"):
                cmd = stripped[4:].strip().strip("|").strip()
                # Inline run: value (single line)
                if cmd:
                    _classify_ci_command(cmd, test_cmds, lint_cmds)
            elif stripped.startswith("-") and any(
                kw in stripped for kw in ("test", "pytest", "jest", "lint", "vet", "clippy")
            ):
                cmd = stripped.lstrip("- ").strip()
                if cmd:
                    _classify_ci_command(cmd, test_cmds, lint_cmds)
    return _dedup(test_cmds), _dedup(lint_cmds)


def _classify_ci_command(cmd: str, test_cmds: list[str], lint_cmds: list[str]) -> None:
    lower = cmd.lower()
    lint_kw = ("lint", "vet", "clippy", "flake8", "ruff", "eslint", "mypy", "staticcheck")
    test_kw = ("test", "pytest", "jest", "cargo test", "go test", "npm test")
    if any(k in lower for k in lint_kw):
        lint_cmds.append(cmd)
    elif any(k in lower for k in test_kw):
        test_cmds.append(cmd)


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Contribution guidelines extraction
# ---------------------------------------------------------------------------

_CONTRIB_FILES = [
    "CONTRIBUTING.md", "CONTRIBUTING.rst",
    "DEVELOPING.md", "DEVELOPMENT.md",
    "docs/CONTRIBUTING.md", "docs/DEVELOPING.md",
]


def _extract_contribution_guidelines(root: Path) -> str:
    for name in _CONTRIB_FILES:
        text = _read(root / name)
        if text:
            return text[:8000]  # cap to avoid bloating the profile
    return ""


# ---------------------------------------------------------------------------
# Issue template schema extraction
# ---------------------------------------------------------------------------

def _parse_issue_templates(root: Path) -> dict[str, Any]:
    """Return a dict of field names discovered in issue templates."""
    schema: dict[str, Any] = {"fields": [], "raw_templates": {}}
    template_dir = root / ".github" / "ISSUE_TEMPLATE"
    standalone = root / ".github" / "ISSUE_TEMPLATE.md"

    candidates: list[Path] = []
    if template_dir.is_dir():
        candidates = _glob(template_dir, "*.md") + _glob(template_dir, "*.yml") + _glob(template_dir, "*.yaml")
    if standalone.exists():
        candidates.append(standalone)
    # Also check legacy location
    legacy = root / "ISSUE_TEMPLATE.md"
    if legacy.exists():
        candidates.append(legacy)

    field_re = re.compile(
        r"###\s+(.+)|"           # GitHub form builder heading
        r"\*\*(.+?)\*\*\s*[:\n]|"  # Bold label
        r"^[-*]\s+\*\*(.+?)\*\*"  # Bullet bold label
        , re.MULTILINE
    )
    seen_fields: set[str] = set()

    for tmpl in candidates:
        text = _read(tmpl)
        if not text:
            continue
        schema["raw_templates"][tmpl.name] = text[:2000]
        for m in field_re.finditer(text):
            field = next(g for g in m.groups() if g)
            field = field.strip().lower()
            if field and field not in seen_fields and len(field) < 80:
                seen_fields.add(field)
                schema["fields"].append(field)

    return schema


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_repo_profile(repo_root: str | Path) -> RepoProfile:
    """
    Inspect *repo_root* and return a fully-populated RepoProfile.

    Parameters
    ----------
    repo_root:
        Absolute or relative path to the repository root directory.

    Returns
    -------
    RepoProfile
        Structured dictionary with keys: test_commands, lint_commands,
        contribution_guidelines, detected_languages, issue_template_schema.

    Raises
    ------
    FileNotFoundError
        If *repo_root* does not exist or is not a directory.
    """
    root = Path(repo_root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Repository root not found: {root}")

    # Collect test/lint commands from all sources
    test_cmds: list[str] = []
    lint_cmds: list[str] = []

    for extractor in (
        _commands_from_makefile,
        _commands_from_go_mod,
        _commands_from_package_json,
        _commands_from_python,
        _commands_from_cargo,
    ):
        t, l = extractor(root)
        test_cmds.extend(t)
        lint_cmds.extend(l)

    # CI workflows can add additional canonical commands
    ci_test, ci_lint = _commands_from_ci(root)
    test_cmds.extend(ci_test)
    lint_cmds.extend(ci_lint)

    return RepoProfile(
        test_commands=_dedup(test_cmds),
        lint_commands=_dedup(lint_cmds),
        contribution_guidelines=_extract_contribution_guidelines(root),
        detected_languages=_detect_languages(root),
        issue_template_schema=_parse_issue_templates(root),
    )
