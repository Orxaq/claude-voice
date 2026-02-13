#!/bin/bash
# One-time setup for Claude Voice
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Claude Voice Setup ==="
echo ""

# Check for .env
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "No .env file found."
    echo ""
    echo "You need an Anthropic API key."
    echo "Get one at: https://console.anthropic.com/settings/keys"
    echo ""
    read -rp "Paste your API key (sk-ant-...): " API_KEY

    if [ -z "$API_KEY" ]; then
        echo "No key provided. You can manually create $PROJECT_DIR/.env"
        cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
        exit 1
    fi

    cat > "$PROJECT_DIR/.env" << EOF
ANTHROPIC_API_KEY=$API_KEY
CLAUDE_VOICE_PORT=7777
CLAUDE_VOICE_MODEL=claude-sonnet-4-5-20250929
CLAUDE_VOICE_MAX_TOKENS=1024
EOF
    echo "[OK] .env created"
else
    echo "[OK] .env exists"
fi

# Install deps
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    echo "[...] Creating virtual environment..."
    python3 -m venv "$PROJECT_DIR/.venv"
    "$PROJECT_DIR/.venv/bin/pip" install --upgrade pip -q
    "$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt" -q
fi
echo "[OK] Dependencies installed"

# Test
echo "[...] Running tests..."
"$PROJECT_DIR/.venv/bin/python" -m pytest "$PROJECT_DIR/tests/" -q 2>&1 || true

echo ""
echo "Setup complete! Next steps:"
echo "  1. Run: bash $SCRIPT_DIR/travel-mode.sh"
echo "  2. Open http://localhost:7777 on your phone"
echo "  3. Add to Home Screen for PWA experience"
