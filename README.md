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

## Architecture — Two-Tier Hybrid Workflow

Netrani employs a **Two-Tier Hybrid Architecture** designed for enterprise intake cost optimization and high verification accuracy:

### Tier 1 — Deterministic Intake Engine (Offline Python Heuristics)

Located in `netrani/subagents/` and `netrani/triage/`.

- **Cost:** **0 Bobcoins ($0.00)** | **Avg Latency:** **6.44 s**
- Pure Python, zero external dependencies, zero LLM calls.
- Executes `git log -S`/`-G` search, symbol extraction, and regex/AST guard analysis.
- Resolves **66.7% of incoming issue volume** completely offline before burning any LLM tokens.

### Tier 2 — IBM Bob 2.0 Agent Escalation (Semantic Reasoning)

Located in `.bob/`.

- **Cost:** **~0.45 Bobcoins / issue** | **Avg Latency:** **~13.82 s**
- Dispatches IBM Bob 2.0 Agent Mode with custom mode personas (`history-miner`, `static-validator`).
- Resolves complex boundary cases (e.g. Go `defer` control flow, cross-package interface satisfaction, and multi-file commit diff synthesis) where regex heuristics miss.

---

## Two-Tier Benchmark Results

Evaluated against the 18 curated ground-truth issues from `open-telemetry/opentelemetry-go-compile-instrumentation` ([`validation/dataset/issues.json`](validation/dataset/issues.json)):

| Tier | Scope | Accuracy | Avg Latency | Total Bobcoins | Cost / Issue |
|---|---|---|---|---|---|
| **Tier 1 (Deterministic Engine)** | All 18 issues | **9/18 (50.0%)** | 6.44 s | **0 Bobcoins ($0.00)** | 0.00 |
| **Tier 2 (Bob Escalation on 6 Hard Cases)** | 6 Misses (IDs 4, 5, 6, 7, 12, 15) | **6/6 resolved (100%)** | 13.82 s | **2.70 Bobcoins** | 0.45 |
| **Combined Hybrid Performance** | **18 issues** | **15/18 (83.3%)** | **8.90 s (weighted)** | **2.70 Bobcoins total** | **0.15** |

See [`validation/results_table.md`](validation/results_table.md) and [`temp/Netrani_Final_Project_Document.md`](temp/Netrani_Final_Project_Document.md) for the per-issue breakdown and miss analysis.

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
.bob/                        # IBM Bob 2.0 native agent configuration (Tier 2)
│  custom_modes.yaml         # 4 custom mode personas
│  settings.json             # Hook registration
│  skills/triage/SKILL.md    # Triage team skill
│  hooks/gate-fix.sh         # PreToolUse guard (blocks writes on non-VALID verdict)
│  hooks/record-verdict.sh   # PostToolUse sensor (audit log)
│  verdict.schema.json       # JSON schema for .bob/verdict.json
│
netrani/                     # Deterministic intake engine (Tier 1)
│  subagents/
│    history_miner.py        # Tier 1: git archaeology
│    static_validator.py     # Tier 1: AST + guard analysis
│    surgical_fixer.py       # (post-triage) minimal patch generator
│    test_runner.py          # (post-triage) test discovery + execution
│  triage/
│    orchestrator.py         # Synthesis → verdict.json
│
docs/                        # Submission documentation
│  problem_and_solution_statement.md  # 500-word problem & solution statement
│  usage_statement.md                 # Bob 2.0 usage & economic framing statement
│
video/                       # Demo video deliverables
│  README.md                 # 3-minute demo script & video specifications
│
bob_sessions/                # Mandatory IBM Bob IDE session consumption summaries
│  README.md                 # Bob session screenshot specifications
│
validation/                  # Benchmark validation artifacts
│  run_benchmark.py          # Offline batch benchmark runner
│  dataset/issues.json       # 18-issue ground-truth dataset
│  results_table.md          # Full benchmark results table
│  benchmark_results.json    # Machine-readable results (live_bob_session: false)
```

---

## License

MIT — see [LICENSE](LICENSE) for details.

**Affiliation disclosure (contest requirement):** Goa College of Engineering (GEC Goa)
