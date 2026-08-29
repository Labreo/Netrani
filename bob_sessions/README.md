# IBM Bob IDE Task Session Summaries

Per the **IBM TechXchange 2026 Dev Day Hackathon Guide** (pages 4, 6, 17–18), all participants are required to upload screenshots of their **Bob IDE Task Session Consumption Summaries** to the `bob_sessions/` folder in their code repository as evidence of Bob usage.

---

## How to Capture Consumption Summaries in Bob IDE

Follow these steps in **Bob IDE** (instance: `ibm-coding-challenge-uat`, region: `us-east`):

1. **Open Task List:** In the Bob IDE chat interface, click **Tasks** (or the task history icon) to view recent agent tasks.
2. **Select Target Task:** Select a task from the list related to Netrani's development or execution.
3. **Open Consumption Summary:** Click on the **Task Header** (the title banner at the top of the chat panel). This opens the **Task Session Consumption Summary** overlay.
4. **Capture Screenshot:** Take a clean screenshot in **PNG** format showing the full consumption summary popup (including **Context Length**, **Task Id**, **Workspace**, **Tokens**, **Cache**, and **API Cost / Bobcoins**).
5. **Save to `bob_sessions/`:** Save the PNG screenshot in this directory using the required naming convention.

---

## File Naming Convention

```
<author_or_team>_task<NN>_<short_description>_summary.png
```

**Author Prefix:** `kanak_waradkar` (or `labreo`)  
**Examples:**
- `kanak_waradkar_task01_history_miner_summary.png`
- `kanak_waradkar_task02_static_validator_summary.png`
- `kanak_waradkar_task03_gated_fixer_summary.png`
- `kanak_waradkar_task04_e2e_pipeline_summary.png`
- `kanak_waradkar_task05_pr_emission_summary.png`

---

## Planned Session Inventory Checklist

| File Name | Target Workflow | Status |
|---|---|---|
| `kanak_waradkar_task01_history_miner_summary.png` | Subagent 1 (`history-miner`) Git archaeology execution | ⬜ Pending Capture |
| `kanak_waradkar_task02_static_validator_summary.png` | Subagent 2 (`static-validator`) AST invariant analysis | ⬜ Pending Capture |
| `kanak_waradkar_task03_gated_fixer_summary.png` | Subagent 3 (`surgical-fixer`) + `gate-fix.sh` PreToolUse hook | ⬜ Pending Capture |
| `kanak_waradkar_task04_e2e_pipeline_summary.png` | 8-Stage Pipeline Orchestration (`netrani run`) | ⬜ Pending Capture |
| `kanak_waradkar_task05_pr_emission_summary.png` | Disclosed PR creation (`gh pr create` with `Assisted-by:` trailer) | ⬜ Pending Capture |

---

## Consumption Summary UI Example

*(Refer to Hackathon Guide page 18 for reference layout)*:

```
┌───────────────────────────────────────────────────────────┐
│ Task                                                    ✕ │
│ Ingest issue #973 and execute static validator            │
├───────────────────────────────────────────────────────────┤
│ Context Length   15.1k / 270.0k (6%)                      │
│ Task Id          9a07cd132962251dea4b504b34979063         │
│ Workspace        Netrani                                  │
│ Tokens           ↑ 36.2k  ↓ 1.2k                          │
│ Cache            ↑ 14.6k  ↓ 21.6k                         │
│ API Cost         0.093 Bobcoins                           │
└───────────────────────────────────────────────────────────┘
```
