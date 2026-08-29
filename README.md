# Netrani

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Netrani** is a general-purpose IBM Bob 2.0 agent that validates incoming bug reports
before any code is written against them.  It returns one of four cited verdicts —
`VALID`, `DUPLICATE`, `OBSOLETE`, or `FALSE_POSITIVE` — using a three-tier
verification workflow: git archaeology, static code analysis, and synthesis.

Validated against `open-telemetry/opentelemetry-go-compile-instrumentation` in
response to [issue #973](https://github.com/open-telemetry/opentelemetry-go-compile-instrumentation/issues/973)
("Our issue and PR intake does not scale").

**Hackathon:** IBM TechXchange 2026 Pre-conference Dev Day — "Build with purpose using IBM Bob 2.0"
**Team:** Kanak Waradkar (Solo) — Goa College of Engineering (GEC Goa)
**Data sources used:** `github.com/open-telemetry/opentelemetry-go-compile-instrumentation` (issue #973)

---

## Architecture — Two Tiers, Clearly Separated

Netrani has two distinct operational tiers.  They are not the same thing and must not
be conflated, especially for submission evaluation:

### Tier A — Offline Python Heuristic Engine (this benchmark)

Located in `netrani/subagents/` and `netrani/triage/`.

**What it does:** Pure Python, zero external dependencies, zero LLM calls.
Runs `git log -S`, `git log -G`, and regex/AST analysis against a local clone
of the target repository.  The benchmark runner (`validation/run_benchmark.py`)
invokes this tier directly, in-process.

**When it runs:** Every time `python validation/run_benchmark.py` is executed.
The `benchmark_results.json` field `"live_bob_session": false` confirms this
programmatically.

**What it proves:** That the triage logic is correct, deterministic, auditable,
and produces non-trivial results above the "always VALID" baseline — without
relying on any LLM or external service.

### Tier B — IBM Bob 2.0 Native Agent (interactive use)

Located in `.bob/`.

**What it does:** Configures IBM Bob IDE's Agent Mode with:
- **4 custom mode personas** ([`.bob/custom_modes.yaml`](.bob/custom_modes.yaml)):
  `history-miner`, `static-validator`, `surgical-fixer`, `test-runner`
- **Team skill** ([`.bob/skills/triage/SKILL.md`](.bob/skills/triage/SKILL.md)):
  activates on "triage issue #NNN" in Bob IDE's chat to run the three-tier workflow
- **PreToolUse hook** ([`.bob/hooks/gate-fix.sh`](.bob/hooks/gate-fix.sh)):
  blocks `write_file`/`apply_diff` with exit code 2 unless `.bob/verdict.json`
  contains `"status": "VALID"`
- **PostToolUse sensor** ([`.bob/hooks/record-verdict.sh`](.bob/hooks/record-verdict.sh)):
  captures test exit codes to `.bob/audit.log`

**When it runs:** Only when invoked interactively in IBM Bob IDE with Agent Mode
enabled.  The automated benchmark does NOT invoke Bob.

**What it proves:** The IBM Bob 2.0 feature integration — custom modes,
guide/sensor hooks, team skills, and the Ask → Plan → Agent mode alignment.

---

## Benchmark Results (Offline Tier)

Run date: 2026-08-29 | Engine: Netrani Python heuristic v0.1.0 | `live_bob_session: false`

| Category | Correct | Accuracy |
|---|---|---|
| VALID | 6 / 8 | 75.0% |
| DUPLICATE | 2 / 3 | 66.7% |
| OBSOLETE | 1 / 4 | 25.0% |
| FALSE_POSITIVE | 0 / 3 | 0.0% |
| **Total** | **9 / 18** | **50.0%** |

See [`validation/results_table.md`](validation/results_table.md) for the full per-issue breakdown.

---

## Quick Start

```bash
# Install (Python 3.11+)
pip install -e .

# Run the offline benchmark against your local clone of the target repo
python validation/run_benchmark.py \
  --repo-root /path/to/opentelemetry-go-compile-instrumentation

# Triage a single issue via CLI
netrani triage \
  --repo /path/to/opentelemetry-go-compile-instrumentation \
  --issue "https://github.com/open-telemetry/opentelemetry-go-compile-instrumentation/issues/973" \
  --title "Our issue and PR intake does not scale"
```

---

## Repository Layout

```
.bob/                        # IBM Bob 2.0 native agent configuration (Tier B)
│  custom_modes.yaml         # 4 custom mode personas
│  settings.json             # Hook registration
│  skills/triage/SKILL.md    # Triage team skill
│  hooks/gate-fix.sh         # PreToolUse guard (blocks writes on non-VALID verdict)
│  hooks/record-verdict.sh   # PostToolUse sensor (audit log)
│  verdict.schema.json       # JSON schema for .bob/verdict.json
│
netrani/                     # Python heuristic engine (Tier A)
│  subagents/
│    history_miner.py        # Tier 1: git archaeology
│    static_validator.py     # Tier 2: AST + guard analysis
│    surgical_fixer.py       # (post-triage) minimal patch generator
│    test_runner.py          # (post-triage) test discovery + execution
│  triage/
│    orchestrator.py         # Tier 3: synthesis → verdict.json
│
validation/
│  run_benchmark.py          # Offline batch benchmark runner
│  dataset/issues.json       # 18-issue ground-truth dataset
│  results_table.md          # Full benchmark results table
│  benchmark_results.json    # Machine-readable results (live_bob_session: false)
```

---

## License

MIT — see [LICENSE](LICENSE) for details.

**Affiliation disclosure (contest requirement):** Goa College of Engineering (GEC Goa)
