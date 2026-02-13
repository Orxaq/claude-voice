"""Claude Voice — FastAPI backend with WebSocket streaming and multi-provider LLM fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logger = logging.getLogger("claude-voice")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.getenv("CLAUDE_VOICE_DATA", str(Path.home() / ".claude-voice")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "conversations.db"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

MAX_TOKENS = int(os.getenv("CLAUDE_VOICE_MAX_TOKENS", "1024"))

# ---------------------------------------------------------------------------
# Provider definitions
# ---------------------------------------------------------------------------

PROVIDER_CONFIGS: dict[str, dict] = {
    "anthropic": {
        "name": "Anthropic",
        "base_url": "https://api.anthropic.com",
        "model": os.getenv("CLAUDE_VOICE_MODEL", "claude-sonnet-4-5-20250929"),
        "api_format": "anthropic",
        "env_key": "ANTHROPIC_API_KEY",
    },
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api",
        "model": "anthropic/claude-sonnet-4-5",
        "api_format": "openai",
        "env_key": "OPENROUTER_API_KEY",
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com",
        "model": "gpt-4o",
        "api_format": "openai",
        "env_key": "OPENAI_API_KEY",
    },
    "groq": {
        "name": "Groq",
        "base_url": "https://api.groq.com",
        "model": "llama-3.3-70b-versatile",
        "api_format": "openai",
        "env_key": "GROQ_API_KEY",
    },
}

# Fallback order — tried sequentially until one succeeds
FALLBACK_ORDER: list[str] = ["anthropic", "openrouter", "openai", "groq"]


# ---------------------------------------------------------------------------
# Vault + API key loading
# ---------------------------------------------------------------------------


def _load_vault_env() -> dict:
    """Load API keys from the encrypted vault."""
    env: dict[str, str] = {}
    try:
        secret_key = subprocess.run(
            ["security", "find-generic-password", "-s", "com.orxaq.age-key", "-a", "orxaq-secrets", "-w"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if not secret_key:
            return env
        vault_path = (
            Path.home()
            / "Library"
            / "Mobile Documents"
            / "com~apple~CloudDocs"
            / "orxaq-vault"
            / "secrets"
            / "orxaq-ops"
            / ".env.autonomy.age"
        )
        if not vault_path.exists():
            return env
        result = subprocess.run(
            ["age", "-d", "-i", "-", str(vault_path)],
            input=secret_key, capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                line = line.removeprefix("export ").strip()
                key, _, value = line.partition("=")
                if key and value:
                    env[key] = value
    except Exception:
        pass
    return env


def _load_api_keys() -> dict[str, str]:
    """Load API keys from environment variables and the encrypted vault.

    Environment variables take precedence over vault values.
    Returns a dict mapping provider names to their API keys.
    """
    # Start with vault keys (lower priority)
    vault_env = _load_vault_env()

    keys: dict[str, str] = {}
    for provider_id, config in PROVIDER_CONFIGS.items():
        env_key = config["env_key"]
        # Env vars take priority, then vault
        value = os.getenv(env_key, "") or vault_env.get(env_key, "")
        if value:
            keys[provider_id] = value

    return keys


# Module-level state — loaded once at import, refreshable via endpoint
_api_keys: dict[str, str] = _load_api_keys()
_active_provider: str | None = None  # None = auto-fallback mode


def _get_available_providers() -> list[str]:
    """Return provider IDs that have API keys, in fallback order."""
    return [p for p in FALLBACK_ORDER if p in _api_keys]


def _resolve_provider() -> str | None:
    """Return the provider to use: the pinned one if set, otherwise the first available."""
    if _active_provider and _active_provider in _api_keys:
        return _active_provider
    available = _get_available_providers()
    return available[0] if available else None


# Convenience — keep the old module-level variable for backward-compat reads
ANTHROPIC_API_KEY = _api_keys.get("anthropic", "")
DEFAULT_MODEL = PROVIDER_CONFIGS["anthropic"]["model"]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def init_db() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'Untitled',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            persona TEXT NOT NULL DEFAULT 'default',
            tone TEXT NOT NULL DEFAULT 'balanced'
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            audio_duration_ms INTEGER DEFAULT 0,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        );
        CREATE TABLE IF NOT EXISTS offline_queue (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
        CREATE INDEX IF NOT EXISTS idx_queue_status ON offline_queue(status);
    """)
    conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Persona system
# ---------------------------------------------------------------------------


def load_personas() -> dict:
    personas_file = CONFIG_DIR / "personas.json"
    if personas_file.exists():
        return json.loads(personas_file.read_text())
    return {
        "default": {
            "name": "Claude",
            "system_prompt": "You are Claude, a helpful AI assistant. You are having a voice conversation. Keep responses concise and conversational — the user is listening, not reading. Aim for 2-3 sentences unless asked for detail.",
            "voice": "Samantha",
            "speed": 180,
        },
        "copilot": {
            "name": "Copilot",
            "system_prompt": "You are a road trip copilot. You give brief, clear updates about projects and tasks. You're upbeat and encouraging. Keep responses to 1-2 sentences max — the user is driving. Use simple language.",
            "voice": "Daniel",
            "speed": 175,
        },
        "narrator": {
            "name": "Narrator",
            "system_prompt": "You are a storyteller narrating the user's engineering journey. You speak in a warm, engaging tone. You frame technical updates as an adventure narrative. Keep it under 4 sentences.",
            "voice": "Reed (English (UK))",
            "speed": 160,
        },
        "executive": {
            "name": "Executive Brief",
            "system_prompt": "You are a concise executive briefer. You summarize project status in bullet points spoken aloud. Numbers, percentages, blockers only. No fluff. Max 3 bullet points.",
            "voice": "Karen",
            "speed": 190,
        },
    }


def load_tones() -> dict:
    tones_file = CONFIG_DIR / "tones.json"
    if tones_file.exists():
        return json.loads(tones_file.read_text())
    return {
        "concise": "Respond in 1-2 sentences max. Be direct.",
        "balanced": "Respond naturally in 2-4 sentences.",
        "detailed": "Give thorough responses with context and explanation.",
        "casual": "Be relaxed and informal, like chatting with a friend.",
        "professional": "Be precise and professional.",
    }


# ---------------------------------------------------------------------------
# LLM API streaming — multi-provider with fallback
# ---------------------------------------------------------------------------


async def _stream_anthropic(
    messages: list[dict],
    system_prompt: str,
    api_key: str,
    model: str,
    base_url: str,
) -> AsyncGenerator[str, None]:
    """Stream response chunks from the Anthropic Messages API."""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    body = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt,
        "messages": messages,
        "stream": True,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{base_url}/v1/messages",
            headers=headers,
            json=body,
        ) as response:
            if response.status_code != 200:
                error_body = await response.aread()
                raise RuntimeError(f"Anthropic API error {response.status_code}: {error_body.decode()[:200]}")

            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        text = delta.get("text", "")
                        if text:
                            yield text
                except json.JSONDecodeError:
                    continue


async def _stream_openai_compat(
    messages: list[dict],
    system_prompt: str,
    api_key: str,
    model: str,
    base_url: str,
) -> AsyncGenerator[str, None]:
    """Stream response chunks from an OpenAI-compatible chat completions API.

    Works with OpenAI, OpenRouter, and Groq.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }

    # OpenRouter likes these extra headers for ranking/tracking
    if "openrouter.ai" in base_url:
        headers["HTTP-Referer"] = "https://claude-voice.local"
        headers["X-Title"] = "Claude Voice"

    # Build messages list with system prompt as first message
    api_messages = [{"role": "system", "content": system_prompt}] + messages

    body = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": api_messages,
        "stream": True,
    }

    # Determine the chat completions endpoint
    if "openrouter.ai" in base_url:
        url = f"{base_url}/v1/chat/completions"
    else:
        url = f"{base_url}/v1/chat/completions"

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code != 200:
                error_body = await response.aread()
                raise RuntimeError(f"API error {response.status_code}: {error_body.decode()[:200]}")

            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                    choices = event.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        text = delta.get("content", "")
                        if text:
                            yield text
                except json.JSONDecodeError:
                    continue


async def stream_llm(
    messages: list[dict],
    system_prompt: str,
    model: str | None = None,
    provider_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Stream LLM response with automatic provider fallback.

    Tries providers in order: anthropic -> openrouter -> openai -> groq.
    Falls through to the next provider on any error.

    If *provider_id* is given, only that provider is tried (no fallback).
    """
    if not _api_keys:
        yield "Error: No LLM API keys configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY, or GROQ_API_KEY."
        return

    # Build the list of providers to try
    if provider_id:
        if provider_id not in _api_keys:
            yield f"Error: Provider '{provider_id}' has no API key configured."
            return
        providers_to_try = [provider_id]
    elif _active_provider and _active_provider in _api_keys:
        # Pinned provider first, then fall through to the rest
        providers_to_try = [_active_provider] + [p for p in FALLBACK_ORDER if p != _active_provider and p in _api_keys]
    else:
        providers_to_try = _get_available_providers()

    if not providers_to_try:
        yield "Error: No LLM providers available."
        return

    last_error = ""
    for pid in providers_to_try:
        config = PROVIDER_CONFIGS[pid]
        api_key = _api_keys[pid]
        use_model = model if (model and pid == "anthropic") else config["model"]
        base_url = config["base_url"]

        try:
            logger.info(f"Trying provider: {pid} (model={use_model})")
            if config["api_format"] == "anthropic":
                streamer = _stream_anthropic(messages, system_prompt, api_key, use_model, base_url)
            else:
                streamer = _stream_openai_compat(messages, system_prompt, api_key, use_model, base_url)

            # Buffer a small amount to detect errors before we start yielding
            first_chunk = None
            async for chunk in streamer:
                if first_chunk is None:
                    first_chunk = chunk
                    yield chunk
                else:
                    yield chunk

            # If we got here without exception, the provider worked
            if first_chunk is not None:
                logger.info(f"Provider {pid} succeeded")
            else:
                logger.info(f"Provider {pid} returned empty response")
            return

        except Exception as exc:
            last_error = f"{pid}: {exc}"
            logger.warning(f"Provider {pid} failed: {exc}")
            continue

    # All providers failed
    yield f"Error: All LLM providers failed. Last error: {last_error}"


async def stream_claude(
    messages: list[dict],
    system_prompt: str,
    model: str = DEFAULT_MODEL,
) -> AsyncGenerator[str, None]:
    """Stream response chunks from the LLM API — backward-compatible wrapper.

    Maintains the original function signature. Internally delegates to
    stream_llm() with multi-provider fallback.
    """
    async for chunk in stream_llm(messages, system_prompt, model=model):
        yield chunk


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    available = _get_available_providers()
    if available:
        logger.info(f"LLM providers available: {', '.join(available)}")
    else:
        logger.warning("No LLM API keys found — all providers unavailable")
    yield


app = FastAPI(title="Claude Voice", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/manifest.json")
async def manifest():
    return FileResponse(str(FRONTEND_DIR / "manifest.json"))


@app.get("/sw.js")
async def service_worker():
    return FileResponse(str(FRONTEND_DIR / "sw.js"), media_type="application/javascript")


@app.get("/api/health")
async def health():
    active = _resolve_provider()
    active_model = PROVIDER_CONFIGS[active]["model"] if active else DEFAULT_MODEL
    return {
        "status": "ok",
        "timestamp": _now(),
        "model": active_model,
        "provider": active,
        "providers_available": _get_available_providers(),
    }


@app.get("/api/personas")
async def get_personas():
    return load_personas()


@app.get("/api/tones")
async def get_tones():
    return load_tones()


@app.get("/api/providers")
async def get_providers():
    """Return which LLM providers are available (have API keys)."""
    available = _get_available_providers()
    result = {}
    for pid in FALLBACK_ORDER:
        config = PROVIDER_CONFIGS[pid]
        result[pid] = {
            "name": config["name"],
            "model": config["model"],
            "available": pid in available,
            "active": pid == _active_provider if _active_provider else (pid == available[0] if available else False),
        }
    return {
        "providers": result,
        "active_provider": _active_provider,
        "fallback_order": FALLBACK_ORDER,
        "mode": "pinned" if _active_provider else "auto-fallback",
    }


class ProviderSwitch(BaseModel):
    provider: str | None = None  # None = reset to auto-fallback mode


@app.post("/api/provider")
async def switch_provider(body: ProviderSwitch):
    """Switch the active LLM provider.

    Set provider to a specific provider ID to pin it, or null/empty to
    reset to auto-fallback mode.
    """
    global _active_provider

    if body.provider is None or body.provider == "":
        _active_provider = None
        return {
            "mode": "auto-fallback",
            "active_provider": None,
            "fallback_order": _get_available_providers(),
        }

    if body.provider not in PROVIDER_CONFIGS:
        return JSONResponse(
            {"error": f"Unknown provider: {body.provider}. Valid: {list(PROVIDER_CONFIGS.keys())}"},
            status_code=400,
        )

    if body.provider not in _api_keys:
        return JSONResponse(
            {"error": f"Provider '{body.provider}' has no API key configured."},
            status_code=400,
        )

    _active_provider = body.provider
    config = PROVIDER_CONFIGS[_active_provider]
    return {
        "mode": "pinned",
        "active_provider": _active_provider,
        "name": config["name"],
        "model": config["model"],
    }


@app.get("/api/conversations")
async def list_conversations():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/conversations")
async def create_conversation():
    conv_id = str(uuid.uuid4())
    now = _now()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (conv_id, "New Conversation", now, now),
    )
    conn.commit()
    conn.close()
    return {"id": conv_id, "title": "New Conversation", "created_at": now}


@app.get("/api/conversations/{conv_id}/messages")
async def get_messages(conv_id: str):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
        (conv_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/offline-queue")
async def queue_message(body: dict):
    """Queue a message for later processing (offline mode)."""
    msg_id = str(uuid.uuid4())
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT INTO offline_queue (id, conversation_id, content, created_at) VALUES (?, ?, ?, ?)",
        (msg_id, body.get("conversation_id", ""), body.get("content", ""), _now()),
    )
    conn.commit()
    conn.close()
    return {"queued": True, "id": msg_id}


@app.post("/api/chat")
async def http_chat(body: dict):
    """HTTP fallback for when WebSocket can't establish (spotty connections)."""
    user_text = body.get("content", "").strip()
    conv_id = body.get("conversation_id")
    persona_key = body.get("persona", "default")
    tone_key = body.get("tone", "balanced")

    if not user_text:
        return JSONResponse({"error": "empty message"}, status_code=400)

    # Create conversation if needed
    conn = sqlite3.connect(str(DB_PATH))
    if not conv_id:
        conv_id = str(uuid.uuid4())
        now = _now()
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, persona, tone) VALUES (?, ?, ?, ?, ?, ?)",
            (conv_id, user_text[:50], now, now, persona_key, tone_key),
        )
        conn.commit()

    # Save user message
    conn.execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), conv_id, "user", user_text, _now()),
    )
    conn.commit()

    # Load history
    conn.row_factory = sqlite3.Row
    history_rows = conn.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
        (conv_id,),
    ).fetchall()
    conn.close()

    api_messages = [{"role": r["role"], "content": r["content"]} for r in history_rows]

    personas = load_personas()
    tones = load_tones()
    persona = personas.get(persona_key, personas["default"])
    tone_instruction = tones.get(tone_key, tones["balanced"])
    system_prompt = f"{persona['system_prompt']}\n\nTone: {tone_instruction}"

    # Collect full response
    full_response = []
    async for chunk in stream_claude(api_messages, system_prompt):
        full_response.append(chunk)

    response_text = "".join(full_response)

    # Save assistant message
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), conv_id, "assistant", response_text, _now()),
    )
    conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (_now(), conv_id))
    conn.commit()
    conn.close()

    return {
        "conversation_id": conv_id,
        "content": response_text,
        "voice": persona.get("voice", "Samantha"),
        "speed": persona.get("speed", 180),
    }


# ---------------------------------------------------------------------------
# WebSocket for real-time voice chat
# ---------------------------------------------------------------------------


@app.websocket("/ws/chat")
async def websocket_chat(ws: WebSocket):
    await ws.accept()

    personas = load_personas()
    tones = load_tones()

    # Connection state
    conv_id: str | None = None
    persona_key = "default"
    tone_key = "balanced"

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type", "")

            # --- Setup / config messages ---
            if msg_type == "init":
                conv_id = msg.get("conversation_id")
                persona_key = msg.get("persona", "default")
                tone_key = msg.get("tone", "balanced")

                if not conv_id:
                    conv_id = str(uuid.uuid4())
                    now = _now()
                    conn = sqlite3.connect(str(DB_PATH))
                    conn.execute(
                        "INSERT INTO conversations (id, title, created_at, updated_at, persona, tone) VALUES (?, ?, ?, ?, ?, ?)",
                        (conv_id, "Voice Chat", now, now, persona_key, tone_key),
                    )
                    conn.commit()
                    conn.close()

                await ws.send_json({
                    "type": "init_ack",
                    "conversation_id": conv_id,
                    "persona": persona_key,
                    "tone": tone_key,
                })
                continue

            if msg_type == "config":
                persona_key = msg.get("persona", persona_key)
                tone_key = msg.get("tone", tone_key)
                await ws.send_json({"type": "config_ack", "persona": persona_key, "tone": tone_key})
                continue

            if msg_type == "ping":
                await ws.send_json({"type": "pong", "timestamp": _now()})
                continue

            # --- Chat message ---
            if msg_type == "message":
                user_text = msg.get("content", "").strip()
                if not user_text:
                    continue

                if not conv_id:
                    conv_id = str(uuid.uuid4())
                    now = _now()
                    conn = sqlite3.connect(str(DB_PATH))
                    conn.execute(
                        "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                        (conv_id, user_text[:50], now, now),
                    )
                    conn.commit()
                    conn.close()

                # Save user message
                conn = sqlite3.connect(str(DB_PATH))
                conn.execute(
                    "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), conv_id, "user", user_text, _now()),
                )
                conn.commit()

                # Load conversation history
                conn.row_factory = sqlite3.Row
                history_rows = conn.execute(
                    "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
                    (conv_id,),
                ).fetchall()
                conn.close()

                # Build messages for API
                api_messages = [{"role": r["role"], "content": r["content"]} for r in history_rows]

                # Build system prompt
                persona = personas.get(persona_key, personas["default"])
                tone_instruction = tones.get(tone_key, tones["balanced"])
                system_prompt = f"{persona['system_prompt']}\n\nTone: {tone_instruction}"

                # Stream response
                full_response = []
                await ws.send_json({"type": "response_start"})

                async for chunk in stream_claude(api_messages, system_prompt):
                    full_response.append(chunk)
                    await ws.send_json({"type": "chunk", "text": chunk})

                response_text = "".join(full_response)

                # Save assistant message
                conn = sqlite3.connect(str(DB_PATH))
                conn.execute(
                    "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), conv_id, "assistant", response_text, _now()),
                )
                conn.execute(
                    "UPDATE conversations SET updated_at = ?, title = CASE WHEN title = 'Voice Chat' OR title = 'New Conversation' THEN ? ELSE title END WHERE id = ?",
                    (_now(), user_text[:50], conv_id),
                )
                conn.commit()
                conn.close()

                # Send completion with voice config
                await ws.send_json({
                    "type": "response_end",
                    "full_text": response_text,
                    "voice": persona.get("voice", "Samantha"),
                    "speed": persona.get("speed", 180),
                })

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    port = int(os.getenv("CLAUDE_VOICE_PORT", "7777"))
    uvicorn.run(app, host="0.0.0.0", port=port)
