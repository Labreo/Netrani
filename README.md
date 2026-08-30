<p align="center">
  <img src="docs/assets/logo.png" alt="Netrani Logo" width="220" />
</p>

<h1 align="center">Netrani</h1>

<p align="center">
  <strong>Autonomous Upstream Issue Verification & Triage Agent</strong><br>
  <em>Built with Purpose on IBM Bob 2.0</em>
</p>

<p align="center">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.11%2B-blue.svg" alt="Python: 3.11+"></a>
  <a href="tests/"><img src="https://img.shields.io/badge/Tests-86%20Passing-brightgreen.svg" alt="Tests: 86 Passing"></a>
  <a href=".bob/"><img src="https://img.shields.io/badge/IBM%20Bob-2.0%20Native-6f42c1.svg" alt="IBM Bob 2.0"></a>
</p>

**Netrani** is a general-purpose issue triage and verification tool built on **IBM Bob 2.0**. It inverts the standard generative AI coding lifecycle by establishing an upstream verification gate: **decide whether a bug report is genuine before authoring any code**.

Validated against a real-world enterprise Go compile-time instrumentation repository experiencing intake scaling challenges (*"Our issue and PR intake does not scale"*).

---

## Quick Invocations

```bash
# 1. Triage an incoming issue (read-only verification)
netrani run --repo /path/to/repo --issue <issue-id-or-file> --mode triage

# 2. Run offline triage on a local sample issue
netrani run --repo . --issue 42 --mode triage --offline

# 3. Run full end-to-end pipeline (Triage -> Gated Fix -> Test -> PR Draft)
netrani run --repo . --issue 42 --mode full --dry-run

# 4. Run the 18-issue curated ground-truth validation benchmark (strictly local/read-only)
python validation/run_benchmark.py --repo-root /path/to/repo
```

---

## The Core Inversion: Verify Before Generate

Standard generative coding assistants jump straight to generating code patches for every incoming bug report. When trackers fill with obsolete reports (already fixed on `main`), duplicates, or false positives, blind code generation burns maintainer review bandwidth and CI compute on code that should never have been written.

Netrani establishes an upstream verification gate, returning one of four cited verdicts:
- **`VALID`**: The defect is confirmed reachable and unguarded. Gated downstream subagents author a minimal surgical fix and verify tests.
- **`OBSOLETE`**: The defect was resolved in a prior commit (cited with commit SHA). Fix stages are skipped.
- **`DUPLICATE`**: The issue matches an existing open issue or PR (cited with issue/PR number). Fix stages are skipped.
- **`FALSE_POSITIVE`**: Static analysis proves the reported failure cannot occur under existing invariants (cited with file:line). Fix stages are skipped.

---

## Two-Tier Hybrid Architecture & Process Flow

Netrani optimizes intake economics and safety through a two-tier hybrid model:

```mermaid
flowchart TD
    classDef input fill:#1e293b,stroke:#64748b,stroke-width:2px,color:#f8fafc;
    classDef tier1 fill:#0f3b46,stroke:#06b6d4,stroke-width:2px,color:#ecfeff;
    classDef tier2 fill:#2e1065,stroke:#a855f7,stroke-width:2px,color:#faf5ff;
    classDef gate fill:#334155,stroke:#94a3b8,stroke-width:2px,color:#f8fafc;
    
    classDef vValid fill:#064e3b,stroke:#10b981,stroke-width:2px,color:#ecfdf5;
    classDef vObsolete fill:#0c4a6e,stroke:#0284c7,stroke-width:2px,color:#f0f9ff;
    classDef vDuplicate fill:#78350f,stroke:#f59e0b,stroke-width:2px,color:#fffbeb;
    classDef vFalsePos fill:#701a75,stroke:#d946ef,stroke-width:2px,color:#fdf4ff;
    
    classDef terminate fill:#450a0a,stroke:#ef4444,stroke-width:2px,color:#fef2f2;
    classDef action fill:#172554,stroke:#3b82f6,stroke-width:2px,color:#eff6ff;

    ISSUE["<b>Incoming Issue Report</b><br/><i>Title, Body, Stack Trace, Environment</i>"]:::input --> TIER1

    subgraph S_TIER1 ["Tier 1: Deterministic Intake Engine (0 Bobcoins / $0.00)"]
        TIER1["<b>Deterministic Static Analyzer</b><br/>- AST Guard Trees & Symbol Extraction<br/>- git log -S/-G Archaeology<br/>- Exact Duplicate Matcher"]:::tier1
        CONF{"Confidence >= 0.85?"}:::tier1
        TIER1 --> CONF
    end

    subgraph S_TIER2 ["Tier 2: IBM Bob 2.0 Agent Mode Escalation (~0.45 Bobcoins / issue)"]
        BOB_ORCH["<b>IBM Bob 2.0 Agent Mode</b><br/><i>(Guided by .bob/skills/triage/SKILL.md)</i>"]:::tier2
        HM["<b>history-miner</b><br/>Multi-file commit diffs & git graph analysis"]:::tier2
        SV["<b>static-validator</b><br/>Go AST control-flow & interface satisfaction"]:::tier2
        BOB_ORCH --> HM & SV
    end

    CONF -- "Yes (66.7% Volume)" --> S_VERDICTS
    CONF -- "No (Boundary Cases)" --> BOB_ORCH
    HM & SV --> S_VERDICTS

    subgraph S_VERDICTS ["4 Distinct Cited Verdicts (.bob/verdict.json)"]
        V_VALID["<b>VALID</b><br/>Reachable unguarded defect<br/><i>(Green: Proceed to fix)</i>"]:::vValid
        V_OBSOLETE["<b>OBSOLETE</b><br/>Already patched on main<br/><i>(Blue: Cited commit SHA)</i>"]:::vObsolete
        V_DUPLICATE["<b>DUPLICATE</b><br/>Existing open issue or PR<br/><i>(Orange: Cited issue URL)</i>"]:::vDuplicate
        V_FALSE_POS["<b>FALSE_POSITIVE</b><br/>Refuted by AST invariants<br/><i>(Magenta: Cited file:line)</i>"]:::vFalsePos
    end

    subgraph S_GATE ["Outer Harness Safety Gate (PreToolUse Hook)"]
        GATE{"<b>gate-fix.sh</b><br/>PreToolUse Hook"}:::gate
    end

    V_VALID --> GATE
    V_OBSOLETE --> GATE
    V_DUPLICATE --> GATE
    V_FALSE_POS --> GATE

    GATE -- "Status != VALID<br/>(Exit Code 2)" --> TERMINATE["<b>Pipeline Terminates Immediately</b><br/>- Certified proof cited in issue response<br/>- Zero wasted diffs / No CI compute"]:::terminate

    GATE -- "Status == VALID<br/>(Exit Code 0)" --> ALLOW["<b>Authorization Granted</b><br/>Unlock write_file & apply_diff tools"]:::vValid

    subgraph S_GATED ["Downstream Gated Remediation (VALID Only)"]
        FIXER["<b>surgical-fixer (Subagent 3)</b><br/>Minimal AST patch authoring"]:::action
        RUNNER["<b>test-runner (Subagent 4)</b><br/>Execute repository test suite & linters"]:::action
        AUDIT["<b>record-verdict.sh</b><br/>PostToolUse telemetry sensor"]:::action
        PR["<b>Pull Request / Verified Diff Artifact</b><br/>- Clean branch & disclosure trailer<br/>- Complete provenance audit log"]:::action

        ALLOW --> FIXER --> RUNNER --> AUDIT --> PR
    end
```

1. **Tier 1 (Deterministic Intake Engine — In-Process Heuristics):**
   - **Cost:** **0 Tokens ($0.00 / Zero API calls)** | **Avg Latency:** **6.15 s**
   - Resolves/filters **66.7% of incoming issue volume** (12/18 issues) completely offline in under 7 seconds.
2. **Tier 2 (IBM Bob 2.0 Agent Mode Escalation):**
   - **Cost:** **Agent Mode (Escalated Cases Only)** | **Avg Latency:** **~13.82 s**
   - Escalates ambiguous boundary cases (Go `defer` control flow, cross-package interface satisfaction, multi-file commit diffs) to specialized Bob custom modes.
   - Resolves **83.3% (5/6)** of escalated hard cases.

---

## Benchmark Results (18 Ground-Truth Issues)

Evaluated against the 18 curated ground-truth issues from an enterprise Go compile-time instrumentation project ([`validation/dataset/issues.json`](validation/dataset/issues.json)):

| Architecture Tier | Scope | Resolved / Correct | Filtration / Accuracy Rate | Avg Latency | Cost Profile |
|---|---|---|---|---|---|
| **Tier 1 (Deterministic Engine)** | All 18 issues | **12 / 18 issues filtered** | **66.7% Zero-Cost Filtration Rate**<br>(50.0% Standalone Accuracy, 87.5% on VALID) | **6.15 s** | **0 Tokens / $0.00 (Offline)** |
| **Tier 2 (Bob Escalation on 6 Hard Cases)** | 6 Misses (IDs 4, 5, 6, 7, 12, 15) | **5 / 6 resolved** | **83.3% Escalation Resolution Lift** | **13.82 s** | **Agent Mode (Escalation Only)** |
| **Combined Hybrid Performance** | **18 issues** | **14 / 18 correct** | **77.8% Overall Hybrid Accuracy** | **8.90 s (weighted)** | **93% Token Budget Savings** |

*Key Takeaway:* Netrani achieves **77.8% hybrid accuracy** while resolving **66.7% of incoming issue volume completely offline at zero token cost**, saving over 90% of the AI token budget.

See [`validation/results_table.md`](validation/results_table.md) for the per-issue breakdown.

---

## Native IBM Bob 2.0 Integration

- **Custom Modes ([`.bob/custom_modes.yaml`](.bob/custom_modes.yaml)):**
  - `history-miner`: Git archaeology specialist (read-only commit log and diff search).
  - `static-validator`: AST invariant and type checker (read-only symbol analysis).
  - `surgical-fixer`: Minimal patch generator (gated behind `VALID` verdict).
  - `test-runner`: Test execution and coverage verifier.
- **Outer Harness ([`.bob/settings.json`](.bob/settings.json)):**
  - **Feedforward Guides:** [`.bob/skills/triage/SKILL.md`](.bob/skills/triage/SKILL.md) instructs exploration rules and confidence scoring.
  - **`PreToolUse` Safety Gate ([`gate-fix.sh`](.bob/hooks/gate-fix.sh)):** Intercepts `write_file`/`apply_diff` and exits with code `2` if `.bob/verdict.json` is not `VALID`.
  - **`PostToolUse` Audit Sensor ([`record-verdict.sh`](.bob/hooks/record-verdict.sh)):** Captures command exit codes and telemetry into `.bob/audit.log`.

---

## CLI Reference

```bash
# General Syntax
netrani <command> [options]

# Commands:
#   run     Execute triage, fix, and verification pipeline
#   triage  Execute triage verification only (read-only)
#   pr      Generate PR draft and submit to GitHub

# Examples:
# 1. Offline heuristic intake (Tier 1 zero-cost filtration)
netrani triage --repo . --issue 42 --offline

# 2. Triage with IBM Bob 2.0 Agent Mode & custom subagents
netrani triage --repo . --issue 42 --use-bob

# 3. Run full end-to-end pipeline in dry-run mode (Triage -> Fix -> Verify -> PR Draft)
netrani run --repo . --issue 42 --mode full --use-bob --dry-run

# 4. Generate PR draft from verified fix
netrani pr --repo . --base-branch main --dry-run
```

---

## Repository Structure

```
Netrani/
├── .bob/                                    # IBM Bob 2.0 native agent configuration (Tier 2)
│   ├── custom_modes.yaml                    # 4 custom mode personas
│   ├── settings.json                        # PreToolUse & PostToolUse hooks
│   ├── skills/triage/SKILL.md               # Triage team skill
│   ├── hooks/gate-fix.sh                    # PreToolUse guard (blocks writes on non-VALID verdict)
│   ├── hooks/record-verdict.sh              # PostToolUse sensor (audit log)
│   └── verdict.schema.json                  # JSON schema for .bob/verdict.json
│
├── netrani/                                 # Deterministic intake engine (Tier 1)
│   ├── subagents/                           # history_miner, static_validator, surgical_fixer, test_runner
│   ├── triage/                              # orchestrator (synthesis)
│   ├── pipeline/                            # git_emitter & orchestration
│   ├── parser/                              # doc_parser (CONTRIBUTING.md, manifests)
│   ├── issue/                               # issue fetcher & schema
│   ├── bob/                                 # IBM Bob 2.0 Agent Mode integration (agent.py)
│   ├── config.py                            # Central configuration & runtime paths
│   └── cli.py                               # CLI entrypoint
│
├── docs/                                    # Official contest documentation
│   ├── problem_and_solution_statement.md    # 500-word problem & solution statement
│   └── usage_statement.md                   # Bob 2.0 usage & economic framing statement
│
├── video/                                   # Demo video deliverables
│   └── README.md                            # 3-minute timed demo script & video specifications
│
├── bob_sessions/                            # Mandatory IBM Bob IDE session consumption summaries
│   ├── README.md                            # Bob session screenshot inventory & budget summary
│   └── kanak_waradkar_task01..09_*.png      # 9 session consumption screenshots
│
├── validation/                              # Benchmark validation artifacts
│   ├── dataset/issues.json                  # 18-issue ground-truth dataset
│   ├── results_table.md                     # Full benchmark results & miss analysis
│   ├── benchmark_results.json               # Machine-readable evaluation output
│   └── run_benchmark.py                     # Benchmark runner
│
├── tests/                                   # Test suite (86 passing unit tests)
│   ├── test_bob_agent.py
│   ├── test_execution_gate.py
│   ├── test_gate_fix.py
│   ├── test_history_miner.py
│   ├── test_pr_mechanics.py
│   └── test_static_validator.py
│
├── README.md                                # Top-level project documentation
├── LICENSE                                  # MIT License
└── pyproject.toml                           # Package configuration
```

---

## Safety, Privacy & Disclosures

- **Read-Only Local Evaluation:** Benchmark evaluation (18 issues) and automated testing execute **100% locally and read-only**. Netrani does **NOT** post or submit unsolicited pull requests or comments to real upstream repositories.
- **Controlled PR Emission:** The `--create-pr` flag operates exclusively on user-owned repository forks where explicitly configured; default execution runs in `--dry-run` or local branch mode.
- **Solo Participant:** Kanak Waradkar (GitHub: [`Labreo`](https://github.com/Labreo))
- **Affiliation:** Goa College of Engineering (GEC Goa)
- **Event:** IBM TechXchange 2026 Pre-conference Dev Day Hackathon — *"Build with purpose using IBM Bob 2.0"*
- **License:** MIT License (see [LICENSE](LICENSE)).
