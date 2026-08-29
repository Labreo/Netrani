# Netrani: Usage Statement & Bob 2.0 Best Practices

**Participant & Affiliation:** Kanak Waradkar — Goa College of Engineering (GEC Goa)  
**Hackathon:** IBM TechXchange 2026 Pre-conference Dev Day — "Build with purpose using IBM Bob 2.0"  
**Repository:** [github.com/Labreo/Netrani](https://github.com/Labreo/Netrani)  
**Target Proof Case:** `open-telemetry/opentelemetry-go-compile-instrumentation` (Issue #973)

---

## 1. Economic Framing: Intake Cost Optimization & Bobcoin Budget Hygiene

In enterprise software engineering and open-source ecosystems, running unconstrained LLM agent loops against every incoming issue report is economically unsustainable and rapidly exhausts token budgets. Under the official hackathon constraint of **40 Bobcoins total per account** (`ibm-coding-challenge-uat` instance, region: `us-east`), disciplined resource allocation is vital.

Netrani frames its primary value proposition around **Intake Cost Optimization**:

- **66.7% Zero-Cost Filtration Rate:** 12 out of 18 issues in the benchmark dataset are resolved at **0 Bobcoins / 0 tokens / $0.00** via pure in-process deterministic heuristics.
- **Surgical Bobcoin Deployment:** Bobcoins are deployed strictly to the 6 boundary cases where LLM semantic reasoning provides decisive lift (e.g., Go `defer` control flow, cross-package interface method satisfaction, and multi-file commit diff synthesis).
- **High ROI on Bobcoin Budget:** Triaging the entire 18-issue validation suite consumed only **2.70 out of 40.00 Bobcoins (6.75%)**, leaving **37.30 Bobcoins (93.25%)** untouched for live interactive development, demo recordings, and PR generation.
- **Amortized Unit Economics:** Average cost per incoming issue across the repository is reduced to **0.15 Bobcoins ($0.00 equivalent)**.

---

## 2. Literal Application of Bob 2.0 Core Concepts & Features

Netrani maps directly onto IBM Bob 2.0's foundational engineering patterns:

### The Basic Cycle (Explore → Plan → Implement → Verify)
1. **Explore (Primary Billing — Subagents 1 & 2 in Parallel):**
   - `history-miner` (`.bob/custom_modes.yaml`): Executes read-only Git archaeology (`git log -S/-G`, commit diff inspection) to identify resolved or duplicate defects.
   - `static-validator` (`.bob/custom_modes.yaml`): Performs read-only AST inspection, symbol resolution, and type/guard invariant analysis.
2. **Plan (The Triage Verdict):**
   - Synthesizes findings into a canonical, schema-validated `.bob/verdict.json` object (`VALID`, `DUPLICATE`, `OBSOLETE`, `FALSE_POSITIVE`) with mandatory evidence citations (commit SHA or file:line range).
3. **Implement (Subagent 3 — Gated Surgical Fixer):**
   - `surgical-fixer` (`.bob/custom_modes.yaml`): Authors the minimal, surgical code diff required to resolve the defect. Gated strictly behind a verified `VALID` verdict.
4. **Verify (Subagent 4 — Dynamic Test Runner):**
   - `test-runner` (`.bob/custom_modes.yaml`): Dynamically discovers repository test and lint commands from manifests (`CONTRIBUTING.md`, `Makefile`, `pyproject.toml`, `go.mod`) and validates patches.

### Context Discipline over Compaction
Rather than dumping entire repositories into an unconstrained 270k context window and relying on lossy context compaction, Netrani strictly scopes each subagent's context:
- `history-miner` receives commit logs and git diffs, never raw AST source trees.
- `static-validator` receives symbol bodies and call graphs, never commit histories.
- `surgical-fixer` receives only the verified defect and reproduction plan.

### Outer Harness: Guides vs. Sensors
- **Guides (Feedforward):** `.bob/skills/triage/SKILL.md` guides subagent exploration before decisions are executed.
- **Sensors (Feedback & Safety):** 
  - `gate-fix.sh` (`PreToolUse` hook on `write_file`/`apply_diff`): Exits with code `2` to block code modification if the verdict is not `VALID`.
  - `record-verdict.sh` (`PostToolUse` hook on `execute_command`): Appends structured exit codes and execution telemetry to `.bob/audit.log`.

---

## 3. Responsible Open-Source Contribution Policy

To adhere to open-source community standards and OpenTelemetry's GenAI Contribution Policy:
- **Validation Batch:** Evaluated strictly read-only against ground-truth datasets without opening unsolicited PRs against upstream repositories.
- **Demo Proof Case:** Exactly **one** real, live pull request is filed for the verified demo issue, personally reviewed by the author and carrying full disclosure (`Assisted-by: IBM Bob 2.0 / Netrani`).
- **Local Artifacts:** All other valid verdicts generate ready-to-submit local diffs and branches.

---

## 4. Multi-Repository Generality & Scaling Story

Netrani does not hardcode target repository assumptions. At runtime, the CLI and Bob custom modes dynamically discover:
1. Contribution guidelines (`CONTRIBUTING.md`, `DEVELOPING.md`).
2. Issue template schemas (`.github/ISSUE_TEMPLATE/`).
3. Verification toolchains (`go test`, `pytest`, `cargo test`, `npm test`, `golangci-lint`, `ruff`).

While validated deeply against `open-telemetry/opentelemetry-go-compile-instrumentation` as a primary proof case to demonstrate empirical rigor, the identical pipeline executes across any Git repository with a standard issue tracker and test runner.
