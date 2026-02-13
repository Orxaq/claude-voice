#!/usr/bin/env python3
"""Always-listening wake word detector.

Listens for "switch to voice" (or configurable wake phrase) using local
Whisper tiny model. When detected, launches the voice_loop.

Designed to run as a LaunchAgent daemon. Uses minimal CPU by only transcribing
when audio energy exceeds threshold (voice activity detection).

Usage:
    python3 wake_listener.py
    python3 wake_listener.py --wake-phrase "hey claude"
"""

from __future__ import annotations

import argparse
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
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
CHUNK_SECONDS = 3  # Listen in 3-second chunks
ENERGY_THRESHOLD = 400  # Only transcribe if chunk has speech
COOLDOWN_SECONDS = 5  # After activation, wait before listening again
STATUS_FILE = Path.home() / ".claude" / "watchdogs" / "wake_listener_status.json"
VOICE_LOOP_SCRIPT = Path(__file__).resolve().parent / "voice_loop.py"

WAKE_PHRASES = {
    "switch to voice",
    "voice mode",
    "hey claude",
    "start voice",
    "activate voice",
}


def write_status(state: str, **extra):
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "state": state,
        "pid": os.getpid(),
        **extra,
    }
    STATUS_FILE.write_text(json.dumps(data, indent=2))


def has_speech(audio: np.ndarray) -> bool:
    """Check if audio chunk contains speech (above energy threshold)."""
    rms = np.sqrt(np.mean(audio.astype(np.float32) ** 2))
    return rms > ENERGY_THRESHOLD


_mlx_whisper = None


def _get_mlx_whisper():
    global _mlx_whisper
    if _mlx_whisper is None:
        import mlx_whisper
        _mlx_whisper = mlx_whisper
    return _mlx_whisper


def transcribe_chunk(audio: np.ndarray) -> str:
    """Quick transcription of short audio chunk using MLX Whisper (GPU)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name
        with wave.open(f, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())

    try:
        mlx_w = _get_mlx_whisper()
        result = mlx_w.transcribe(
            tmp_path,
            path_or_hf_repo="mlx-community/whisper-tiny",
            language="en",
        )
        return result.get("text", "").strip().lower()
    except Exception:
        return ""
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def matches_wake(text: str, custom_phrases: set[str]) -> bool:
    """Check if transcribed text contains any wake phrase."""
    text_lower = text.lower().strip()
    all_phrases = WAKE_PHRASES | custom_phrases
    return any(phrase in text_lower for phrase in all_phrases)


def launch_voice_mode(persona: str = "copilot"):
    """Launch the voice loop in the current terminal."""
    write_status("voice_active", persona=persona)

    # Use osascript to open a new Terminal window with voice_loop
    script = f'''
    tell application "Terminal"
        activate
        do script "python3 {VOICE_LOOP_SCRIPT} --persona {persona}"
    end tell
    '''
    subprocess.run(["osascript", "-e", script], capture_output=True)


def listen_loop(custom_phrases: set[str], persona: str):
    """Main listening loop — record chunks, check for wake phrase."""
    block_size = int(SAMPLE_RATE * CHUNK_SECONDS)
    activations = 0

    write_status("listening", activations=activations)
    print(f"[wake_listener] Listening for wake phrases... (PID {os.getpid()})")

    while True:
        try:
            # Record a chunk
            audio = sd.rec(block_size, samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE)
            sd.wait()

            # Only transcribe if there's speech
            if not has_speech(audio):
                continue

            # Transcribe
            text = transcribe_chunk(audio)
            if not text:
                continue

            # Check for wake phrase
            if matches_wake(text, custom_phrases):
                print(f"[wake_listener] Wake phrase detected: \"{text}\"")
                activations += 1

                # Announce
                subprocess.run(
                    ["say", "-v", "Samantha", "-r", "220", "Switching to voice mode"],
                    capture_output=True,
                )

                # Launch voice mode
                launch_voice_mode(persona)

                # Cooldown
                write_status("cooldown", activations=activations)
                time.sleep(COOLDOWN_SECONDS)
                write_status("listening", activations=activations)
            else:
                # Update status periodically
                write_status("listening", activations=activations, last_heard=text[:50])

        except KeyboardInterrupt:
            break
        except Exception as e:
            write_status("error", error=str(e))
            time.sleep(2)

    write_status("stopped", activations=activations)
    print("[wake_listener] Stopped.")


def main():
    parser = argparse.ArgumentParser(description="Always-listening wake word detector")
    parser.add_argument("--wake-phrase", action="append", default=[], help="Additional wake phrases")
    parser.add_argument("--persona", default="copilot", help="Default persona for voice mode")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    custom = set(p.lower() for p in args.wake_phrase)
    listen_loop(custom, args.persona)


if __name__ == "__main__":
    main()
