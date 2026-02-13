#!/usr/bin/env bash
# stop-guard.sh — BLOCKING Stop hook that prevents premature pausing.
#
# Claude Code Stop hook protocol:
#   - Exit 0 = allow stop
#   - Exit 2 + stderr message = BLOCK stop, feed stderr back to Claude as instructions
#   - JSON stdout {"decision":"block","reason":"..."} = also blocks
#
# This hook reads the stop event from stdin, checks if work appears complete,
# and blocks the stop if there are signs of incomplete work.

set -euo pipefail

COUNTER_FILE="${HOME}/.claude/hooks/.stop_guard_counter"
MAX_CONTINUES=15  # Safety valve: max consecutive blocks before allowing stop
LOG_FILE="${HOME}/.claude/watchdogs/stop_guard.log"

mkdir -p "$(dirname "$COUNTER_FILE")" "$(dirname "$LOG_FILE")"

# Read stop event from stdin
INPUT=$(cat)

# Extract key fields
STOP_HOOK_ACTIVE=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('stop_hook_active', False))
except: print('False')
" 2>/dev/null || echo "False")

# Extract last assistant message
LAST_OUTPUT=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    msgs = d.get('messages', [])
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get('role') == 'assistant':
            content = m.get('content', '')
            if isinstance(content, list):
                content = ' '.join(c.get('text','') for c in content if isinstance(c, dict))
            print(content[-1000:] if len(content) > 1000 else content)
            break
except: print('')
" 2>/dev/null || echo "")

# Read/increment counter
COUNT=0
if [[ -f "$COUNTER_FILE" ]]; then
    COUNT=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
fi

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Safety valve: if we've blocked too many times, allow stop to prevent infinite loop
if (( COUNT >= MAX_CONTINUES )); then
    echo "0" > "$COUNTER_FILE"
    echo "[$ts] ALLOW: safety valve after $COUNT blocks" >> "$LOG_FILE"
    exit 0
fi

# If stop_hook_active is True, Claude is already continuing due to us — be more lenient
if [[ "$STOP_HOOK_ACTIVE" == "True" ]]; then
    # Still check for obvious incomplete work, but with higher threshold
    LENIENCY=true
else
    LENIENCY=false
fi

# Analyze the last output for signs of incomplete work
SHOULD_BLOCK=$(python3 -c "
import re, sys

output = sys.stdin.read().strip()
leniency = '$LENIENCY' == 'true'
reasons = []

# Check for task list references with pending items
if re.search(r'(?i)(pending|in.progress|remaining|TODO|not yet|still need)', output):
    reasons.append('mentions pending/incomplete work')

# Check for 'will do next' / 'next steps' without actually doing them
if re.search(r'(?i)(next step|will (now|then)|let me (now|next)|moving on to)', output):
    reasons.append('describes next steps instead of doing them')

# Very short responses (< 200 chars) that aren't clear completions
if len(output) < 200 and not re.search(r'(?i)(done|complete|finished|all (set|good)|committed|pushed|merged)', output):
    if output and not leniency:
        reasons.append('short response without completion signal')

# Questions instead of actions
if re.search(r'(?i)(would you like|do you want|should I|shall I|want me to|let me know)', output):
    reasons.append('asking permission instead of acting')

# Summary-and-stop pattern
if re.search(r'(?i)(here.s (what|a summary)|in summary|to (recap|summarize))', output) and not re.search(r'(?i)(commit|push|deploy|test.*pass)', output):
    if not leniency:
        reasons.append('summarizing instead of working')

if reasons and not leniency:
    print('BLOCK:' + '; '.join(reasons))
elif reasons and leniency and len(reasons) >= 3:
    # Even with leniency, block if multiple strong signals
    print('BLOCK:' + '; '.join(reasons))
else:
    print('ALLOW')
" <<< "$LAST_OUTPUT" 2>/dev/null || echo "ALLOW")

if [[ "$SHOULD_BLOCK" == BLOCK:* ]]; then
    REASON="${SHOULD_BLOCK#BLOCK:}"
    COUNT=$((COUNT + 1))
    echo "$COUNT" > "$COUNTER_FILE"
    echo "[$ts] BLOCK #$COUNT: $REASON" >> "$LOG_FILE"

    # Exit code 2 = block stop, stderr is fed back to Claude
    echo "You stopped prematurely. $REASON. Continue working on the current task without asking for permission. Do not summarize — take action." >&2
    exit 2
else
    # Reset counter on allowed stop
    echo "0" > "$COUNTER_FILE"
    echo "[$ts] ALLOW: work appears complete (count was $COUNT)" >> "$LOG_FILE"
    exit 0
fi
