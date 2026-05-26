#!/usr/bin/env bash
# make-app.sh — Build a proper Mac .app bundle for Nolan.
# Called by install.sh; can also be run standalone.
#
# Usage:
#   ./make-app.sh                       # builds in current dir as ./Nolan.app
#   ./make-app.sh /Applications         # builds + installs to /Applications/Nolan.app
#   ./make-app.sh ~/Desktop             # builds + installs to ~/Desktop/Nolan.app
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-$ROOT}"
APP="$TARGET_DIR/Nolan.app"

# Need /Applications? Use sudo for install
NEED_SUDO=""
if [[ "$TARGET_DIR" == "/Applications" ]] && [[ ! -w "/Applications" ]]; then
    NEED_SUDO="sudo"
fi

echo "▸ Building $APP"

# Remove old bundle if exists
[[ -d "$APP" ]] && $NEED_SUDO rm -rf "$APP"

$NEED_SUDO mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ── Info.plist ──
$NEED_SUDO tee "$APP/Contents/Info.plist" >/dev/null <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Nolan</string>
    <key>CFBundleDisplayName</key>
    <string>Nolan</string>
    <key>CFBundleIdentifier</key>
    <string>studio.nolan</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>Nolan</string>
    <key>CFBundleIconFile</key>
    <string>nolan.icns</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleSignature</key>
    <string>NLST</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.video</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSHumanReadableCopyright</key>
    <string>Nolan Studio</string>
</dict>
</plist>
EOF

# ── Launcher script ──
# Stays attached to python3 so macOS shows the running-dot under Nolan in the Dock.
# Quitting via Dock right-click → Quit (or Cmd+Q after focusing the app) cleanly
# kills the Python server.
$NEED_SUDO tee "$APP/Contents/MacOS/Nolan" >/dev/null <<EOF
#!/bin/bash
# Nolan launcher

NOLAN_ROOT="$ROOT"
LOG_DIR="\$HOME/Library/Logs/Nolan"
LOG_FILE="\$LOG_DIR/server.log"

mkdir -p "\$LOG_DIR"

if [ ! -d "\$NOLAN_ROOT" ]; then
    osascript -e 'display dialog "Nolan source folder is missing at:\n\n$ROOT\n\nRe-run install.sh from the repo." buttons {"OK"} default button 1 with icon stop'
    exit 1
fi

cd "\$NOLAN_ROOT"

# Kill any previous instance on port 8765
EXISTING=\$(lsof -ti :8765 2>/dev/null)
if [ -n "\$EXISTING" ]; then
    kill -9 \$EXISTING 2>/dev/null || true
    sleep 0.5
fi

# .app launches have a minimal PATH — add the usual Homebrew & Python locations
export PATH="/usr/local/bin:/opt/homebrew/bin:/Library/Frameworks/Python.framework/Versions/3.11/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:\$PATH"

# Find a Python that has our deps. Order:
#   1. project venv (.venv/bin/python3)
#   2. Framework Python 3.11/3.12 (typical brew/python.org)
#   3. /opt/homebrew/bin/python3 (Apple Silicon brew)
#   4. /usr/local/bin/python3 (Intel brew)
#   5. whatever python3 is on PATH
PYTHON_CANDIDATES=(
    "\$NOLAN_ROOT/.venv/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3"
    "\$(command -v python3)"
)
PYTHON_BIN=""
for cand in "\${PYTHON_CANDIDATES[@]}"; do
    if [ -x "\$cand" ] && "\$cand" -c "import dotenv, fastapi, faster_whisper" >/dev/null 2>&1; then
        PYTHON_BIN="\$cand"
        break
    fi
done

if [ -z "\$PYTHON_BIN" ]; then
    osascript -e 'display dialog "Nolan needs Python with its dependencies installed.\n\nOpen Terminal and run:\n\n  cd '"\$NOLAN_ROOT"' && ./install.sh\n\nThen relaunch Nolan." buttons {"OK"} default button 1 with icon stop'
    exit 1
fi

# Rotate log
[ -f "\$LOG_FILE" ] && mv "\$LOG_FILE" "\$LOG_FILE.prev"

# Record which Python we're using (helps debugging)
echo "Nolan launcher using: \$PYTHON_BIN" >"\$LOG_FILE"
echo "──────────────────────────────────" >>"\$LOG_FILE"

# Start the server in background, log to file. Capture PID.
"\$PYTHON_BIN" main.py >>"\$LOG_FILE" 2>&1 &
SERVER_PID=\$!

# Ensure python is killed when the launcher quits (Cmd+Q on the app, etc.)
trap 'kill \$SERVER_PID 2>/dev/null; wait \$SERVER_PID 2>/dev/null; exit 0' SIGTERM SIGINT EXIT

# Wait for the server to become reachable, then open the browser
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if curl -sf http://localhost:8765/ >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 \$SERVER_PID 2>/dev/null; then
        # Server died — show last few log lines and bail
        osascript -e "display dialog \"Nolan failed to start. See \$LOG_FILE for details.\" buttons {\"OK\"} default button 1 with icon stop"
        exit 1
    fi
    sleep 0.7
done
open "http://localhost:8765/"

# Stay attached — this is what gives Nolan the 'running dot' in the Dock.
# Block on the python child; exit when it does.
wait \$SERVER_PID
EOF

$NEED_SUDO chmod +x "$APP/Contents/MacOS/Nolan"

# ── Icon ──
if [[ -f "$ROOT/nolan.icns" ]]; then
    $NEED_SUDO cp "$ROOT/nolan.icns" "$APP/Contents/Resources/nolan.icns"
fi

# ── Touch so Finder picks up new icon ──
$NEED_SUDO touch "$APP"

# Refresh Finder's icon cache
killall Finder 2>/dev/null || true

echo "✓ $APP ready"
