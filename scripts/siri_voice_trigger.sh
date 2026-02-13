#!/bin/bash
# Called by Siri Shortcut "Switch to Voice"
osascript -e '
tell application "Terminal"
    activate
    do script "voice"
end tell
'
