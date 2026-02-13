#!/usr/bin/env bash
# heartbeat_guard.sh — Lightweight background monitor for Claude autopilot
# Runs as a macOS Login Item. No Python, no cron, no LaunchAgent.
# Watches status.json staleness and nudges Terminal when Claude stalls.

set -uo pipefail

# --- Configuration ---
STATUS_FILE="$HOME/.claude/autopilot/status.json"
LOG_FILE="$HOME/.claude/watchdogs/heartbeat_guard.log"
STATUS_JSON="$HOME/.claude/watchdogs/heartbeat_guard_status.json"
CHECK_INTERVAL=20
STALE_NUDGE=100
STALE_ESCALATE=200
MAX_NUDGES_PER_HOUR=20

# --- State ---
nudge_count=0
nudge_window_start=$(date +%s)
total_nudges=0
total_checks=0
started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
started_epoch=$(date +%s)

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

write_status() {
    local now
    now=$(date +%s)
    cat > "$STATUS_JSON" <<EOJSON
{
  "pid": $$,
  "started_at": "$started_at",
  "last_check": "$(date -u +'%Y-%m-%dT%H:%M:%SZ')",
  "total_checks": $total_checks,
  "total_nudges": $total_nudges,
  "nudges_this_window": $nudge_count,
  "status": "$1"
}
EOJSON
}

reset_nudge_window_if_needed() {
    local now
    now=$(date +%s)
    local elapsed=$((now - nudge_window_start))
    if [ "$elapsed" -ge 3600 ]; then
        nudge_count=0
        nudge_window_start=$now
    fi
}

terminal_is_active() {
    pgrep -x "Terminal" >/dev/null 2>&1
}

nudge_enter() {
    osascript -e '
        tell application "Terminal" to activate
        delay 0.3
        tell application "System Events"
            keystroke return
        end tell
    ' >/dev/null 2>&1
}

nudge_continue() {
    osascript -e '
        tell application "Terminal" to activate
        delay 0.3
        tell application "System Events"
            keystroke "continue working autonomously"
            delay 0.1
            keystroke return
        end tell
    ' >/dev/null 2>&1
}

# --- Startup ---
mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$STATUS_JSON")" "$(dirname "$STATUS_FILE")"
log "STARTED pid=$$ interval=${CHECK_INTERVAL}s nudge=${STALE_NUDGE}s escalate=${STALE_ESCALATE}s"
write_status "starting"

# Ensure status file exists so stat does not fail on first run
if [ ! -f "$STATUS_FILE" ]; then
    echo '{"status":"unknown","ts":"'"$(date -u +'%Y-%m-%dT%H:%M:%SZ')"'"}' > "$STATUS_FILE"
    log "Created initial status file"
fi

# --- Main Loop ---
while true; do
    total_checks=$((total_checks + 1))
    reset_nudge_window_if_needed

    now=$(date +%s)

    # Get file modification time (BSD stat on macOS)
    file_mtime=$(stat -f %m "$STATUS_FILE" 2>/dev/null || echo "0")
    staleness=$((now - file_mtime))

    if [ "$staleness" -ge "$STALE_ESCALATE" ] && terminal_is_active; then
        if [ "$nudge_count" -lt "$MAX_NUDGES_PER_HOUR" ]; then
            nudge_continue
            nudge_count=$((nudge_count + 1))
            total_nudges=$((total_nudges + 1))
            log "ESCALATE staleness=${staleness}s nudges_this_hr=${nudge_count} typed='continue working autonomously'"
            write_status "escalated"
        else
            log "THROTTLED staleness=${staleness}s nudge_count=${nudge_count} (max ${MAX_NUDGES_PER_HOUR}/hr)"
            write_status "throttled"
        fi
    elif [ "$staleness" -ge "$STALE_NUDGE" ] && terminal_is_active; then
        if [ "$nudge_count" -lt "$MAX_NUDGES_PER_HOUR" ]; then
            nudge_enter
            nudge_count=$((nudge_count + 1))
            total_nudges=$((total_nudges + 1))
            log "NUDGE staleness=${staleness}s nudges_this_hr=${nudge_count} sent=Enter"
            write_status "nudged"
        else
            log "THROTTLED staleness=${staleness}s nudge_count=${nudge_count} (max ${MAX_NUDGES_PER_HOUR}/hr)"
            write_status "throttled"
        fi
    else
        write_status "watching"
    fi

    sleep "$CHECK_INTERVAL"
done
