"""Claude Voice — FastAPI backend with WebSocket streaming."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.getenv("CLAUDE_VOICE_DATA", str(Path.home() / ".claude-voice")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "conversations.db"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DEFAULT_MODEL = os.getenv("CLAUDE_VOICE_MODEL", "claude-sonnet-4-5-20250929")
MAX_TOKENS = int(os.getenv("CLAUDE_VOICE_MAX_TOKENS", "1024"))

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
# Claude API streaming
# ---------------------------------------------------------------------------


async def stream_claude(
    messages: list[dict],
    system_prompt: str,
    model: str = DEFAULT_MODEL,
) -> AsyncGenerator[str, None]:
    """Stream response chunks from Claude API."""
    if not ANTHROPIC_API_KEY:
        yield "Error: ANTHROPIC_API_KEY not set. Please set it in your environment."
        return

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
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
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=body,
        ) as response:
            if response.status_code != 200:
                error_body = await response.aread()
                yield f"API error {response.status_code}: {error_body.decode()[:200]}"
                return

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


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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
    return {"status": "ok", "timestamp": _now(), "model": DEFAULT_MODEL}


@app.get("/api/personas")
async def get_personas():
    return load_personas()


@app.get("/api/tones")
async def get_tones():
    return load_tones()


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

    port = int(os.getenv("CLAUDE_VOICE_PORT", "7777"))
    uvicorn.run(app, host="0.0.0.0", port=port)
