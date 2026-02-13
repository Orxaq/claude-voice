#!/usr/bin/env python3
"""Zero-lag voice conversation loop.

Records from mic with VAD silence detection, transcribes locally via Whisper,
sends to LLM (LM Studio local-first, cloud fallback), speaks response via macOS say.

Usage:
    python3 voice_loop.py              # default (copilot persona)
    python3 voice_loop.py --persona executive
    python3 voice_loop.py --model qwen/qwen3-coder-next
    voice                              # shell alias
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
BLOCK_DURATION = 0.1  # 100ms blocks for VAD
SILENCE_THRESHOLD = 500  # RMS below this = silence
SILENCE_DURATION = 1.5  # seconds of silence to stop recording
MAX_RECORD_SECONDS = 30  # safety cap
MIN_RECORD_SECONDS = 0.5  # ignore very short recordings

LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "http://localhost:1234")
LM_STUDIO_MODEL = os.getenv("LM_STUDIO_MODEL", "liquid/lfm2.5-1.2b")  # fastest for voice
VOICE_BACKEND_URL = os.getenv("VOICE_BACKEND_URL", "http://localhost:7777")

WAKE_WORDS = {"stop", "exit", "quit", "bye", "goodbye", "end voice", "switch to text"}

# (TTS voices defined in the TTS section below)

PERSONAS = {
    "default": "You are Claude, a helpful AI assistant having a voice conversation. Keep responses concise — 2-3 sentences. The user is listening, not reading.",
    "copilot": "You are a copilot for a software engineer. Give brief, clear updates. You're upbeat and encouraging. Keep responses to 1-2 sentences — the user is on the move.",
    "narrator": "You are a storyteller narrating the user's engineering journey. Warm, engaging tone. Frame technical updates as adventure. Under 4 sentences.",
    "executive": "You are a concise executive briefer. Bullet points spoken aloud. Numbers, percentages, blockers only. No fluff. Max 3 bullets.",
    "coder": "You are a pair programming partner. Keep it technical and brief. Suggest code fixes, name files, be specific. 2-3 sentences max.",
}

# ---------------------------------------------------------------------------
# Audio recording with VAD
# ---------------------------------------------------------------------------


def record_until_silence(
    silence_threshold: int = SILENCE_THRESHOLD,
    silence_duration: float = SILENCE_DURATION,
    max_seconds: float = MAX_RECORD_SECONDS,
) -> np.ndarray | None:
    """Record from mic until silence is detected. Returns audio as numpy array."""
    block_size = int(SAMPLE_RATE * BLOCK_DURATION)
    blocks: list[np.ndarray] = []
    silence_blocks = 0
    silence_blocks_needed = int(silence_duration / BLOCK_DURATION)
    max_blocks = int(max_seconds / BLOCK_DURATION)
    speech_started = False

    print("  🎤 Listening...", end="", flush=True)

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
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=block_size,
            callback=callback,
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
        print(f"\n  ⚠ Mic error: {e}")
        return None


def audio_to_wav_bytes(audio: np.ndarray) -> bytes:
    """Convert numpy audio to WAV bytes for Whisper."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)  # int16
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# STT via MLX Whisper (Apple Silicon GPU — blazing fast)
# ---------------------------------------------------------------------------

_mlx_whisper = None


def _get_mlx_whisper():
    """Lazy-load mlx_whisper to avoid startup delay."""
    global _mlx_whisper
    if _mlx_whisper is None:
        import mlx_whisper
        _mlx_whisper = mlx_whisper
    return _mlx_whisper


def transcribe_whisper(audio: np.ndarray) -> str:
    """Transcribe audio using MLX Whisper on Apple Silicon GPU.

    Falls back to CLI whisper if mlx_whisper is unavailable.
    """
    # Write temp WAV for transcription
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name
        with wave.open(f, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())

    try:
        # Try MLX Whisper first (fast, GPU-accelerated)
        try:
            mlx_w = _get_mlx_whisper()
            result = mlx_w.transcribe(
                tmp_path,
                path_or_hf_repo="mlx-community/whisper-tiny",
                language="en",
            )
            text = result.get("text", "").strip()
            return text
        except Exception:
            pass

        # Fallback: CLI whisper
        proc = subprocess.run(
            [
                "whisper", tmp_path,
                "--model", "tiny",
                "--language", "en",
                "--output_format", "txt",
                "--output_dir", tempfile.gettempdir(),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        txt_path = Path(tempfile.gettempdir()) / Path(tmp_path).with_suffix(".txt").name
        if txt_path.exists():
            text = txt_path.read_text().strip()
            txt_path.unlink(missing_ok=True)
            return text

        return ""
    except Exception:
        return ""
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# LLM via LM Studio (local) with cloud fallback
# ---------------------------------------------------------------------------


def chat_lm_studio(messages: list[dict], system_prompt: str, model: str) -> str:
    """Send chat to LM Studio and get response."""
    import urllib.request
    import urllib.error

    api_messages = [{"role": "system", "content": system_prompt}] + messages

    body = json.dumps({
        "model": model,
        "messages": api_messages,
        "max_tokens": 300,
        "temperature": 0.7,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        f"{LM_STUDIO_URL}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            text = data["choices"][0]["message"]["content"]
            # Strip thinking tokens from models like qwen/deepseek
            import re
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            return text
    except (urllib.error.URLError, TimeoutError, KeyError) as e:
        return f"LLM error: {e}"


def chat_cloud_fallback(messages: list[dict], system_prompt: str) -> str:
    """Fallback to claude-voice backend (which has multi-provider fallback)."""
    import urllib.request
    import urllib.error

    body = json.dumps({
        "content": messages[-1]["content"] if messages else "",
        "persona": "copilot",
    }).encode()

    req = urllib.request.Request(
        f"{VOICE_BACKEND_URL}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get("content", "No response")
    except Exception as e:
        return f"Cloud fallback error: {e}"


def chat(messages: list[dict], system_prompt: str, model: str) -> str:
    """Try local LM Studio first, fall back to cloud."""
    try:
        # Quick connectivity check
        import urllib.request
        urllib.request.urlopen(f"{LM_STUDIO_URL}/v1/models", timeout=2)
        return chat_lm_studio(messages, system_prompt, model)
    except Exception:
        return chat_cloud_fallback(messages, system_prompt)


# ---------------------------------------------------------------------------
# TTS — edge-tts neural (online) → macOS say (offline fallback)
# ---------------------------------------------------------------------------

# Neural voice map — these sound like real humans
NEURAL_VOICES = {
    "default": "en-US-AvaNeural",        # Expressive, caring, friendly
    "copilot": "en-US-BrianNeural",      # Approachable, casual, sincere
    "narrator": "en-GB-RyanNeural",      # Warm, British
    "executive": "en-US-AndrewNeural",   # Warm, confident, authentic
    "coder": "en-US-EmmaNeural",         # Cheerful, clear, conversational
}

# Fallback macOS say voices (for offline)
FALLBACK_VOICES = {
    "default": "Samantha",
    "copilot": "Daniel",
    "narrator": "Reed (English (UK))",
    "executive": "Karen",
    "coder": "Samantha",
}

_tts_mode = "auto"  # "auto", "neural", "local"


def speak(text: str, voice: str = "Samantha", speed: int = 200, persona: str = "default") -> subprocess.Popen:
    """Speak text using the best available TTS.

    Tries edge-tts neural voice first (sounds like a real person),
    falls back to macOS say if offline.
    Returns a subprocess.Popen that the caller can wait() or terminate().
    """
    # Clean text for speech — remove code artifacts
    clean = text.replace("```", "").replace("`", "").replace("**", "")
    clean = clean.replace("<think>", "").replace("</think>", "")
    clean = clean.strip()
    if not clean:
        clean = "I didn't catch that."

    # Try edge-tts neural voice first
    if _tts_mode in ("auto", "neural"):
        try:
            neural_voice = NEURAL_VOICES.get(persona, NEURAL_VOICES["default"])
            return _speak_neural(clean, neural_voice)
        except Exception:
            pass

    # Fallback: macOS say
    return _speak_macos(clean, voice, speed)


def _speak_neural(text: str, voice: str) -> subprocess.Popen:
    """Use edge-tts for neural/natural-sounding speech."""
    tmp_audio = tempfile.mktemp(suffix=".mp3")

    # edge-tts generates audio file, then we play it
    # Run edge-tts synchronously to generate, then play async
    result = subprocess.run(
        ["edge-tts", "--voice", voice, "--text", text, "--write-media", tmp_audio],
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"edge-tts failed: {result.stderr[:100]}")

    # Play with afplay (non-blocking) — will clean up temp file after
    return subprocess.Popen(
        ["bash", "-c", f'afplay "{tmp_audio}" ; rm -f "{tmp_audio}"'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _speak_macos(text: str, voice: str, speed: int) -> subprocess.Popen:
    """Fallback: macOS say command."""
    # Write text to temp file to avoid shell escaping issues
    tmp_txt = tempfile.mktemp(suffix=".txt")
    Path(tmp_txt).write_text(text)
    return subprocess.Popen(
        ["bash", "-c", f'say -v "{voice}" -r {speed} -f "{tmp_txt}" ; rm -f "{tmp_txt}"'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Main voice loop
# ---------------------------------------------------------------------------


def voice_loop(persona: str, model: str, voice: str | None, speed: int) -> None:
    """Main voice conversation loop."""
    system_prompt = PERSONAS.get(persona, PERSONAS["default"])
    tts_voice = voice or FALLBACK_VOICES.get(persona, "Samantha")
    messages: list[dict] = []

    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║       VOICE MODE ACTIVE              ║")
    print("  ╠══════════════════════════════════════╣")
    print(f"  ║  Persona: {persona:<26} ║")
    print(f"  ║  Model:   {model[:26]:<26} ║")
    print(f"  ║  Voice:   {tts_voice[:26]:<26} ║")
    print("  ║                                      ║")
    print('  ║  Say "stop" or press Ctrl+C to exit   ║')
    print("  ╚══════════════════════════════════════╝")
    print()

    # Warm up MLX Whisper in background while greeting plays
    print("  Warming up STT engine...", end="", flush=True)
    import threading

    def _warmup():
        try:
            _get_mlx_whisper()
        except Exception:
            pass

    warmup = threading.Thread(target=_warmup, daemon=True)
    warmup.start()

    # Greeting (plays while whisper loads)
    greeting_proc = speak(f"Voice mode active. {persona} persona ready.", tts_voice, speed, persona=persona)
    warmup.join(timeout=10)
    print(" ready.")
    greeting_proc.wait()

    tts_proc: subprocess.Popen | None = None

    while True:
        try:
            # Kill any ongoing TTS before listening
            if tts_proc and tts_proc.poll() is None:
                tts_proc.terminate()

            # Record
            audio = record_until_silence()
            if audio is None:
                continue

            # Transcribe
            print("  📝 Transcribing...", end="", flush=True)
            t0 = time.monotonic()
            text = transcribe_whisper(audio)
            dt = time.monotonic() - t0
            print(f" [{dt:.1f}s] \"{text}\"")

            if not text or len(text.strip()) < 2:
                continue

            # Check for exit commands
            if text.strip().lower().rstrip(".!?,") in WAKE_WORDS:
                speak("Switching back to text. Goodbye.", tts_voice, speed, persona=persona).wait()
                print("\n  👋 Voice mode ended.\n")
                break

            # Chat
            messages.append({"role": "user", "content": text})
            print("  🧠 Thinking...", end="", flush=True)
            t0 = time.monotonic()
            response = chat(messages, system_prompt, model)
            dt = time.monotonic() - t0
            print(f" [{dt:.1f}s]")

            # Show response text
            print(f"  💬 {response}")
            messages.append({"role": "assistant", "content": response})

            # Speak response (non-blocking so user can interrupt)
            tts_proc = speak(response, tts_voice, speed, persona=persona)

            # Keep only last 10 turns to avoid context overflow
            if len(messages) > 20:
                messages = messages[-20:]

        except KeyboardInterrupt:
            if tts_proc and tts_proc.poll() is None:
                tts_proc.terminate()
            speak("Voice mode ended.", tts_voice, speed, persona=persona).wait()
            print("\n\n  👋 Voice mode ended.\n")
            break


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def pipeline_test():
    """Non-interactive test of the full pipeline."""
    import threading

    print("Pipeline test — no mic needed")
    print()

    # 1. MLX Whisper
    print("1. MLX Whisper...", end="", flush=True)
    t0 = time.monotonic()
    _get_mlx_whisper()
    print(f" loaded [{time.monotonic()-t0:.1f}s]")

    # 2. LM Studio
    print("2. LM Studio...", end="", flush=True)
    t0 = time.monotonic()
    resp = chat(
        [{"role": "user", "content": "Say hello in one word."}],
        "You are a test. One word only.",
        LM_STUDIO_MODEL,
    )
    print(f" [{time.monotonic()-t0:.1f}s] \"{resp}\"")

    # 3. TTS (neural voice)
    print("3. TTS (neural)...", end="", flush=True)
    t0 = time.monotonic()
    p = speak("Pipeline test complete. Neural voice sounds much better, right?", persona="copilot")
    p.wait()
    print(f" [{time.monotonic()-t0:.1f}s]")

    print()
    print("All systems go!")


def main():
    parser = argparse.ArgumentParser(description="Zero-lag voice conversation")
    parser.add_argument("--persona", default="copilot", choices=list(PERSONAS.keys()))
    parser.add_argument("--model", default=LM_STUDIO_MODEL)
    parser.add_argument("--voice", default=None, help="macOS TTS voice name")
    parser.add_argument("--speed", type=int, default=200, help="TTS words per minute")
    parser.add_argument("--test", action="store_true", help="Run pipeline test (no mic)")
    args = parser.parse_args()

    if args.test:
        pipeline_test()
        return

    # Graceful shutdown
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    voice_loop(args.persona, args.model, args.voice, args.speed)


if __name__ == "__main__":
    main()
