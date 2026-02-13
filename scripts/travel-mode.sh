#!/bin/bash
# Travel Mode — optimizes Claude Voice for road trip use
# Run this before a trip to set everything up
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Claude Voice — Travel Mode Setup ==="
echo ""

# 1. Check API key
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    if [ -f "$PROJECT_DIR/.env" ]; then
        set -a
        source "$PROJECT_DIR/.env"
        set +a
    fi
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "[FAIL] ANTHROPIC_API_KEY not set"
    echo "  Create $PROJECT_DIR/.env with your key"
    exit 1
fi
echo "[OK] API key configured"

# 2. Check/install dependencies
if [ ! -d "$PROJECT_DIR/.venv" ]; then
    echo "[...] Installing dependencies..."
    python3 -m venv "$PROJECT_DIR/.venv"
    "$PROJECT_DIR/.venv/bin/pip" install -q -r "$PROJECT_DIR/requirements.txt"
fi
echo "[OK] Dependencies installed"

# 3. Get local IP
LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || echo "not connected")
echo "[OK] Local IP: $LOCAL_IP"

# 4. Test Claude API connectivity
echo "[...] Testing Claude API..."
API_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "x-api-key: $ANTHROPIC_API_KEY" \
    -H "anthropic-version: 2023-06-01" \
    https://api.anthropic.com/v1/messages \
    -d '{"model":"claude-sonnet-4-5-20250929","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' \
    -H "content-type: application/json" 2>/dev/null || echo "000")

if [ "$API_STATUS" = "200" ]; then
    echo "[OK] Claude API reachable"
elif [ "$API_STATUS" = "000" ]; then
    echo "[WARN] Claude API not reachable (offline?) — will queue messages"
else
    echo "[WARN] Claude API returned $API_STATUS"
fi

# 5. Cloud Run deployment check
if command -v gcloud &>/dev/null; then
    CLOUD_URL=$(gcloud run services describe claude-voice --region us-central1 --format 'value(status.url)' 2>/dev/null || echo "")
    if [ -n "$CLOUD_URL" ]; then
        echo "[OK] Cloud backup: $CLOUD_URL"
    else
        echo "[INFO] No cloud deployment yet — run deploy-cloud-run.sh for laptop-off resilience"
    fi
fi

# 6. Create LaunchAgent for auto-start
PLIST="$HOME/Library/LaunchAgents/com.orxaq.claude-voice.plist"
cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.orxaq.claude-voice</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PROJECT_DIR}/.venv/bin/python</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>backend.server:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>7777</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ANTHROPIC_API_KEY</key>
        <string>${ANTHROPIC_API_KEY}</string>
        <key>CLAUDE_VOICE_MODEL</key>
        <string>claude-sonnet-4-5-20250929</string>
    </dict>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>NetworkState</key>
        <true/>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${HOME}/.claude-voice/server.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME}/.claude-voice/server.err</string>
    <key>Nice</key>
    <integer>5</integer>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
EOF

# Load the agent
launchctl bootout "gui/$(id -u)/com.orxaq.claude-voice" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "[OK] LaunchAgent installed — server starts on boot"

# 7. Verify server is running
sleep 2
if curl -s http://localhost:7777/api/health | grep -q '"ok"'; then
    echo "[OK] Server running on port 7777"
else
    echo "[WARN] Server may still be starting..."
fi

echo ""
echo "=== Travel Mode Active ==="
echo ""
echo "On your phone, open:"
echo "  http://$LOCAL_IP:7777"
echo ""
echo "Or use the cloud URL if deployed."
echo ""
echo "Tips:"
echo "  - Add to Home Screen for app-like experience"
echo "  - Voice works even with spotty connection (messages queue)"
echo "  - Switch personas in Settings for different vibes"
echo "  - Server restarts automatically if it crashes"
