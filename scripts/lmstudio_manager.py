"""LM Studio manager — launch, load/unload models, query via API.

Provides a clean interface for hey_claude.py and other scripts to:
- Ensure LM Studio is running
- Load/unload models by name
- Query the LLM for wake word interpretation and voice responses
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
import urllib.error

LMS_CLI = "/Users/sdevisch/.lmstudio/bin/lms"
API_BASE = "http://localhost:1234/v1"

# Model tiers — smallest first for fast tasks, larger for quality
MODELS = {
    "fast": "liquid/lfm2.5-1.2b",
    "code": "deepseek-coder-v2-lite-instruct",
    "reason_small": "deepseek/deepseek-r1-0528-qwen3-8b",
    "vision": "qwen/qwen3-vl-8b",
    "general": "google/gemma-3-4b",
    "code_large": "qwen/qwen2.5-coder-32b",
    "reason_large": "deepseek-r1-distill-llama-70b",
}

DEFAULT_MODEL = MODELS["fast"]


def _run(args: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except Exception as e:
        return 1, str(e)


def is_server_running() -> bool:
    try:
        req = urllib.request.Request(f"{API_BASE}/models", method="GET")
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False


def ensure_server() -> bool:
    """Ensure LM Studio app and server are running. Returns True if ready."""
    if is_server_running():
        return True

    # Try starting the server via CLI
    _run([LMS_CLI, "server", "start"])
    time.sleep(3)

    if is_server_running():
        return True

    # Launch LM Studio app itself
    subprocess.Popen(
        ["open", "-a", "LM Studio"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Wait for server to come up
    for _ in range(20):
        time.sleep(2)
        if is_server_running():
            return True

    return False


def list_loaded() -> list[str]:
    """List currently loaded model identifiers."""
    try:
        req = urllib.request.Request(f"{API_BASE}/models", method="GET")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        return [m["id"] for m in data.get("data", [])]
    except Exception:
        return []


def list_available() -> list[dict]:
    """List models on disk."""
    code, out = _run([LMS_CLI, "ls"])
    return out


def load_model(model_id: str, ttl: int = 3600, gpu: str = "max") -> bool:
    """Load a model into memory."""
    loaded = list_loaded()
    if model_id in loaded:
        return True

    code, out = _run([
        LMS_CLI, "load", model_id,
        "--gpu", gpu,
        "--ttl", str(ttl),
        "-y",
    ], timeout=120)
    return code == 0


def unload_model(model_id: str) -> bool:
    """Unload a model from memory."""
    code, out = _run([LMS_CLI, "unload", model_id, "-y"], timeout=30)
    return code == 0


def download_model(model_name: str, mlx: bool = True) -> bool:
    """Download a model. Returns True on success."""
    args = [LMS_CLI, "get", model_name, "-y"]
    if mlx:
        args.append("--mlx")
    code, out = _run(args, timeout=600)
    return code == 0


def chat(
    prompt: str,
    system: str = "",
    model: str = DEFAULT_MODEL,
    temperature: float = 0.3,
    max_tokens: int = 200,
) -> str:
    """Send a chat completion request. Returns the response text."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()

    req = urllib.request.Request(
        f"{API_BASE}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"[LM Studio error: {e}]"


def interpret_wake_word(raw_transcription: str) -> dict:
    """Use LLM to interpret whether a sloppy transcription is 'Hey Claude'.

    Returns {"is_wake": bool, "command": str, "is_stop": bool}
    """
    resp = chat(
        prompt=f'Speech transcription: "{raw_transcription}"\n\nIs this "Hey Claude"?',
        system=(
            "You classify short speech transcriptions. A small speech model transcribed the user's audio. "
            "You must decide if they said 'Hey Claude' (a wake word to activate a voice assistant).\n\n"
            "IMPORTANT RULES:\n"
            "- ONLY return true if the transcription sounds phonetically similar to 'Hey Claude'\n"
            "- Valid matches: 'hey claude', 'hey claud', 'a clue', 'hey clod', 'hey cloud', 'hey clawed', 'he claude'\n"
            "- NOT matches: 'random words', 'hello', 'hake plug', 'thank you', anything that doesn't sound like 'hey claude'\n"
            "- 'command' = any words AFTER the wake word part. e.g. 'hey claude what time is it' → command='what time is it'\n"
            "- 'is_stop' = true only if they said bye/stop/quit/exit after the wake word\n\n"
            "Respond with ONLY valid JSON, no other text:\n"
            '{"is_wake": true/false, "command": "", "is_stop": false}'
        ),
        temperature=0.0,
        max_tokens=80,
    )

    # Parse JSON from response
    try:
        # Find JSON in response
        start = resp.index("{")
        end = resp.rindex("}") + 1
        return json.loads(resp[start:end])
    except (ValueError, json.JSONDecodeError):
        return {"is_wake": False, "command": "", "is_stop": False}


# Quick test
if __name__ == "__main__":
    print(f"Server running: {is_server_running()}")
    print(f"Loaded models: {list_loaded()}")

    if not is_server_running():
        print("Starting server...")
        ensure_server()

    # Ensure fast model is loaded
    print(f"Loading {DEFAULT_MODEL}...")
    load_model(DEFAULT_MODEL)
    print(f"Loaded: {list_loaded()}")

    # Test wake word interpretation
    tests = [
        "hey claude.", "a clue.", "hey, claude.", "hake plug?",
        "hey cloud do something", "hey claude bye", "random words",
    ]
    for t in tests:
        result = interpret_wake_word(t)
        print(f'  "{t}" → {result}')
