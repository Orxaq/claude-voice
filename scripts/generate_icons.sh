#!/bin/bash
# Generate PWA icons using a simple SVG
set -euo pipefail

FRONTEND_DIR="$(cd "$(dirname "$0")/../frontend" && pwd)"

# Create a simple SVG icon
cat > /tmp/claude-voice-icon.svg << 'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="96" fill="#6366f1"/>
  <circle cx="256" cy="200" r="60" fill="white"/>
  <rect x="236" y="260" width="40" height="80" rx="20" fill="white"/>
  <path d="M176 320 q80 100 160 0" stroke="white" stroke-width="12" fill="none"/>
  <circle cx="176" cy="200" r="8" fill="white" opacity="0.5"/>
  <circle cx="336" cy="200" r="8" fill="white" opacity="0.5"/>
  <circle cx="156" cy="240" r="5" fill="white" opacity="0.3"/>
  <circle cx="356" cy="240" r="5" fill="white" opacity="0.3"/>
</svg>
SVG

# Generate PNGs using sips (macOS built-in)
if command -v rsvg-convert &>/dev/null; then
    rsvg-convert -w 180 -h 180 /tmp/claude-voice-icon.svg > "$FRONTEND_DIR/icon-180.png"
    rsvg-convert -w 192 -h 192 /tmp/claude-voice-icon.svg > "$FRONTEND_DIR/icon-192.png"
    rsvg-convert -w 512 -h 512 /tmp/claude-voice-icon.svg > "$FRONTEND_DIR/icon-512.png"
    echo "Icons generated"
else
    # Fallback: create minimal 1x1 PNGs (browser will still work)
    printf '\x89PNG\r\n\x1a\n' > "$FRONTEND_DIR/icon-180.png"
    cp "$FRONTEND_DIR/icon-180.png" "$FRONTEND_DIR/icon-192.png"
    cp "$FRONTEND_DIR/icon-180.png" "$FRONTEND_DIR/icon-512.png"
    echo "Placeholder icons created (install librsvg for proper icons: brew install librsvg)"
fi
