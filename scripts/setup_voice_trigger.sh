#!/bin/bash
# Set up "Switch to Voice" Siri Shortcut and Voice Control command.
#
# After running this:
#   1. Say "Switch to Voice" to Siri → opens voice mode
#   2. Enable Voice Control (System Settings → Accessibility → Voice Control)
#      and say "switch to voice" hands-free anytime
#   3. Type "voice" in any terminal

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VOICE_SCRIPT="$SCRIPT_DIR/voice_loop.py"

echo "═══════════════════════════════════════"
echo "  Setting up Voice Triggers"
echo "═══════════════════════════════════════"

# 1. Create a small AppleScript app that launches voice mode
APP_DIR="$HOME/Applications/SwitchToVoice.app/Contents/MacOS"
mkdir -p "$APP_DIR"
mkdir -p "$HOME/Applications/SwitchToVoice.app/Contents"

# Info.plist
cat > "$HOME/Applications/SwitchToVoice.app/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>SwitchToVoice</string>
    <key>CFBundleIdentifier</key>
    <string>com.orxaq.switch-to-voice</string>
    <key>CFBundleName</key>
    <string>Switch to Voice</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
PLIST

# Executable script
cat > "$APP_DIR/SwitchToVoice" << SCRIPT
#!/bin/bash
osascript -e '
tell application "Terminal"
    activate
    do script "python3 $VOICE_SCRIPT --persona copilot"
end tell
'
SCRIPT
chmod +x "$APP_DIR/SwitchToVoice"

echo "  ✓ Created SwitchToVoice.app"

# 2. Create Siri Shortcut via shortcuts CLI
# Note: The shortcuts CLI can import .shortcut files but creating from scratch
# requires the Shortcuts app. We'll create an Automator-style workflow instead.

# Create a shell script that Siri Shortcuts can call
cat > "$SCRIPT_DIR/siri_voice_trigger.sh" << 'SIRI'
#!/bin/bash
# Called by Siri Shortcut "Switch to Voice"
osascript -e '
tell application "Terminal"
    activate
    do script "voice"
end tell
'
SIRI
chmod +x "$SCRIPT_DIR/siri_voice_trigger.sh"

echo "  ✓ Created Siri trigger script"

# 3. Try to create the Siri Shortcut
# First check if a shortcut already exists
if shortcuts list 2>/dev/null | grep -q "Switch to Voice"; then
    echo "  ✓ Siri Shortcut 'Switch to Voice' already exists"
else
    echo "  ℹ To create the Siri Shortcut:"
    echo "    1. Open Shortcuts app"
    echo "    2. Create new shortcut named 'Switch to Voice'"
    echo "    3. Add action: 'Run Shell Script'"
    echo "    4. Script: $SCRIPT_DIR/siri_voice_trigger.sh"
    echo "    5. Enable 'Add to Siri' with phrase 'Switch to Voice'"
fi

# 4. macOS Voice Control custom command
# Voice Control commands are stored in:
# ~/Library/Speech/CustomCommands/
VC_DIR="$HOME/Library/Speech"
mkdir -p "$VC_DIR"

echo ""
echo "  ℹ To enable Voice Control trigger:"
echo "    1. System Settings → Accessibility → Voice Control → ON"
echo "    2. Click 'Commands...' → Custom"
echo "    3. Add command: 'switch to voice'"
echo "    4. Action: Open App → SwitchToVoice"
echo ""
echo "  Or just say 'Open Switch to Voice' with Voice Control on."
echo ""

# 5. Register URL scheme for deep linking
# This lets anything on the system trigger voice mode via:
#   open orxaq-voice://start
HANDLER_DIR="$HOME/Applications/VoiceHandler.app/Contents/MacOS"
mkdir -p "$HANDLER_DIR"
mkdir -p "$HOME/Applications/VoiceHandler.app/Contents"

cat > "$HOME/Applications/VoiceHandler.app/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>VoiceHandler</string>
    <key>CFBundleIdentifier</key>
    <string>com.orxaq.voice-handler</string>
    <key>CFBundleName</key>
    <string>Voice Handler</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>CFBundleURLTypes</key>
    <array>
        <dict>
            <key>CFBundleURLName</key>
            <string>Orxaq Voice</string>
            <key>CFBundleURLSchemes</key>
            <array>
                <string>orxaq-voice</string>
            </array>
        </dict>
    </array>
</dict>
</plist>
PLIST

cat > "$HANDLER_DIR/VoiceHandler" << SCRIPT
#!/bin/bash
osascript -e '
tell application "Terminal"
    activate
    do script "voice"
end tell
'
SCRIPT
chmod +x "$HANDLER_DIR/VoiceHandler"

# Register the URL scheme
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$HOME/Applications/VoiceHandler.app" 2>/dev/null || true

echo "  ✓ Registered orxaq-voice:// URL scheme"
echo "    Test: open orxaq-voice://start"

echo ""
echo "═══════════════════════════════════════"
echo "  Voice Triggers Ready!"
echo "═══════════════════════════════════════"
echo ""
echo "  Terminal:        voice"
echo "  Short:           v"
echo "  Coder mode:      vc"
echo "  Executive:       ve"
echo "  Siri:            'Switch to Voice'"
echo "  Voice Control:   'switch to voice'"
echo "  URL:             open orxaq-voice://start"
echo "  Spotlight:       'Switch to Voice'"
echo ""
