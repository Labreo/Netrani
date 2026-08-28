---
name: triage
description: >-
  Use when the user wants to triage, verify, or investigate a reported issue, bug, or defect
  before any code modification occurs. Runs a three-tier verification workflow (history check,
  static code check, synthesis) and writes a deterministic verdict to .bob/verdict.json.
  Activates on phrases like "triage issue", "verify bug", "investigate defect", "is this a
  real bug", or "check if issue is valid".
---

# Netrani Triage Skill

**Philosophy: Verify before acting.**

This skill governs Netrani's Explore phase. Its sole output is a deterministic verdict written
to `.bob/verdict.json` that conforms to `.bob/verdict.schema.json`. No source file is touched
until that verdict exists and carries `"status": "VALID"`.

---

## Verdict Taxonomy

| Status | Meaning | Required Citation |
|---|---|---|
| `VALID` | Defect is reproducible or statically confirmed; no existing commit resolves it. | File path + line number of the failure path, or reproduction trace. |
| `DUPLICATE` | Same root cause is already tracked in an open issue or PR. | URL of the existing issue or PR. |
| `OBSOLETE` | Defect was already fixed in a prior commit on the default branch. | Full commit SHA (40 hex characters). |
| `FALSE_POSITIVE` | Static code inspection proves the failure mode is technically impossible. | File path + line range proving impossibility. |

A verdict with `status != VALID` immediately terminates the workflow. No fix is drafted.

---

## Three-Tier Verification Workflow

Execute each tier in sequence. Do not skip a tier. Record findings from each tier before
proceeding to the next.

---

### Tier 1 — History Check (history-miner)

**Goal:** Determine if the defect is `DUPLICATE` or `OBSOLETE` before spending time on code
inspection.

**Steps:**

1. Invoke the `history-miner` mode (or use `execute_command` in the current mode with read-only
   Git queries).

2. Extract the core symptom keywords from the issue report (function names, error messages,
   file paths, variable names).

3. Run targeted Git history searches:
   ```
   git log -S "<keyword>" --oneline --all
   git log -G "<pattern>" --oneline --all
   git log --all --oneline --grep="<keyword>"
   git log --oneline --all -- <suspect_file>
   ```

4. For each matching commit, inspect the diff:
   ```
   git show <sha> -- <file>
   ```

5. Search for open and recently closed issues/PRs in any linked issue tracker files
   (e.g. `CHANGELOG.md`, `RELEASES.md`) or referenced in commit messages.

6. **Decision after Tier 1:**
   - If a commit on the default branch resolves the exact root cause → **verdict `OBSOLETE`**,
     cite the SHA, skip to Tier 3 Synthesis.
   - If an open issue/PR tracks the same root cause → **verdict `DUPLICATE`**, cite the URL,
     skip to Tier 3 Synthesis.
   - If no matching history → continue to Tier 2.

---

### Tier 2 — Static Code Check (static-validator)

**Goal:** Confirm whether the failure path exists in the codebase as it stands today, or whether
code inspection refutes the claim.

**Steps:**

1. Invoke the `static-validator` mode (or use `read` tools in the current mode).

2. Locate the relevant source files using `grep` and `FindSymbol`:
   - Search for the function, class, or module referenced in the issue.
   - Read the symbol body with `read_file` or `FindSymbol` with `include_body: true`.

3. Trace the call path from entry point to the alleged failure site:
   - Use `FindReferencingSymbols` to discover callers.
   - Verify argument types, guard conditions, and null checks along the path.

4. Check type annotations, default values, and configuration schemas for any invariants that
   would prevent the failure.

5. Check version-gated code paths: does the failure require a specific runtime version,
   environment variable, or feature flag that the issue reporter may or may not have?

6. **Decision after Tier 2:**
   - If inspection proves the failure path cannot be reached under any valid inputs →
     **verdict `FALSE_POSITIVE`**, cite the exact file path and line range.
   - If the failure path demonstrably exists and is reachable → **verdict `VALID`**, cite the
     file path and line number of the failure site.
   - If evidence is ambiguous (60 % or more confidence in either direction) → default to
     **`VALID`** with `confidence` set accordingly, so the defect is not silently dismissed.

---

### Tier 3 — Synthesis and Verdict Output

**Goal:** Aggregate findings from Tiers 1 and 2 into a single, machine-readable verdict file.

**Steps:**

1. Collect all evidence strings from both tiers.

2. Construct the verdict object. Every field is mandatory:

   ```json
   {
     "status": "<VALID|DUPLICATE|OBSOLETE|FALSE_POSITIVE>",
     "citation": "<evidence string — commit SHA, issue URL, or file:line>",
     "rationale": "<two to five sentences explaining the verdict>",
     "confidence": 0.95,
     "timestamp": "<ISO 8601 UTC — e.g. 2025-07-15T10:30:00Z>",
     "target_repo": "<owner/repo or remote URL>",
     "issue_reference": "<issue URL, ticket ID, or descriptor>"
   }
   ```

3. Validate the object against `.bob/verdict.schema.json` before writing:
   - All required fields must be present.
   - `status` must be exactly one of the four enum values.
   - `confidence` must be a float between 0.0 and 1.0.
   - `timestamp` must be a valid ISO 8601 date-time string.

4. Write the verdict to `.bob/verdict.json` using `write_file`. This is the **only** file
   write this skill performs.

5. Print a summary table to the chat:

   ```
   ┌─────────────────────────────────────────────────────────┐
   │  NETRANI TRIAGE VERDICT                                 │
   ├─────────────────────────────────────────────────────────┤
   │  Status     : <status>                                  │
   │  Confidence : <0.0–1.0>                                 │
   │  Citation   : <citation>                                │
   │  Issue      : <issue_reference>                         │
   │  Repo       : <target_repo>                             │
   ├─────────────────────────────────────────────────────────┤
   │  Rationale  : <rationale (wrapped)>                     │
   └─────────────────────────────────────────────────────────┘
   ```

6. **Post-verdict routing:**
   - `VALID` → Inform the user that `surgical-fixer` may now be invoked to draft a fix.
   - `DUPLICATE` / `OBSOLETE` / `FALSE_POSITIVE` → Explain the finding and close the triage.
     Do not draft any code.

---

## General-Purpose Runtime Discovery Rules

These rules ensure Netrani works across any repository without hardcoded assumptions.

1. **Discover test commands dynamically.** Before running any test or lint command, inspect the
   repository root for:
   - `CONTRIBUTING.md` or `DEVELOPING.md` — look for "testing", "running tests", "linting"
     sections.
   - `Makefile` — extract relevant `make` targets (`test`, `lint`, `check`, `verify`).
   - `package.json` — read `.scripts` for `test`, `lint`, `typecheck`.
   - `pyproject.toml` / `setup.cfg` / `tox.ini` — extract test runner and lint commands.
   - `Cargo.toml` — `cargo test`, `cargo clippy`.
   - `go.mod` — `go test ./...`, `golangci-lint run`.

2. **Never assume a specific test framework.** Do not hardcode `pytest`, `jest`, `mocha`, `go
   test`, or any other tool unless you have read it from a project manifest.

3. **Respect CI configuration.** If `.github/workflows/`, `.circleci/`, or `.gitlab-ci.yml`
   exists, read those files to understand what the project itself considers the canonical
   verification suite.

4. **Scope test execution to the affected package.** When running tests after a fix, prefer
   targeted execution (e.g. the specific package or directory containing the changed file)
   over running the full suite, unless the project manifest indicates otherwise.

5. **Report all discovered commands to the user** before executing them. Do not run commands
   silently.

---

## Important Constraints

- **Do not write any source file during the Explore phase.** The only permitted write during
  triage is `.bob/verdict.json`.
- **Do not invoke `surgical-fixer` from within this skill.** Routing to the fix phase is the
  user's or orchestrator's decision after reading the verdict summary.
- **Do not speculate.** Every claim in `rationale` must be grounded in code or history
  evidence found during Tiers 1 and 2. If evidence is insufficient, lower `confidence` and
  state the gap explicitly.
- **Confidence calibration:**
  - 0.95–1.00 — Direct evidence (commit SHA, exact line match).
  - 0.75–0.94 — Strong circumstantial evidence (type invariant, guarded code path).
  - 0.50–0.74 — Ambiguous; rationale must explain the uncertainty.
  - Below 0.50 — Do not emit a verdict; request more information from the user.
