#!/usr/bin/env bash
# cron_nudge.sh — Pure bash + osascript anti-pause system
# Uses file modification time (no timezone issues) to detect stale heartbeat.
# No Python, no Ruby, no LaunchAgents. Just cron + bash + osascript.

set -euo pipefail

STATUS_FILE="/Users/sdevisch/.claude/autopilot/status.json"
LOG_FILE="/Users/sdevisch/.claude/watchdogs/cron_nudge.log"
STALE_THRESHOLD=90
HARD_THRESHOLD=180

log() {
    local now
    now="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$now] $*" >> "$LOG_FILE"
}

# --- Guard: status file must exist ---
if [[ ! -f "$STATUS_FILE" ]]; then
    exit 0
fi

# --- Use file mtime to avoid timezone parsing issues ---
file_mtime=$(stat -f %m "$STATUS_FILE" 2>/dev/null) || exit 0
now_epoch=$(date +%s)
age=$(( now_epoch - file_mtime ))

# Sanity: negative = clock issue
if (( age < 0 )); then
    exit 0
fi

# Below threshold: nothing to do
if (( age < STALE_THRESHOLD )); then
    exit 0
fi

# --- Check if Terminal.app is running ---
if ! osascript -e 'tell application "System Events" to (name of processes) contains "Terminal"' 2>/dev/null | grep -q "true"; then
    log "STALE ${age}s but Terminal not running"
    exit 0
fi

# --- Soft nudge: 90-179s ---
if (( age >= STALE_THRESHOLD && age < HARD_THRESHOLD )); then
    log "SOFT NUDGE: ${age}s stale — Enter"
    osascript -e '
        tell application "Terminal" to activate
        delay 0.3
        tell application "System Events" to tell process "Terminal" to keystroke return
    ' 2>/dev/null || true
    exit 0
fi

# --- Hard nudge: 180s+ ---
if (( age >= HARD_THRESHOLD )); then
    log "HARD NUDGE: ${age}s stale — continue message"
    osascript -e '
        tell application "Terminal" to activate
        delay 0.3
        tell application "System Events" to tell process "Terminal"
            keystroke "Please continue working on the current task without stopping."
            delay 0.2
            keystroke return
        end tell
    ' 2>/dev/null || true
    exit 0
fi
