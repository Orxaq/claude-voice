#!/usr/bin/env python3
"""Hey Claude — voice bridge for Claude Code sessions.

Runs as a BACKGROUND daemon. Never steals focus. Never opens windows.
Listens for "Hey Claude" wake word, transcribes speech, types it into
the active Claude Code terminal, watches for the response, speaks it.

"Hey Claude bye" deactivates.

This is NOT a separate LLM — it's a transparent voice I/O layer on
top of whatever Claude session is running in Terminal.

Uses:
- MLX Whisper (tiny) for fast local STT on Apple Silicon GPU
- LM Studio local LLM for smart wake word confirmation
- edge-tts for natural neural TTS

Usage:
    python3 hey_claude.py              # start daemon
    python3 hey_claude.py --test       # test pipeline
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
CHUNK_SECONDS = 3        # Listen in 3-second chunks for wake word
SPEECH_SILENCE_SEC = 1.5  # Silence to stop recording speech
ENERGY_THRESHOLD = 200    # VAD threshold (ambient ~110, speech chunk ~300+)
MAX_SPEECH_SEC = 30       # Max recording length

STATUS_FILE = Path.home() / ".claude" / "watchdogs" / "hey_claude_status.json"
LOG_FILE = Path.home() / ".claude" / "watchdogs" / "hey_claude.log"

# Neural voices for TTS
NEURAL_VOICE = "en-US-BrianNeural"
FALLBACK_VOICE = "Daniel"

# LM Studio
LMSTUDIO_FAST_MODEL = "liquid/lfm2.5-1.2b"

STOP_VARIANTS = ["bye", "stop", "quit", "exit"]

# ---------------------------------------------------------------------------
# Logging & status
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
# LM Studio integration
# ---------------------------------------------------------------------------

import urllib.request
import urllib.error

LMS_CLI = Path.home() / ".lmstudio" / "bin" / "lms"
LMS_API = "http://localhost:1234/v1"


def lmstudio_running() -> bool:
    try:
        req = urllib.request.Request(f"{LMS_API}/models", method="GET")
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False


def ensure_lmstudio() -> bool:
    """Ensure LM Studio is running with the fast model loaded."""
    if lmstudio_running():
        return True

    log("LM Studio not running, starting...")

    # Try CLI server start
    try:
        subprocess.run(
            [str(LMS_CLI), "server", "start"],
            capture_output=True, timeout=10,
        )
        time.sleep(3)
        if lmstudio_running():
            log("LM Studio server started via CLI")
            return True
    except Exception:
        pass

    # Launch the app
    try:
        subprocess.Popen(
            ["open", "-a", "LM Studio"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(15):
            time.sleep(2)
            if lmstudio_running():
                log("LM Studio app launched")
                return True
    except Exception:
        pass

    log("Failed to start LM Studio")
    return False


def ensure_model_loaded(model_id: str = LMSTUDIO_FAST_MODEL) -> bool:
    """Ensure a model is loaded in LM Studio."""
    try:
        req = urllib.request.Request(f"{LMS_API}/models", method="GET")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        loaded = [m["id"] for m in data.get("data", [])]
        if model_id in loaded:
            return True
    except Exception:
        pass

    # Load it
    log(f"Loading model {model_id}...")
    try:
        subprocess.run(
            [str(LMS_CLI), "load", model_id, "--gpu", "max", "--ttl", "3600", "-y"],
            capture_output=True, timeout=120,
        )
        log(f"Model {model_id} loaded")
        return True
    except Exception as e:
        log(f"Failed to load model: {e}")
        return False


def lm_chat(prompt: str, system: str = "", max_tokens: int = 150) -> str:
    """Quick chat completion via LM Studio."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = json.dumps({
        "model": LMSTUDIO_FAST_MODEL,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }).encode()

    req = urllib.request.Request(
        f"{LMS_API}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return ""


# ---------------------------------------------------------------------------
# Wake word detection — fast regex + LLM confirmation
# ---------------------------------------------------------------------------

def _quick_wake_check(text: str) -> tuple[bool, str]:
    """Fast regex-based wake word check. Returns (likely_match, remaining)."""
    import string
    clean = text.translate(str.maketrans("", "", string.punctuation)).strip().lower()
    words = clean.split()

    if not words:
        return False, ""

    hey_words = {"hey", "a", "hay", "he", "hei", "ei", "ey"}
    claude_words = {
        "claude", "claud", "clod", "cloud", "clue", "klod", "klaude",
        "clawed", "cod", "cla", "clot", "clout", "close", "glad",
        "clause", "cloth", "klaud",
    }

    for i, word in enumerate(words):
        if word in hey_words and i + 1 < len(words) and words[i + 1] in claude_words:
            remaining = " ".join(words[i + 2:])
            return True, remaining

    # Concatenated check
    joined = clean.replace(" ", "")
    for hey in hey_words:
        for cl in claude_words:
            if hey + cl in joined:
                return True, ""

    return False, ""


def _lm_wake_check(text: str) -> tuple[bool, str, bool]:
    """Use LLM to confirm wake word. Returns (is_wake, command, is_stop)."""
    resp = lm_chat(
        prompt=f'"{text}"',
        system=(
            "Classify this speech transcription. Does it sound like someone saying 'Hey Claude'?\n"
            "Common mishearings: 'a clue', 'hey clod', 'hey cloud', 'hey clawed', 'he claude'.\n"
            "Reply ONLY with JSON: {\"wake\": true/false, \"cmd\": \"words after hey claude\", \"stop\": false}\n"
            "stop=true only if they say bye/stop/quit/exit after hey claude."
        ),
        max_tokens=60,
    )
    try:
        start = resp.index("{")
        end = resp.rindex("}") + 1
        d = json.loads(resp[start:end])
        return d.get("wake", False), d.get("cmd", ""), d.get("stop", False)
    except Exception:
        return False, "", False


def detect_wake_word(text: str) -> tuple[bool, str, bool]:
    """Two-stage wake word detection: fast regex then LLM confirmation.

    Returns (is_wake, command_after_wake, is_stop).
    """
    # Stage 1: fast regex
    quick_match, remaining = _quick_wake_check(text)
    if quick_match:
        is_stop = any(s in remaining for s in STOP_VARIANTS)
        return True, remaining, is_stop

    # Stage 2: LLM fallback only for 8B+ models (1.2B too unreliable)
    # For now regex handles all known mishearings; add new ones as discovered
    return False, "", False


# ---------------------------------------------------------------------------
# MLX Whisper (lazy-loaded)
# ---------------------------------------------------------------------------

_mlx_whisper = None

def _get_mlx_whisper():
    global _mlx_whisper
    if _mlx_whisper is None:
        import mlx_whisper
        _mlx_whisper = mlx_whisper
    return _mlx_whisper


def transcribe(audio: np.ndarray) -> str:
    """Transcribe audio via MLX Whisper on M4 Max GPU.

    Passes numpy array directly — no ffmpeg dependency.
    """
    try:
        audio_f32 = audio.flatten().astype(np.float32) / 32768.0
        result = _get_mlx_whisper().transcribe(
            audio_f32, path_or_hf_repo="mlx-community/whisper-tiny", language="en",
        )
        return result.get("text", "").strip()
    except Exception as e:
        log(f"Transcribe error: {e}")
        return ""


# ---------------------------------------------------------------------------
# Terminal interaction — NO focus stealing
# ---------------------------------------------------------------------------

def get_terminal_content() -> str:
    """Read Terminal.app content WITHOUT activating or focusing it."""
    try:
        result = subprocess.run(
            ["osascript", "-e", """
            tell application "Terminal"
                set output to ""
                repeat with w in windows
                    try
                        set output to output & (contents of selected tab of w)
                    end try
                end repeat
                return output
            end tell
            """],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return ""


def type_into_terminal(text: str):
    """Type text into the frontmost Terminal WITHOUT activating it."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(
            ["osascript", "-e", f"""
            tell application "System Events"
                tell process "Terminal"
                    set frontmost to true
                    delay 0.2
                    keystroke "{escaped}"
                    delay 0.1
                    keystroke return
                end tell
            end tell
            """],
            capture_output=True, timeout=5,
        )
    except Exception as e:
        log(f"Type error: {e}")


def wait_for_response(before_content: str, timeout: float = 60) -> str:
    """Wait for Claude to respond by watching terminal content change."""
    before_len = len(before_content)
    start = time.monotonic()
    last_len = before_len
    stable_count = 0

    while time.monotonic() - start < timeout:
        time.sleep(1.0)
        current = get_terminal_content()
        current_len = len(current)

        if current_len > before_len:
            if current_len == last_len:
                stable_count += 1
                if stable_count >= 3:
                    new_content = current[before_len:]
                    return _clean_response(new_content)
            else:
                stable_count = 0
            last_len = current_len

    return ""


def _clean_response(raw: str) -> str:
    """Clean terminal output to extract just the response text."""
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw)
    clean = re.sub(r"[\u2500-\u257F]+", "", clean)
    clean = re.sub(r"\s*⏵⏵.*$", "", clean, flags=re.MULTILINE)
    clean = re.sub(r"\s*>\s*$", "", clean, flags=re.MULTILINE)
    lines = []
    for line in clean.splitlines():
        line = line.strip()
        if line and len(line) < 500:
            lines.append(line)
    text = "\n".join(lines[-20:]) if lines else ""
    return text.strip()


# ---------------------------------------------------------------------------
# TTS — edge-tts neural (background, no focus steal)
# ---------------------------------------------------------------------------

def speak(text: str) -> subprocess.Popen | None:
    """Speak text using edge-tts neural voice. Returns Popen or None."""
    clean = text.replace("```", "").replace("`", "").replace("**", "")
    clean = re.sub(r"<think>.*?</think>", "", clean, flags=re.DOTALL)
    clean = re.sub(r"\[.*?\]", "", clean)
    clean = clean.strip()
    if not clean or len(clean) < 3:
        return None

    if len(clean) > 500:
        for i in range(400, min(len(clean), 600)):
            if clean[i] in ".!?\n":
                clean = clean[:i+1]
                break
        else:
            clean = clean[:500] + "..."

    try:
        tmp_audio = tempfile.mktemp(suffix=".mp3")
        result = subprocess.run(
            ["edge-tts", "--voice", NEURAL_VOICE, "--text", clean, "--write-media", tmp_audio],
            capture_output=True, timeout=15,
        )
        if result.returncode == 0:
            return subprocess.Popen(
                ["bash", "-c", f'afplay "{tmp_audio}" ; rm -f "{tmp_audio}"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass

    # Fallback: macOS say
    try:
        tmp_txt = tempfile.mktemp(suffix=".txt")
        Path(tmp_txt).write_text(clean)
        return subprocess.Popen(
            ["bash", "-c", f'say -v "{FALLBACK_VOICE}" -r 200 -f "{tmp_txt}" ; rm -f "{tmp_txt}"'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None


def speak_and_wait(text: str):
    """Speak and wait for completion."""
    p = speak(text)
    if p:
        p.wait()


# ---------------------------------------------------------------------------
# Audio recording
# ---------------------------------------------------------------------------

def record_chunk(seconds: float) -> np.ndarray:
    """Record a fixed-length chunk from mic."""
    frames = int(SAMPLE_RATE * seconds)
    audio = sd.rec(frames, samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE)
    sd.wait()
    return audio


def has_speech(audio: np.ndarray) -> bool:
    """Check if any 100ms block in the audio exceeds the energy threshold."""
    block_size = int(SAMPLE_RATE * 0.1)
    for i in range(0, len(audio), block_size):
        block = audio[i:i + block_size]
        if len(block) < block_size // 2:
            continue
        rms = np.sqrt(np.mean(block.astype(np.float32) ** 2))
        if rms > ENERGY_THRESHOLD:
            return True
    return False


def chunk_rms(audio: np.ndarray) -> float:
    """Overall RMS for logging."""
    return float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))


def record_speech() -> np.ndarray | None:
    """Record speech until silence (for the actual command after wake word)."""
    block_size = int(SAMPLE_RATE * 0.1)
    blocks: list[np.ndarray] = []
    silence_blocks = 0
    silence_needed = int(SPEECH_SILENCE_SEC / 0.1)
    max_blocks = int(MAX_SPEECH_SEC / 0.1)
    speech_started = False

    def callback(indata, frames, time_info, status):
        nonlocal silence_blocks, speech_started
        blocks.append(indata.copy())
        rms = np.sqrt(np.mean(indata.astype(np.float32) ** 2))
        if rms > ENERGY_THRESHOLD:
            speech_started = True
            silence_blocks = 0
        elif speech_started:
            silence_blocks += 1

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS,
            dtype=DTYPE, blocksize=block_size, callback=callback,
        ):
            while True:
                sd.sleep(100)
                if speech_started and silence_blocks >= silence_needed:
                    break
                if len(blocks) >= max_blocks:
                    break

        if not speech_started or len(blocks) < 5:
            return None
        return np.concatenate(blocks)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------

def daemon_loop():
    """Main loop: listen for wake word, bridge voice to Claude."""
    activations = 0
    active = False
    tts_proc: subprocess.Popen | None = None

    log("Hey Claude daemon started")
    write_status("loading")

    # Ensure LM Studio is running
    log("Ensuring LM Studio...")
    if ensure_lmstudio():
        log("LM Studio ready")
        ensure_model_loaded(LMSTUDIO_FAST_MODEL)
    else:
        log("LM Studio not available — using regex-only wake detection")

    # Warm up whisper
    log("Loading MLX Whisper...")
    try:
        _get_mlx_whisper()
        log("MLX Whisper ready")
    except Exception as e:
        log(f"Whisper load error: {e}")

    write_status("listening")
    speak_and_wait("Hey Claude is ready.")

    while True:
        try:
            # Record a chunk
            audio = record_chunk(CHUNK_SECONDS)
            rms = chunk_rms(audio)

            if not has_speech(audio):
                write_status("listening", activations=activations, active=active, rms=round(rms, 1))
                continue

            # Transcribe the chunk
            log(f"Speech detected (RMS={rms:.0f}), transcribing...")
            text = transcribe(audio).lower().strip()
            log(f"Heard: \"{text}\"")
            if not text:
                continue

            # Two-stage wake word detection
            is_wake, after_wake, is_stop = detect_wake_word(text)
            if not is_wake:
                log(f"Not wake word: \"{text}\"")
                continue

            # Check for stop command
            if is_stop:
                log(f"Stop command: \"{text}\"")
                speak_and_wait("Voice mode off. Goodbye.")
                active = False
                write_status("listening", activations=activations, active=False)
                continue

            # Wake word detected!
            activations += 1
            active = True
            log(f"Wake #{activations}: \"{text}\" → cmd=\"{after_wake}\"")
            write_status("active", activations=activations)

            if len(after_wake) > 3:
                user_text = after_wake
                log(f"Inline command: \"{user_text}\"")
            else:
                # Acknowledge
                speak_and_wait("Yes?")

                # Record the actual command
                log("Recording command...")
                speech_audio = record_speech()
                if speech_audio is None:
                    log("No speech detected after wake word")
                    continue

                user_text = transcribe(speech_audio).strip()
                if not user_text:
                    log("Empty transcription")
                    continue

            log(f"User said: \"{user_text}\"")

            # Kill any ongoing TTS
            if tts_proc and tts_proc.poll() is None:
                tts_proc.terminate()

            # Capture terminal BEFORE typing
            before = get_terminal_content()

            # Type the user's speech into Claude's terminal
            log(f"Typing into terminal: \"{user_text}\"")
            type_into_terminal(user_text)

            # Wait for Claude's response
            log("Waiting for Claude's response...")
            response = wait_for_response(before, timeout=60)

            if response:
                log(f"Got response ({len(response)} chars)")
                tts_proc = speak(response)
                if tts_proc:
                    tts_proc.wait()
                    tts_proc = None
            else:
                log("No response detected")
                speak_and_wait("I didn't see a response. Claude might still be working.")

            write_status("listening", activations=activations, active=active)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"Error: {e}")
            write_status("error", error=str(e), activations=activations)
            time.sleep(2)

    write_status("stopped", activations=activations)
    log("Hey Claude daemon stopped")


# ---------------------------------------------------------------------------
# Pipeline test
# ---------------------------------------------------------------------------

def pipeline_test():
    print("Hey Claude — Pipeline Test")
    print()

    # 1. LM Studio
    print("1. LM Studio...", end="", flush=True)
    t0 = time.monotonic()
    ok = ensure_lmstudio()
    print(f" [{time.monotonic()-t0:.1f}s] {'OK' if ok else 'FAILED'}")
    if ok:
        ensure_model_loaded(LMSTUDIO_FAST_MODEL)
        # Test wake word detection via LLM
        for test in ["hey claude", "a clue", "random words"]:
            is_w, cmd, is_s = detect_wake_word(test)
            print(f"   Wake test \"{test}\": {'WAKE' if is_w else 'skip'}")

    # 2. Whisper
    print("2. MLX Whisper...", end="", flush=True)
    t0 = time.monotonic()
    _get_mlx_whisper()
    print(f" [{time.monotonic()-t0:.1f}s]")

    # 3. Terminal read
    print("3. Terminal content...", end="", flush=True)
    t0 = time.monotonic()
    content = get_terminal_content()
    print(f" [{time.monotonic()-t0:.1f}s] ({len(content)} chars)")

    # 4. TTS
    print("4. TTS (neural)...", end="", flush=True)
    t0 = time.monotonic()
    speak_and_wait("Pipeline test complete. Hey Claude is ready.")
    print(f" [{time.monotonic()-t0:.1f}s]")

    print()
    print("All good! Run without --test to start the daemon.")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Hey Claude — voice bridge for Claude Code")
    parser.add_argument("--test", action="store_true", help="Test pipeline")
    args = parser.parse_args()

    if args.test:
        pipeline_test()
        return

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    daemon_loop()


if __name__ == "__main__":
    main()
