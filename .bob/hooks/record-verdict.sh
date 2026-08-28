#!/usr/bin/env bash
# =============================================================================
# record-verdict.sh  —  Netrani PostToolUse Sensor
#
# Purpose : Append a structured JSON audit entry to .bob/audit.log after
#           every execute_command tool call.  Captures timestamp, the command
#           that was run, the exit code reported by Bob, and basic sensor
#           metrics (log sequence number).
#
# This script ALWAYS exits 0 — it must never disrupt the tool's output.
# =============================================================================

AUDIT_LOG=".bob/audit.log"

# ── Ensure the audit log directory exists ────────────────────────────────────
mkdir -p "$(dirname "$AUDIT_LOG")"

# ── Read the JSON input Bob provides via stdin (PostToolUse payload) ─────────
# Bob pipes a JSON object describing the just-executed tool call to stdin.
# Structure (best-effort — schema may vary across Bob versions):
#   { "tool": "execute_command", "input": { "command": "..." }, "output": { "exitCode": 0, ... } }
RAW_INPUT=""
if [ -t 0 ]; then
  # stdin is a terminal (manual invocation / no piped input) — use placeholder
  RAW_INPUT="{}"
else
  RAW_INPUT=$(cat)
fi

# ── Extract fields (jq preferred; grep fallback) ─────────────────────────────
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

if command -v jq &>/dev/null; then
  COMMAND=$(echo "$RAW_INPUT"   | jq -r '.input.command  // .command  // "unknown"' 2>/dev/null || echo "unknown")
  EXIT_CODE=$(echo "$RAW_INPUT" | jq -r '.output.exitCode // .exitCode // "unknown"' 2>/dev/null || echo "unknown")
  TOOL_NAME=$(echo "$RAW_INPUT" | jq -r '.tool // "execute_command"' 2>/dev/null || echo "execute_command")
else
  COMMAND=$(echo  "$RAW_INPUT" | grep -o '"command"\s*:\s*"[^"]*"'  | sed 's/.*"command"\s*:\s*"\([^"]*\)".*/\1/'  | head -n1 || echo "unknown")
  EXIT_CODE=$(echo "$RAW_INPUT" | grep -o '"exitCode"\s*:\s*[0-9]*'  | sed 's/.*"exitCode"\s*:\s*\([0-9]*\).*/\1/'  | head -n1 || echo "unknown")
  TOOL_NAME="execute_command"
fi

# ── Compute log sequence number ───────────────────────────────────────────────
SEQ=1
if [[ -f "$AUDIT_LOG" ]]; then
  SEQ=$(( $(grep -c '"seq"' "$AUDIT_LOG" 2>/dev/null || echo 0) + 1 ))
fi

# ── Escape values for safe JSON embedding ─────────────────────────────────────
# Replace backslashes, then double-quotes in COMMAND to keep JSON valid.
COMMAND_SAFE=$(printf '%s' "$COMMAND" | sed 's/\\/\\\\/g; s/"/\\"/g')

# ── Append the JSON log entry ─────────────────────────────────────────────────
cat >> "$AUDIT_LOG" <<EOF
{"seq":${SEQ},"timestamp":"${TIMESTAMP}","tool":"${TOOL_NAME}","command":"${COMMAND_SAFE}","exit_code":"${EXIT_CODE}","sensor":"record-verdict"}
EOF

# ── Always succeed — PostToolUse sensors must not block ──────────────────────
exit 0
