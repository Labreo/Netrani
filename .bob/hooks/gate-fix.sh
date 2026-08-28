#!/usr/bin/env bash
# =============================================================================
# gate-fix.sh  —  Netrani PreToolUse Guard (Bob exit-code 2 = block tool)
#
# Purpose : Enforce the "Verify before acting" principle.
#           Blocks write_file / apply_diff / edit_file unless .bob/verdict.json
#           exists and contains "status": "VALID".
#
# Exit codes:
#   0  — Verdict is VALID; allow the file-modification tool to proceed.
#   2  — Block the tool (Bob Outer Harness blocking code).
# =============================================================================

set -euo pipefail

VERDICT_FILE=".bob/verdict.json"

# ── Helper: print a boxed error and exit 2 ───────────────────────────────────
block() {
  local reason="$1"
  echo ""
  echo "╔══════════════════════════════════════════════════════════════════════╗"
  echo "║  [GATE-FIX] CODE MODIFICATION BLOCKED                               ║"
  echo "╠══════════════════════════════════════════════════════════════════════╣"
  printf  "║  Reason : %-58s ║\n" "$reason"
  echo "║                                                                      ║"
  echo "║  Netrani requires a VALID triage verdict before any file edit.       ║"
  echo "║  Run the triage skill first:  Netrani triage <issue-reference>       ║"
  echo "║  Then ensure .bob/verdict.json contains  \"status\": \"VALID\"           ║"
  echo "╚══════════════════════════════════════════════════════════════════════╝"
  echo ""
  exit 2
}

# ── 1. Check that verdict file exists ────────────────────────────────────────
if [[ ! -f "$VERDICT_FILE" ]]; then
  block "No verdict file found at $VERDICT_FILE"
fi

# ── 2. Parse the status field ────────────────────────────────────────────────
STATUS=""

if command -v jq &>/dev/null; then
  # Preferred: use jq for robust JSON parsing
  STATUS=$(jq -r '.status // empty' "$VERDICT_FILE" 2>/dev/null || true)
else
  # Fallback: grep-based extraction (handles common formatting variants)
  STATUS=$(grep -o '"status"\s*:\s*"[^"]*"' "$VERDICT_FILE" \
           | sed 's/.*"status"\s*:\s*"\([^"]*\)".*/\1/' \
           | head -n1 || true)
fi

# ── 3. Gate on verdict status ─────────────────────────────────────────────────
case "$STATUS" in
  VALID)
    echo "[GATE-FIX] Verification passed: Verdict is VALID. Allowing modification."
    exit 0
    ;;
  DUPLICATE)
    block "Verdict is DUPLICATE — this issue is already tracked. No fix needed."
    ;;
  OBSOLETE)
    block "Verdict is OBSOLETE — this defect was already resolved in a prior commit."
    ;;
  FALSE_POSITIVE)
    block "Verdict is FALSE_POSITIVE — code inspection proves this failure cannot occur."
    ;;
  "")
    block "Could not read 'status' from $VERDICT_FILE — file may be malformed or empty."
    ;;
  *)
    block "Unknown verdict status '$STATUS' in $VERDICT_FILE — expected VALID/DUPLICATE/OBSOLETE/FALSE_POSITIVE."
    ;;
esac
