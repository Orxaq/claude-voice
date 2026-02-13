#!/bin/bash
# Start Claude Voice server
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_DIR/.venv"

# Create venv if needed
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip
    "$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"
fi

# Check for API key
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    # Try to load from .env
    if [ -f "$PROJECT_DIR/.env" ]; then
        set -a
        source "$PROJECT_DIR/.env"
        set +a
    fi
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "ERROR: ANTHROPIC_API_KEY not set."
    echo "Set it via: export ANTHROPIC_API_KEY=sk-ant-..."
    echo "Or create a .env file in $PROJECT_DIR"
    exit 1
fi

PORT="${CLAUDE_VOICE_PORT:-7777}"

echo "Starting Claude Voice on http://localhost:$PORT"
echo "Open this URL on your phone (same network) to use voice mode"
echo ""

# Get local IP for phone access
LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || echo "localhost")
echo "Phone URL: http://$LOCAL_IP:$PORT"
echo ""

exec "$VENV_DIR/bin/python" -m uvicorn backend.server:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --reload \
    --app-dir "$PROJECT_DIR"
