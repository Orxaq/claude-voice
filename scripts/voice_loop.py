#!/usr/bin/env python3
"""Zero-lag voice conversation loop with screen awareness.

Records from mic with VAD, transcribes via MLX Whisper on M4 Max GPU,
talks to LLM with live screen context, speaks via edge-tts neural voices.

Features:
  - "What's on screen?" / "What's happening?" → reads terminal, summarizes
  - "Summarize" → intelligent summary of recent terminal activity
  - Screen context is always available to the LLM for natural conversation
  - Neural TTS (edge-tts) with macOS say fallback

Usage:
    voice                  # copilot (default)
    voice coder            # pair programming partner
    voice exec             # executive briefer
"""

from __future__ import annotations

import argparse
import io
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
BLOCK_DURATION = 0.1
SILENCE_THRESHOLD = 500
SILENCE_DURATION = 1.5
MAX_RECORD_SECONDS = 30
MIN_RECORD_SECONDS = 0.5

LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "http://localhost:1234")
LM_STUDIO_MODEL = os.getenv("LM_STUDIO_MODEL", "liquid/lfm2.5-1.2b")
VOICE_BACKEND_URL = os.getenv("VOICE_BACKEND_URL", "http://localhost:7777")

WAKE_WORDS = {"stop", "exit", "quit", "bye", "goodbye", "end voice", "switch to text"}

# Screen-reading trigger phrases
SCREEN_TRIGGERS = {
    "what's on screen", "whats on screen", "what is on screen",
    "what's happening", "whats happening", "what is happening",
    "what do you see", "read the screen", "screen update",
    "what's going on", "whats going on", "status update",
    "summarize", "summary", "what's the status", "give me a summary",
    "what are you working on", "what is claude doing",
}

# ---------------------------------------------------------------------------
# Persona system prompts (screen-aware)
# ---------------------------------------------------------------------------

SCREEN_CONTEXT_PREFIX = """You have the ability to see what's on the user's terminal screen. When screen content is provided, reference it naturally in your response — like a colleague looking over their shoulder. Don't just read it back; interpret, summarize, and add value.

"""

PERSONAS = {
    "default": SCREEN_CONTEXT_PREFIX + "You are Claude, a helpful AI assistant having a voice conversation. You can see the user's screen. Keep responses concise — 2-3 sentences. The user is listening, not reading.",
    "copilot": SCREEN_CONTEXT_PREFIX + "You are a copilot for a software engineer. You can see their terminal. Give brief, smart updates about what you see. You're upbeat and encouraging. 1-2 sentences — they're on the move. Reference specific files, tests, or errors you see.",
    "narrator": SCREEN_CONTEXT_PREFIX + "You are a storyteller narrating the user's engineering journey. You can see their screen. Warm, engaging tone. Frame what's happening as an adventure. Under 4 sentences.",
    "executive": SCREEN_CONTEXT_PREFIX + "You are a concise executive briefer. You can see the terminal. Bullet points spoken aloud. Summarize what's happening: progress, blockers, numbers. No fluff. Max 3 bullets.",
    "coder": SCREEN_CONTEXT_PREFIX + "You are a pair programming partner. You can see the terminal. Keep it technical and brief. Comment on errors, suggest fixes, name files. 2-3 sentences max.",
}

# ---------------------------------------------------------------------------
# Screen capture — reads Terminal.app content via AppleScript
# ---------------------------------------------------------------------------

_last_screen: str = ""
_screen_lock = threading.Lock()


def capture_screen() -> str:
    """Capture the visible text from the frontmost Terminal window."""
    global _last_screen
    try:
        # Get content from all Terminal windows
        result = subprocess.run(
            ["osascript", "-e", """
            tell application "Terminal"
                set output to ""
                repeat with w in windows
                    try
                        set tabContent to contents of selected tab of w
                        -- Take last 3000 chars to keep context manageable
                        if length of tabContent > 3000 then
                            set tabContent to text ((length of tabContent) - 2999) thru -1 of tabContent
                        end if
                        set output to output & "--- Terminal Window ---" & return & tabContent & return
                    end try
                end repeat
                return output
            end tell
            """],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            with _screen_lock:
                _last_screen = result.stdout.strip()
            return _last_screen
    except Exception:
        pass

    # Fallback: try to read the autopilot status for some context
    try:
        status_path = Path.home() / ".claude" / "autopilot" / "status.json"
        if status_path.exists():
            data = json.loads(status_path.read_text())
            return f"[Autopilot status: {json.dumps(data, indent=2)}]"
    except Exception:
        pass

    with _screen_lock:
        return _last_screen or "(no screen content available)"


def get_screen_context() -> str:
    """Get a formatted screen context string for the LLM."""
    screen = capture_screen()
    if not screen or screen == "(no screen content available)":
        return ""
    # Truncate to avoid overwhelming the LLM
    if len(screen) > 2000:
        screen = screen[-2000:]
    return f"\n\n[CURRENT SCREEN CONTENT]\n{screen}\n[END SCREEN CONTENT]"


def is_screen_request(text: str) -> bool:
    """Check if the user is asking about what's on screen."""
    text_lower = text.lower().strip().rstrip(".!?,")
    return any(trigger in text_lower for trigger in SCREEN_TRIGGERS)


# Background screen refresh (updates every 10s so it's always fresh)
def _screen_refresh_loop():
    while True:
        try:
            capture_screen()
        except Exception:
            pass
        time.sleep(10)


# ---------------------------------------------------------------------------
# Audio recording with VAD
# ---------------------------------------------------------------------------


def record_until_silence(
    silence_threshold: int = SILENCE_THRESHOLD,
    silence_duration: float = SILENCE_DURATION,
    max_seconds: float = MAX_RECORD_SECONDS,
) -> np.ndarray | None:
    """Record from mic until silence is detected."""
    block_size = int(SAMPLE_RATE * BLOCK_DURATION)
    blocks: list[np.ndarray] = []
    silence_blocks = 0
    silence_blocks_needed = int(silence_duration / BLOCK_DURATION)
    max_blocks = int(max_seconds / BLOCK_DURATION)
    speech_started = False

    print("  Listening...", end="", flush=True)

    def callback(indata, frames, time_info, status):
        nonlocal silence_blocks, speech_started
        blocks.append(indata.copy())
        rms = np.sqrt(np.mean(indata.astype(np.float32) ** 2))
        if rms > silence_threshold:
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
                sd.sleep(int(BLOCK_DURATION * 1000))
                if speech_started and silence_blocks >= silence_blocks_needed:
                    break
                if len(blocks) >= max_blocks:
                    break

        print(" done.")
        if not speech_started or len(blocks) < int(MIN_RECORD_SECONDS / BLOCK_DURATION):
            return None
        return np.concatenate(blocks)
    except Exception as e:
        print(f"\n  Mic error: {e}")
        return None


# ---------------------------------------------------------------------------
# STT via MLX Whisper (Apple Silicon GPU)
# ---------------------------------------------------------------------------

_mlx_whisper = None


def _get_mlx_whisper():
    global _mlx_whisper
    if _mlx_whisper is None:
        import mlx_whisper
        _mlx_whisper = mlx_whisper
    return _mlx_whisper


def transcribe_whisper(audio: np.ndarray) -> str:
    """Transcribe audio using MLX Whisper on M4 Max GPU (~0.3s)."""
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
        return result.get("text", "").strip()
    except Exception:
        # Fallback: CLI whisper
        try:
            proc = subprocess.run(
                ["whisper", tmp_path, "--model", "tiny", "--language", "en",
                 "--output_format", "txt", "--output_dir", tempfile.gettempdir()],
                capture_output=True, text=True, timeout=30,
            )
            txt_path = Path(tempfile.gettempdir()) / Path(tmp_path).with_suffix(".txt").name
            if txt_path.exists():
                text = txt_path.read_text().strip()
                txt_path.unlink(missing_ok=True)
                return text
        except Exception:
            pass
        return ""
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# LLM — LM Studio (local) → cloud fallback
# ---------------------------------------------------------------------------


def chat_lm_studio(messages: list[dict], system_prompt: str, model: str) -> str:
    import urllib.request, urllib.error

    api_messages = [{"role": "system", "content": system_prompt}] + messages
    body = json.dumps({
        "model": model, "messages": api_messages,
        "max_tokens": 300, "temperature": 0.7, "stream": False,
    }).encode()

    req = urllib.request.Request(
        f"{LM_STUDIO_URL}/v1/chat/completions",
        data=body, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            text = data["choices"][0]["message"]["content"]
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            return text
    except (urllib.error.URLError, TimeoutError, KeyError) as e:
        return f"LLM error: {e}"


def chat_cloud_fallback(messages: list[dict], system_prompt: str) -> str:
    import urllib.request, urllib.error

    body = json.dumps({
        "content": messages[-1]["content"] if messages else "",
        "persona": "copilot",
    }).encode()
    req = urllib.request.Request(
        f"{VOICE_BACKEND_URL}/api/chat",
        data=body, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get("content", "No response")
    except Exception as e:
        return f"Cloud fallback error: {e}"


def chat(messages: list[dict], system_prompt: str, model: str) -> str:
    try:
        import urllib.request
        urllib.request.urlopen(f"{LM_STUDIO_URL}/v1/models", timeout=2)
        return chat_lm_studio(messages, system_prompt, model)
    except Exception:
        return chat_cloud_fallback(messages, system_prompt)


# ---------------------------------------------------------------------------
# TTS — edge-tts neural (online) → macOS say (offline)
# ---------------------------------------------------------------------------

NEURAL_VOICES = {
    "default": "en-US-AvaNeural",
    "copilot": "en-US-BrianNeural",
    "narrator": "en-GB-RyanNeural",
    "executive": "en-US-AndrewNeural",
    "coder": "en-US-EmmaNeural",
}

FALLBACK_VOICES = {
    "default": "Samantha",
    "copilot": "Daniel",
    "narrator": "Reed (English (UK))",
    "executive": "Karen",
    "coder": "Samantha",
}


def speak(text: str, voice: str = "Samantha", speed: int = 200, persona: str = "default") -> subprocess.Popen:
    """Speak text. Tries edge-tts neural first, macOS say fallback.

    ALWAYS returns a Popen the caller can .wait() or .terminate().
    """
    # Clean for speech
    clean = text.replace("```", "").replace("`", "").replace("**", "")
    clean = re.sub(r"<think>.*?</think>", "", clean, flags=re.DOTALL)
    clean = re.sub(r"\[.*?\]", "", clean)  # remove [SCREEN CONTENT] markers
    clean = clean.strip()
    if not clean:
        clean = "I didn't catch that."

    # Try edge-tts neural first
    try:
        neural_voice = NEURAL_VOICES.get(persona, NEURAL_VOICES["default"])
        return _speak_neural(clean, neural_voice)
    except Exception:
        pass

    # Fallback: macOS say
    return _speak_macos(clean, voice, speed)


def _speak_neural(text: str, voice: str) -> subprocess.Popen:
    """edge-tts neural voice — sounds like a real person."""
    tmp_audio = tempfile.mktemp(suffix=".mp3")
    result = subprocess.run(
        ["edge-tts", "--voice", voice, "--text", text, "--write-media", tmp_audio],
        capture_output=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"edge-tts failed: {result.returncode}")
    return subprocess.Popen(
        ["bash", "-c", f'afplay "{tmp_audio}" ; rm -f "{tmp_audio}"'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _speak_macos(text: str, voice: str, speed: int) -> subprocess.Popen:
    """macOS say — offline fallback."""
    tmp_txt = tempfile.mktemp(suffix=".txt")
    Path(tmp_txt).write_text(text)
    return subprocess.Popen(
        ["bash", "-c", f'say -v "{voice}" -r {speed} -f "{tmp_txt}" ; rm -f "{tmp_txt}"'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Main voice loop
# ---------------------------------------------------------------------------


def voice_loop(persona: str, model: str, voice: str | None, speed: int) -> None:
    system_prompt = PERSONAS.get(persona, PERSONAS["default"])
    tts_voice = voice or FALLBACK_VOICES.get(persona, "Samantha")
    messages: list[dict] = []

    print()
    print("  VOICE MODE ACTIVE")
    print("  ─────────────────────────────────────")
    print(f"  Persona:  {persona}")
    print(f"  Model:    {model}")
    print(f"  Voice:    {NEURAL_VOICES.get(persona, 'Samantha')} (neural)")
    print()
    print("  Commands you can say:")
    print('    "What\'s on screen?"  — reads and discusses terminal')
    print('    "Summarize"          — intelligent status summary')
    print('    "Stop" / Ctrl+C     — exit voice mode')
    print("  ─────────────────────────────────────")
    print()

    # Start background screen refresh
    screen_thread = threading.Thread(target=_screen_refresh_loop, daemon=True)
    screen_thread.start()

    # Warm up MLX Whisper while greeting plays
    print("  Warming up...", end="", flush=True)

    def _warmup():
        try:
            _get_mlx_whisper()
        except Exception:
            pass

    warmup = threading.Thread(target=_warmup, daemon=True)
    warmup.start()

    # Capture initial screen context
    initial_screen = capture_screen()

    # Greeting — speaks immediately
    greeting = f"Voice mode active. I'm your {persona}."
    if initial_screen and initial_screen != "(no screen content available)":
        greeting += " I can see your terminal. Ask me anything about what's on screen."
    greeting_proc = speak(greeting, tts_voice, speed, persona=persona)
    warmup.join(timeout=10)
    print(" ready.")
    greeting_proc.wait()

    tts_proc: subprocess.Popen | None = None

    while True:
        try:
            # Kill ongoing TTS before listening
            if tts_proc and tts_proc.poll() is None:
                tts_proc.terminate()

            # Record
            audio = record_until_silence()
            if audio is None:
                continue

            # Transcribe
            print("  Transcribing...", end="", flush=True)
            t0 = time.monotonic()
            text = transcribe_whisper(audio)
            dt = time.monotonic() - t0
            print(f" [{dt:.1f}s] \"{text}\"")

            if not text or len(text.strip()) < 2:
                continue

            # Exit check
            if text.strip().lower().rstrip(".!?,") in WAKE_WORDS:
                speak("Switching back to text. Goodbye.", tts_voice, speed, persona=persona).wait()
                print("\n  Voice mode ended.\n")
                break

            # Build the user message — attach screen context if relevant
            user_content = text
            if is_screen_request(text):
                # Explicit screen request — always attach fresh screen
                screen = capture_screen()
                user_content = f"{text}\n\n{get_screen_context()}"
                print("  [screen captured]")
            else:
                # For general conversation, attach screen context periodically
                # so the LLM stays aware of what's happening
                screen_ctx = get_screen_context()
                if screen_ctx and len(messages) % 3 == 0:
                    user_content = f"{text}\n\n[Background context — latest screen state]{screen_ctx}"

            messages.append({"role": "user", "content": user_content})

            # Chat
            print("  Thinking...", end="", flush=True)
            t0 = time.monotonic()
            response = chat(messages, system_prompt, model)
            dt = time.monotonic() - t0
            print(f" [{dt:.1f}s]")

            # Show response
            print(f"  > {response}")
            messages.append({"role": "assistant", "content": response})

            # SPEAK the response (this is the critical part)
            tts_proc = speak(response, tts_voice, speed, persona=persona)

            # Wait for speech to finish before listening again
            # This prevents the mic from picking up the TTS output
            tts_proc.wait()
            tts_proc = None

            # Keep conversation manageable
            if len(messages) > 20:
                messages = messages[-20:]

        except KeyboardInterrupt:
            if tts_proc and tts_proc.poll() is None:
                tts_proc.terminate()
            speak("Voice mode ended.", tts_voice, speed, persona=persona).wait()
            print("\n\n  Voice mode ended.\n")
            break


# ---------------------------------------------------------------------------
# Pipeline test
# ---------------------------------------------------------------------------


def pipeline_test():
    print("Pipeline test — no mic needed")
    print()

    # 1. MLX Whisper
    print("1. MLX Whisper...", end="", flush=True)
    t0 = time.monotonic()
    _get_mlx_whisper()
    print(f" loaded [{time.monotonic()-t0:.1f}s]")

    # 2. Screen capture
    print("2. Screen capture...", end="", flush=True)
    t0 = time.monotonic()
    screen = capture_screen()
    print(f" [{time.monotonic()-t0:.1f}s] ({len(screen)} chars)")

    # 3. LM Studio with screen context
    print("3. LM Studio + screen context...", end="", flush=True)
    t0 = time.monotonic()
    ctx = get_screen_context()
    resp = chat(
        [{"role": "user", "content": f"What do you see on screen? Be brief.{ctx}"}],
        PERSONAS["copilot"],
        LM_STUDIO_MODEL,
    )
    print(f" [{time.monotonic()-t0:.1f}s]")
    print(f"   LLM says: \"{resp[:100]}\"")

    # 4. TTS
    print("4. TTS (neural)...", end="", flush=True)
    t0 = time.monotonic()
    p = speak("Pipeline test complete. I can see your screen and talk about it.", persona="copilot")
    p.wait()
    print(f" [{time.monotonic()-t0:.1f}s]")

    print()
    print("All systems go!")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Voice conversation with screen awareness")
    parser.add_argument("--persona", default="copilot", choices=list(PERSONAS.keys()))
    parser.add_argument("--model", default=LM_STUDIO_MODEL)
    parser.add_argument("--voice", default=None, help="macOS TTS voice override")
    parser.add_argument("--speed", type=int, default=200, help="TTS words per minute")
    parser.add_argument("--test", action="store_true", help="Run pipeline test (no mic)")
    args = parser.parse_args()

    if args.test:
        pipeline_test()
        return

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    voice_loop(args.persona, args.model, args.voice, args.speed)


if __name__ == "__main__":
    main()
