#!/usr/bin/env python3
"""
validation/run_benchmark.py
Netrani Phase 3 — Validation Batch Benchmark Runner

Reads validation/dataset/issues.json, converts each entry into a local
fixture that the Netrani triage engine can consume via --offline mode, runs
the triage orchestrator against each, records the verdict, and writes the
raw results to validation/benchmark_results.json.

Usage:
    python validation/run_benchmark.py [--repo-root <path>] [--verbose]

The script:
  1. Loads the ground-truth dataset.
  2. For each issue, creates a temporary fixture JSON under
     netrani/fixtures/<tmp_id>.json so the offline fetcher can load it.
  3. Invokes netrani.triage.orchestrator.run() directly (in-process) to
     avoid subprocess overhead and to capture the structured verdict dict.
  4. Compares the returned verdict['status'] against expected_verdict.
  5. Records match/mismatch, confidence, latency_ms, and citation.
  6. Writes validation/benchmark_results.json.
  7. Prints a live progress table to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Ensure the repo root is on the path
_REPO_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(_REPO_ROOT))

from netrani.triage.orchestrator import run as triage_run  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_dataset(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _make_fixture(issue: dict, fixture_dir: Path) -> Path:
    """Write a fixture JSON the offline fetcher accepts and return its path."""
    fixture = {
        "title": issue["title"],
        "body": issue["body"],
        "author": "benchmark-runner",
        "labels": [],
        "url": issue["url"],
    }
    fpath = fixture_dir / f"bm_{issue['id']}.json"
    fpath.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    return fpath


def _remove_fixture(fpath: Path) -> None:
    try:
        fpath.unlink()
    except OSError:
        pass


def _verdict_verdict_path(repo_root: Path, issue_id: int) -> Path:
    vdir = repo_root / ".bob" / "benchmark_verdicts"
    vdir.mkdir(parents=True, exist_ok=True)
    return vdir / f"verdict_{issue_id}.json"


def _extract_suspect_symbols(body: str) -> list[str]:
    """Minimal symbol extractor matching the fetcher heuristic (backticks)."""
    import re
    symbols = []
    seen: set[str] = set()
    for m in re.finditer(r"`([^`\n]{2,60})`", body):
        cand = m.group(1).strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_./\-:]*(\(.*\))?$", cand):
            if cand not in seen:
                seen.add(cand)
                symbols.append(cand)
    return symbols[:20]


# ---------------------------------------------------------------------------
# Table renderer
# ---------------------------------------------------------------------------

_COL_ID    = 4
_COL_TITLE = 52
_COL_EXP   = 16
_COL_GOT   = 16
_COL_MATCH = 7
_COL_CONF  = 6
_COL_MS    = 7


def _hr() -> str:
    return (
        "+-" + "-" * _COL_ID + "-+-"
        + "-" * _COL_TITLE + "-+-"
        + "-" * _COL_EXP + "-+-"
        + "-" * _COL_GOT + "-+-"
        + "-" * _COL_MATCH + "-+-"
        + "-" * _COL_CONF + "-+-"
        + "-" * _COL_MS + "-+"
    )


def _header_row() -> str:
    return (
        "| " + "ID".ljust(_COL_ID) + " | "
        + "Title".ljust(_COL_TITLE) + " | "
        + "Expected".ljust(_COL_EXP) + " | "
        + "Got".ljust(_COL_GOT) + " | "
        + "Match".ljust(_COL_MATCH) + " | "
        + "Conf".ljust(_COL_CONF) + " | "
        + "ms".ljust(_COL_MS) + " |"
    )


def _data_row(r: dict) -> str:
    match_sym = "✓ YES " if r["match"] else "✗ NO  "
    return (
        "| " + str(r["id"]).ljust(_COL_ID) + " | "
        + r["title"][:_COL_TITLE].ljust(_COL_TITLE) + " | "
        + r["expected_verdict"].ljust(_COL_EXP) + " | "
        + r["actual_verdict"].ljust(_COL_GOT) + " | "
        + match_sym.ljust(_COL_MATCH) + " | "
        + f"{r['confidence']:.2f}".ljust(_COL_CONF) + " | "
        + str(r["latency_ms"]).ljust(_COL_MS) + " |"
    )


# ---------------------------------------------------------------------------
# Main benchmark loop
# ---------------------------------------------------------------------------

def run_benchmark(
    repo_root: Path,
    verbose: bool = False,
) -> list[dict]:
    dataset_path = _REPO_ROOT / "validation" / "dataset" / "issues.json"
    fixture_dir = _REPO_ROOT / "netrani" / "fixtures"
    fixture_dir.mkdir(exist_ok=True)

    issues = _load_dataset(dataset_path)
    results: list[dict] = []

    print()
    print("━" * 100)
    print("  Netrani Phase 3 — Validation Batch Benchmark")
    print(f"  Dataset: {dataset_path}")
    print(f"  Issues:  {len(issues)}")
    print(f"  Repo:    {repo_root}")
    print("━" * 100)
    print()
    print(_hr())
    print(_header_row())
    print(_hr())

    for issue in issues:
        fpath = _make_fixture(issue, fixture_dir)
        vpath = _verdict_verdict_path(repo_root, issue["id"])

        symbols = _extract_suspect_symbols(issue["body"])

        t0 = time.perf_counter()
        try:
            verdict = triage_run(
                repo_path=repo_root,
                issue_reference=issue["url"],
                title=issue["title"],
                body=issue["body"],
                suspect_symbols=symbols,
                reproduction_trace=[],
                issue_url=issue["url"],
                verdict_path=str(vpath),
                quiet=True,
            )
            actual_verdict = verdict.get("status", "ERROR")
            confidence = float(verdict.get("confidence", 0.0))
            citation = verdict.get("citation", "")
            error = ""
        except Exception as exc:  # noqa: BLE001
            actual_verdict = "ERROR"
            confidence = 0.0
            citation = ""
            error = str(exc)[:200]
        finally:
            _remove_fixture(fpath)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        match = actual_verdict == issue["expected_verdict"]

        row = {
            "id": issue["id"],
            "url": issue["url"],
            "title": issue["title"],
            "expected_verdict": issue["expected_verdict"],
            "actual_verdict": actual_verdict,
            "match": match,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "citation": citation,
            "ground_truth_citation": issue["ground_truth_citation"],
            "ground_truth_rationale": issue["ground_truth_rationale"],
            "error": error,
        }
        results.append(row)
        print(_data_row(row))

    print(_hr())
    print()

    # Summary statistics
    total   = len(results)
    correct = sum(1 for r in results if r["match"])
    accuracy = correct / total if total > 0 else 0.0
    avg_latency = int(sum(r["latency_ms"] for r in results) / total) if total > 0 else 0
    avg_conf    = sum(r["confidence"] for r in results) / total if total > 0 else 0.0

    # Per-category breakdown
    for cat in ("VALID", "DUPLICATE", "OBSOLETE", "FALSE_POSITIVE"):
        cat_items = [r for r in results if r["expected_verdict"] == cat]
        cat_correct = sum(1 for r in cat_items if r["match"])
        cat_acc = cat_correct / len(cat_items) if cat_items else 0.0
        print(f"  [{cat:16s}]  {cat_correct}/{len(cat_items)} correct  ({cat_acc:.0%})")

    print()
    print(f"  Overall accuracy : {correct}/{total}  ({accuracy:.1%})")
    print(f"  Avg confidence   : {avg_conf:.2f}")
    print(f"  Avg latency      : {avg_latency} ms")
    print()
    print("━" * 100)
    print()

    return results


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------

def write_results(results: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "benchmark_version": "1.0",
                "repo": "open-telemetry/opentelemetry-go-compile-instrumentation",
                "reference_issue": "#973",
                # ── Architecture transparency ──────────────────────────────────
                # This benchmark runs the Netrani Python heuristic engine
                # (netrani/subagents/ + netrani/triage/) directly in-process.
                # Zero IBM Bob Shell or LLM calls are made during this run.
                # The .bob/ directory provides the native IBM Bob 2.0 agent
                # configuration (custom_modes, skills, hooks) for interactive
                # use in Bob IDE — it is NOT invoked by this benchmark runner.
                "live_bob_session": False,
                "engine": "Netrani Python heuristic engine v0.1.0 (offline mode)",
                "bob_config": ".bob/ directory (custom_modes.yaml, skills/triage/SKILL.md, hooks/)",
                # ──────────────────────────────────────────────────────────────
                "total": len(results),
                "correct": sum(1 for r in results if r["match"]),
                "accuracy": sum(1 for r in results if r["match"]) / len(results),
                "avg_confidence": sum(r["confidence"] for r in results) / len(results),
                "avg_latency_ms": int(sum(r["latency_ms"] for r in results) / len(results)),
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  Results written → {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_benchmark",
        description="Netrani Phase 3 — Validation Batch Benchmark Runner",
    )
    _OTel_REPO = "/Users/sanjaywaradkar/opentelemetry-go-compile-instrumentation"
    p.add_argument(
        "--repo-root",
        default=_OTel_REPO,
        metavar="<path>",
        help=f"Repository root to triage against (default: {_OTel_REPO})",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print per-verdict ASCII tables to stdout",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = Path(args.repo_root).expanduser().resolve()

    results = run_benchmark(repo_root=repo_root, verbose=args.verbose)
    out_path = _REPO_ROOT / "validation" / "benchmark_results.json"
    write_results(results, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
