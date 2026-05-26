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
# Points to the repo where Python + venv live. The .app stays small.
$NEED_SUDO tee "$APP/Contents/MacOS/Nolan" >/dev/null <<EOF
#!/bin/bash
# Nolan launcher — starts the FastAPI server then opens the browser.

NOLAN_ROOT="$ROOT"

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

# Activate venv if present
if [ -f ".venv/bin/activate" ]; then
    # shellcheck source=/dev/null
    source .venv/bin/activate
fi

# Open Nolan in a Terminal window so logs are visible + user can Cmd+Q to quit
osascript <<APPLESCRIPT 2>/dev/null
tell application "Terminal"
    activate
    set w to do script "cd '\$NOLAN_ROOT' && [ -f .venv/bin/activate ] && source .venv/bin/activate; python3 main.py"
    set custom title of w to "Nolan"
end tell
APPLESCRIPT

# Wait for the server to boot, then open the browser
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf http://localhost:8765/ >/dev/null 2>&1; then
        break
    fi
    sleep 0.8
done
open "http://localhost:8765/"
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
