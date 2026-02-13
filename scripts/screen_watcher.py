#!/usr/bin/env python3
"""screen_watcher.py — Ultra-fast anti-pause via terminal screen monitoring.

Monitors Terminal.app window contents for changes. If content stops changing
for STALE_THRESHOLD seconds, sends recovery input via direct PTY write.

NO AppleScript keystroke permissions needed. Reads screen via osascript
(read-only, always allowed) and writes via /dev/ttysXXX (process owner, no
special permissions needed).

Target: 10-second recovery time. Tests show 5-second is achievable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config — tune these for recovery speed
# ---------------------------------------------------------------------------

CHECK_INTERVAL = 2.0      # How often to check screen content (seconds)
STALE_THRESHOLD = 10.0    # Seconds of no change before intervention
COOLDOWN = 30.0           # Min seconds between interventions (was 15)
MAX_INTERVENTIONS_HOUR = 30  # Safety cap
ESCALATION_THRESHOLD = 3  # After N soft nudges, escalate to hard nudge

# States that should trigger nudges. "waiting" is excluded because it means
# Claude asked a question and is legitimately waiting for user input.
NUDGEABLE_STATES = {"idle", "unknown", "error"}

# Max nudges per window before backing off (give up for BACKOFF_DURATION)
MAX_NUDGES_PER_WINDOW = 5
BACKOFF_DURATION = 300.0  # 5 minutes

STATUS_FILE = Path.home() / ".claude" / "watchdogs" / "screen_watcher_status.json"
LOG_FILE = Path.home() / ".claude" / "watchdogs" / "screen_watcher.log"

# Messages to cycle through on escalation
SOFT_NUDGE = ""  # Just Enter key
HARD_NUDGES = [
    "continue",
    "Please continue working on the current task without stopping.",
    "You paused. Keep working autonomously. Do not ask questions.",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def write_status(state: str, **extra):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "state": state,
        "pid": os.getpid(),
        **extra,
    }
    try:
        STATUS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Screen reading — osascript (read-only, no permissions needed)
# ---------------------------------------------------------------------------

def get_window_contents() -> dict[int, str]:
    """Read all Terminal.app window contents. Returns {window_idx: content}."""
    try:
        result = subprocess.run(
            ["osascript", "-e", """
            set output to ""
            tell application "Terminal"
                set winCount to count of windows
                repeat with i from 1 to winCount
                    try
                        set tabContent to contents of selected tab of window i
                        set output to output & "---WINDOW" & i & "---" & return & tabContent & return
                    end try
                end repeat
            end tell
            return output
            """],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return {}

        windows = {}
        parts = result.stdout.split("---WINDOW")
        for part in parts[1:]:  # Skip first empty part
            lines = part.split("\n", 1)
            try:
                idx = int(lines[0].replace("---", "").strip())
                content = lines[1] if len(lines) > 1 else ""
                windows[idx] = content
            except (ValueError, IndexError):
                continue
        return windows
    except Exception:
        return {}


def content_hash(text: str) -> str:
    """Hash the last ~2000 chars of content for change detection."""
    # Focus on the tail — that's where new output appears
    tail = text[-2000:] if len(text) > 2000 else text
    return hashlib.md5(tail.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Claude session detection
# ---------------------------------------------------------------------------

def find_claude_ttys() -> list[str]:
    """Find TTY devices associated with Claude processes."""
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,tty,comm"],
            capture_output=True, text=True, timeout=5,
        )
        ttys = []
        for line in result.stdout.splitlines():
            if "claude" in line.lower() and "ttys" in line:
                parts = line.split()
                if len(parts) >= 2:
                    tty = parts[1]
                    if tty.startswith("ttys"):
                        dev = f"/dev/{tty}"
                        if os.path.exists(dev):
                            ttys.append(dev)
        return list(set(ttys))
    except Exception:
        return []


def is_claude_window(content: str) -> bool:
    """Check if a window contains a Claude Code session."""
    if not content:
        return False
    # Look for Claude-specific markers in the content
    markers = ["claude", "Claude", "\u23f5", "Co-Authored-By", "anthropic",
               "Tool", "Read(", "Write(", "Bash(", "Edit(", "Glob(", "Grep("]
    return any(m in content for m in markers)


def detect_claude_state(content: str) -> str:
    """Analyze terminal content to determine Claude's state.

    Returns: 'working', 'waiting', 'idle', 'error', 'not_claude', 'unknown'
    """
    if not content:
        return "unknown"

    if not is_claude_window(content):
        return "not_claude"

    # Get last 20 lines
    lines = [l.strip() for l in content.strip().splitlines() if l.strip()]
    tail = lines[-20:] if len(lines) > 20 else lines
    tail_text = "\n".join(tail)

    # Actively working: spinner, tool calls, file operations
    if any(x in tail_text for x in ["\u280b", "\u2819", "\u2839", "\u2838", "\u283c", "\u2834", "\u2826", "\u2827", "\u2807", "\u280f"]):
        return "working"
    if re.search(r"(Reading|Writing|Editing|Running|Searching)", tail_text):
        return "working"
    if "Bash" in tail_text and ("$" in tail_text or "command" in tail_text.lower()):
        return "working"

    # Waiting for user input (Claude asked a question)
    if re.search(r"(Would you like|Do you want|Should I|Shall I|want me to)", tail_text, re.I):
        return "waiting"

    # Claude prompt waiting for input
    if re.search(r"\u23f5\s*$", tail_text):
        return "idle"
    if re.search(r">\s*$", tail_text):
        return "waiting"

    # Error state
    if re.search(r"(Error|error|FAILED|failed|Traceback|panic)", tail_text):
        return "error"

    return "unknown"


# ---------------------------------------------------------------------------
# Input injection — direct PTY write (no permissions needed)
# ---------------------------------------------------------------------------

def send_to_tty(tty_path: str, text: str):
    """Write text directly to a TTY device.

    This works because we're the same user who owns the TTY.
    No accessibility permissions needed — it's a file write.
    """
    try:
        with open(tty_path, "w") as f:
            f.write(text + "\n")
        return True
    except PermissionError:
        log(f"Permission denied writing to {tty_path}")
        return False
    except Exception as e:
        log(f"TTY write error ({tty_path}): {e}")
        return False


def send_to_all_claude_ttys(text: str) -> int:
    """Send text to all Claude TTY sessions. Returns count of successful sends."""
    ttys = find_claude_ttys()
    if not ttys:
        log("No Claude TTYs found")
        return 0

    sent = 0
    for tty in ttys:
        if send_to_tty(tty, text):
            sent += 1
            log(f"Sent to {tty}: {repr(text[:50])}")
    return sent


def send_to_single_tty(text: str) -> bool:
    """Send text to a single Claude TTY (the first one found).

    Used for targeted nudging to avoid spamming all sessions.
    Falls back to osascript if no TTY found.
    """
    ttys = find_claude_ttys()
    if not ttys:
        log("No Claude TTYs found for targeted send")
        return False

    # Send to only the first TTY
    tty = ttys[0]
    if send_to_tty(tty, text):
        log(f"Targeted send to {tty}: {repr(text[:50])}")
        return True
    return False


# Fallback: osascript keystroke (may fail with permission errors)
def send_via_osascript(text: str) -> bool:
    """Fallback: send via AppleScript. May fail with error 1002."""
    try:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        result = subprocess.run(
            ["osascript", "-e", f"""
            tell application "System Events"
                tell process "Terminal"
                    set frontmost to true
                    delay 0.1
                    keystroke "{escaped}"
                    delay 0.1
                    keystroke return
                end tell
            end tell
            """],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def nudge(text: str) -> bool:
    """Send nudge via best available method.

    Uses targeted single-TTY send instead of broadcasting to all TTYs.
    """
    # Try targeted single TTY write first (avoids spamming healthy sessions)
    if send_to_single_tty(text):
        return True

    # Fallback to osascript
    log("Falling back to osascript")
    return send_via_osascript(text)


# ---------------------------------------------------------------------------
# Main watcher loop
# ---------------------------------------------------------------------------

def watch_loop():
    """Main screen watching loop."""
    log("Screen watcher started")
    log(f"Config: check={CHECK_INTERVAL}s, stale={STALE_THRESHOLD}s, cooldown={COOLDOWN}s")
    log(f"Nudgeable states: {NUDGEABLE_STATES}")
    log(f"Max nudges per window before backoff: {MAX_NUDGES_PER_WINDOW} (backoff={BACKOFF_DURATION}s)")
    write_status("starting")

    # State tracking per window
    window_hashes: dict[int, str] = {}
    window_last_change: dict[int, float] = {}

    # Per-window nudge tracking to prevent feedback loops
    # Tracks nudge count per window, and the hash recorded just before nudging
    window_nudge_count: dict[int, int] = {}        # win_idx -> nudge count
    window_pre_nudge_hash: dict[int, str] = {}     # win_idx -> hash before nudge
    window_backoff_until: dict[int, float] = {}     # win_idx -> monotonic time to resume

    last_intervention = 0.0
    interventions_this_hour = 0
    hour_start = time.monotonic()
    total_interventions = 0
    total_recoveries = 0
    total_false_positives = 0

    while True:
        try:
            now = time.monotonic()

            # Reset hourly counter
            if now - hour_start > 3600:
                interventions_this_hour = 0
                hour_start = now

            # Read all terminal windows
            windows = get_window_contents()
            if not windows:
                write_status("no_windows")
                time.sleep(CHECK_INTERVAL)
                continue

            stale_windows = []
            active_windows = []

            for idx, content in windows.items():
                h = content_hash(content)

                if idx not in window_hashes:
                    # First time seeing this window
                    window_hashes[idx] = h
                    window_last_change[idx] = now
                    continue

                if h != window_hashes[idx]:
                    # Content changed — but is it a genuine change or just our nudge echoing?
                    pre_nudge_h = window_pre_nudge_hash.get(idx)
                    if pre_nudge_h is not None and pre_nudge_h != h:
                        # We had a pre-nudge hash recorded. The content changed from
                        # before-nudge to something new. But we need to check if this
                        # is genuinely different work or just the nudge echo.
                        # Clear the pre-nudge hash — we've consumed it.
                        del window_pre_nudge_hash[idx]
                        # The nudge itself would have changed content (echo of newline).
                        # A *genuine* recovery means Claude started producing real output,
                        # which would change the hash again on the NEXT check cycle.
                        # For now, just update the hash but do NOT reset nudge count.
                        # The nudge count only resets on a second consecutive change
                        # (indicating real work is happening).
                        window_hashes[idx] = h
                        window_last_change[idx] = now
                        active_windows.append(idx)
                    else:
                        # No pending pre-nudge hash — this is an organic content change.
                        # This means genuine work is happening. Reset nudge count.
                        window_hashes[idx] = h
                        window_last_change[idx] = now
                        if idx in window_nudge_count:
                            window_nudge_count[idx] = 0
                        active_windows.append(idx)
                else:
                    # Content unchanged — check how long
                    stale_secs = now - window_last_change.get(idx, now)
                    state = detect_claude_state(content)

                    if (stale_secs >= STALE_THRESHOLD
                        and state in NUDGEABLE_STATES):
                        # Check if this window is in backoff
                        if now < window_backoff_until.get(idx, 0):
                            remaining = window_backoff_until[idx] - now
                            # Only log occasionally to avoid spam
                            if int(stale_secs) % 60 == 0:
                                log(f"win#{idx} in backoff ({remaining:.0f}s remaining), skipping")
                        else:
                            stale_windows.append((idx, stale_secs, state))

            # Write status
            write_status(
                "watching",
                windows=len(windows),
                active=len(active_windows),
                stale=len(stale_windows),
                interventions=total_interventions,
                recoveries=total_recoveries,
                false_positives=total_false_positives,
                backed_off_windows=sum(1 for idx in window_backoff_until
                                       if now < window_backoff_until[idx]),
            )

            # Intervene on stale windows
            if stale_windows and (now - last_intervention) >= COOLDOWN:
                if interventions_this_hour < MAX_INTERVENTIONS_HOUR:
                    for idx, stale_secs, state in stale_windows:
                        nudge_count = window_nudge_count.get(idx, 0)

                        # Check if we've exceeded max nudges for this window
                        if nudge_count >= MAX_NUDGES_PER_WINDOW:
                            window_backoff_until[idx] = now + BACKOFF_DURATION
                            window_nudge_count[idx] = 0  # Reset for after backoff
                            log(f"win#{idx} hit {MAX_NUDGES_PER_WINDOW} nudges without genuine recovery, "
                                f"backing off for {BACKOFF_DURATION:.0f}s")
                            continue

                        # Choose nudge level based on per-window escalation
                        if nudge_count < ESCALATION_THRESHOLD:
                            msg = SOFT_NUDGE  # Just Enter
                            nudge_type = "soft"
                        else:
                            msg_idx = min(nudge_count - ESCALATION_THRESHOLD, len(HARD_NUDGES) - 1)
                            msg = HARD_NUDGES[msg_idx]
                            nudge_type = "hard"

                        log(f"STALE win#{idx} ({stale_secs:.0f}s, state={state}, "
                            f"nudge_count={nudge_count}) -> {nudge_type} nudge")

                        # Record the hash BEFORE nudging so we can detect false recoveries
                        window_pre_nudge_hash[idx] = window_hashes.get(idx, "")

                        if nudge(msg):
                            total_interventions += 1
                            interventions_this_hour += 1
                            window_nudge_count[idx] = nudge_count + 1
                            last_intervention = now
                            log(f"Nudge sent ({nudge_type}): {repr(msg[:60])}")

                            # Track recovery: check in 5 seconds if content genuinely changed
                            time.sleep(5)
                            new_windows = get_window_contents()
                            if idx in new_windows:
                                new_h = content_hash(new_windows[idx])
                                old_h = window_hashes.get(idx)
                                pre_h = window_pre_nudge_hash.get(idx)

                                if new_h != old_h:
                                    # Content changed after nudge. Update hash.
                                    window_hashes[idx] = new_h
                                    window_last_change[idx] = time.monotonic()

                                    # Check the NEW state to see if Claude is actually working now
                                    new_state = detect_claude_state(new_windows[idx])
                                    if new_state == "working":
                                        # Genuine recovery — Claude started working
                                        total_recoveries += 1
                                        window_nudge_count[idx] = 0
                                        if idx in window_pre_nudge_hash:
                                            del window_pre_nudge_hash[idx]
                                        recovery_time = stale_secs + 5
                                        log(f"RECOVERED in ~{recovery_time:.0f}s (state now: {new_state})")
                                    else:
                                        # Content changed but Claude not working — likely just nudge echo
                                        total_false_positives += 1
                                        log(f"Content changed but state={new_state} (likely nudge echo, "
                                            f"not genuine recovery). nudge_count={window_nudge_count[idx]}")
                                else:
                                    log(f"No content change after nudge for win#{idx}")
                        break  # One nudge per cycle

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"Error: {e}")
            time.sleep(CHECK_INTERVAL)

    write_status("stopped", total_interventions=total_interventions)
    log("Screen watcher stopped")


# ---------------------------------------------------------------------------
# Simulation / testing
# ---------------------------------------------------------------------------

def simulate_test():
    """Test the screen watcher with simulated stalls."""
    print("Screen Watcher — Simulation Test")
    print()

    # 1. Screen reading
    print("1. Reading Terminal windows...", end="", flush=True)
    t0 = time.monotonic()
    windows = get_window_contents()
    elapsed = time.monotonic() - t0
    print(f" [{elapsed:.2f}s] Found {len(windows)} windows")
    for idx, content in windows.items():
        state = detect_claude_state(content)
        print(f"   Window {idx}: {len(content)} chars, state={state}")

    # 2. Claude TTY detection
    print("\n2. Finding Claude TTYs...", end="", flush=True)
    ttys = find_claude_ttys()
    print(f" Found: {ttys}")

    # 3. Hash speed test
    print("\n3. Hash speed test...", end="", flush=True)
    if windows:
        content = list(windows.values())[0]
        t0 = time.monotonic()
        for _ in range(1000):
            content_hash(content)
        elapsed = time.monotonic() - t0
        print(f" 1000 hashes in {elapsed:.3f}s ({elapsed/1000*1000:.2f}ms each)")

    # 4. Screen read latency
    print("\n4. Screen read latency (10 reads)...", end="", flush=True)
    times = []
    for _ in range(10):
        t0 = time.monotonic()
        get_window_contents()
        times.append(time.monotonic() - t0)
    avg = sum(times) / len(times)
    print(f" avg={avg:.3f}s, min={min(times):.3f}s, max={max(times):.3f}s")

    # 5. TTY write test (send empty to test permissions)
    print("\n5. TTY write test...", end="", flush=True)
    if ttys:
        ok = send_to_tty(ttys[0], "")  # Empty string = just newline
        print(f" {'OK' if ok else 'FAILED'}")
    else:
        print(" No TTYs to test")

    # 6. Stale detection simulation
    print("\n6. Stale detection simulation...")
    print(f"   CHECK_INTERVAL = {CHECK_INTERVAL}s")
    print(f"   STALE_THRESHOLD = {STALE_THRESHOLD}s")
    print(f"   COOLDOWN = {COOLDOWN}s")
    print(f"   NUDGEABLE_STATES = {NUDGEABLE_STATES}")
    print(f"   MAX_NUDGES_PER_WINDOW = {MAX_NUDGES_PER_WINDOW}")
    print(f"   BACKOFF_DURATION = {BACKOFF_DURATION}s")
    print(f"   Min detection time = {STALE_THRESHOLD}s (if content stops immediately after check)")
    print(f"   Max detection time = {STALE_THRESHOLD + CHECK_INTERVAL}s (worst case alignment)")
    print(f"   Recovery check = +5s after nudge")
    print(f"   Total worst-case recovery = {STALE_THRESHOLD + CHECK_INTERVAL + 5}s")

    # 7. Minimum possible recovery time calculation
    print("\n7. Minimum recovery time analysis:")
    for interval in [0.5, 1.0, 2.0, 3.0, 5.0]:
        for threshold in [3, 5, 8, 10, 15]:
            worst = threshold + interval + 5
            best = threshold + 5
            if interval <= 2.0 and threshold <= 10:
                print(f"   interval={interval}s, threshold={threshold}s -> best={best}s, worst={worst}s")

    print()
    print("Recommendation: interval=1s, threshold=5s -> 10-11s recovery")
    print("Aggressive:     interval=0.5s, threshold=3s -> 8-8.5s recovery")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Screen watcher anti-pause daemon")
    parser.add_argument("--test", action="store_true", help="Run simulation test")
    parser.add_argument("--interval", type=float, default=CHECK_INTERVAL, help="Check interval (seconds)")
    parser.add_argument("--threshold", type=float, default=STALE_THRESHOLD, help="Stale threshold (seconds)")
    args = parser.parse_args()

    if args.test:
        simulate_test()
    else:
        CHECK_INTERVAL = args.interval
        STALE_THRESHOLD = args.threshold
        watch_loop()
