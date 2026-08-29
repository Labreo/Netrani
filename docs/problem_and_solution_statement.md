# Netrani: Problem and Solution Statement

**Author & Affiliation:** Kanak Waradkar — Goa College of Engineering (GEC Goa)  
**Hackathon:** IBM TechXchange 2026 Pre-conference Dev Day Hackathon — "Build with purpose using IBM Bob 2.0"  
**Target Proof Case:** `open-telemetry/opentelemetry-go-compile-instrumentation` (Issue #973)

### Problem
Open source maintainers face an intake scaling crisis. In `open-telemetry/opentelemetry-go-compile-instrumentation` issue #973 ("Our issue and PR intake does not scale"), maintainers documented that unvalidated bug reports and premature pull requests overwhelm review bandwidth. Incoming trackers fill with obsolete reports already fixed on `main`, duplicate filings, and false-positive claims that are technically impossible under the codebase's runtime architecture. Standard generative AI tools exacerbate this burden by immediately generating code patches for non-existent defects, burning maintainer hours and CI compute on code that should never have been written.

### Solution: Verify Before Generate
Netrani inverts the standard generative coding lifecycle by establishing an upstream verification gate. Built natively on IBM Bob 2.0, Netrani evaluates whether a defect is genuine before authoring any code.

Netrani employs a **Two-Tier Hybrid Architecture**:
1. **Tier 1 (Deterministic Intake Engine):** Fast, offline Python heuristics (`history-miner`, `static-validator`) perform git archaeology (`git log -S/-G`) and AST guard analysis at **0 Bobcoins / $0.00**, resolving 66.7% of incoming issue volume without burning LLM budget.
2. **Tier 2 (IBM Bob 2.0 Agent Mode Escalation):** For ambiguous boundary cases, Netrani escalates to specialized Bob custom modes (`history-miner`, `static-validator`) for semantic reasoning (e.g. Go `defer` control flow, interface satisfaction).
3. **Gated Remediation & Outer Harness:** A `PreToolUse` hook (`gate-fix.sh`) strictly blocks file edits (`write_file`/`apply_diff`) unless a verified `VALID` verdict exists in `.bob/verdict.json`. For verified bugs, the `surgical-fixer` mode authors a minimal patch and `test-runner` executes real repo test suites, captured via a `PostToolUse` audit sensor (`record-verdict.sh`).

Every verdict returns one of four cited states: `VALID`, `DUPLICATE`, `OBSOLETE`, or `FALSE_POSITIVE`, anchored by commit SHAs or file:line citations.

### Results & Scaling Story
Benchmarked against 18 curated ground-truth issues from the OpenTelemetry Go compile-instrumentation repository:
- **Accuracy:** Tier 1 heuristic baseline (50.0%) escalates to **83.3% overall hybrid accuracy** (15/18 resolved) with 100% resolution on escalated hard cases.
- **Budget Hygiene:** Triaging the entire 18-issue suite consumed only **2.70 out of 40.00 Bobcoins (6.75%)**, amortizing to **0.15 Bobcoins/issue**.
- **Generality & Scaling:** Netrani discovers repository properties dynamically at runtime—reading `CONTRIBUTING.md`, manifests, issue templates, and test commands (`pytest`, `go test`, `make`) without project-specific hardcoding. The validation evidence is scoped deeply to one real repository to demonstrate empirical rigor, while the architecture generalizes seamlessly across any Git project. Post-hackathon, this "verify before trusting" model naturally extends to validating stale-base pull requests against concurrent commits.
