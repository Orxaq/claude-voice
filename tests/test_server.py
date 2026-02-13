"""Tests for Claude Voice server."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

# Set test data dir before import
os.environ["CLAUDE_VOICE_DATA"] = "/tmp/claude-voice-test"

from backend.server import (
    DB_PATH,
    app,
    init_db,
    load_personas,
    load_tones,
    _now,
)


@pytest.fixture(autouse=True)
def clean_db(tmp_path):
    """Use fresh DB for each test."""
    os.environ["CLAUDE_VOICE_DATA"] = str(tmp_path)
    # Reimport to pick up new path
    import backend.server as srv
    srv.DB_PATH = tmp_path / "conversations.db"
    srv.DATA_DIR = tmp_path
    srv.init_db()
    yield
    # Cleanup
    if srv.DB_PATH.exists():
        srv.DB_PATH.unlink()


@pytest.fixture
def client():
    return TestClient(app)


class TestHealth:
    def test_health_endpoint(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "timestamp" in data
        assert "model" in data


class TestPersonas:
    def test_get_personas(self, client):
        resp = client.get("/api/personas")
        assert resp.status_code == 200
        personas = resp.json()
        assert "default" in personas
        assert "copilot" in personas
        assert "narrator" in personas
        assert "executive" in personas
        assert "friend" in personas

    def test_persona_has_required_fields(self, client):
        resp = client.get("/api/personas")
        for key, persona in resp.json().items():
            assert "name" in persona, f"{key} missing name"
            assert "system_prompt" in persona, f"{key} missing system_prompt"
            assert "voice" in persona, f"{key} missing voice"
            assert "speed" in persona, f"{key} missing speed"

    def test_load_personas_returns_defaults(self):
        personas = load_personas()
        assert len(personas) >= 4


class TestTones:
    def test_get_tones(self, client):
        resp = client.get("/api/tones")
        assert resp.status_code == 200
        tones = resp.json()
        assert "concise" in tones
        assert "balanced" in tones
        assert "detailed" in tones


class TestConversations:
    def test_create_conversation(self, client):
        resp = client.post("/api/conversations")
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["title"] == "New Conversation"

    def test_list_conversations(self, client):
        # Create two conversations
        client.post("/api/conversations")
        client.post("/api/conversations")

        resp = client.get("/api/conversations")
        assert resp.status_code == 200
        convs = resp.json()
        assert len(convs) == 2

    def test_list_conversations_returns_all(self, client):
        c1 = client.post("/api/conversations").json()
        c2 = client.post("/api/conversations").json()

        resp = client.get("/api/conversations")
        convs = resp.json()
        ids = {c["id"] for c in convs}
        assert c1["id"] in ids
        assert c2["id"] in ids


class TestMessages:
    def test_get_messages_empty(self, client):
        conv = client.post("/api/conversations").json()
        resp = client.get(f"/api/conversations/{conv['id']}/messages")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_messages_nonexistent_conv(self, client):
        resp = client.get("/api/conversations/nonexistent/messages")
        assert resp.status_code == 200
        assert resp.json() == []


class TestOfflineQueue:
    def test_queue_message(self, client):
        resp = client.post("/api/offline-queue", json={
            "conversation_id": "test-conv",
            "content": "Hello from offline",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["queued"] is True
        assert "id" in data


class TestHTTPChat:
    def test_chat_empty_message(self, client):
        resp = client.post("/api/chat", json={"content": ""})
        assert resp.status_code == 400

    def test_chat_creates_conversation(self, client):
        # This will fail at Claude API (no key) but should create conversation
        resp = client.post("/api/chat", json={
            "content": "hello",
            "persona": "default",
            "tone": "concise",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "conversation_id" in data
        assert "content" in data

    def test_chat_with_existing_conversation(self, client):
        conv = client.post("/api/conversations").json()
        resp = client.post("/api/chat", json={
            "content": "hello",
            "conversation_id": conv["id"],
        })
        assert resp.status_code == 200


class TestFrontend:
    def test_serve_index(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Claude Voice" in resp.text

    def test_serve_manifest(self, client):
        resp = client.get("/manifest.json")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Claude Voice"

    def test_serve_service_worker(self, client):
        resp = client.get("/sw.js")
        assert resp.status_code == 200
        assert "claude-voice" in resp.text


class TestDatabase:
    def test_init_creates_tables(self, clean_db, tmp_path):
        import backend.server as srv
        conn = sqlite3.connect(str(srv.DB_PATH))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        conn.close()
        table_names = {t[0] for t in tables}
        assert "conversations" in table_names
        assert "messages" in table_names
        assert "offline_queue" in table_names


class TestNow:
    def test_now_format(self):
        ts = _now()
        assert ts.endswith("Z")
        assert "T" in ts
