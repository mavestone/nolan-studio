#!/usr/bin/env bash
# make-app.sh — Build a proper Mac .app bundle for Nolan.
# Called by install.sh; can also be run standalone.
#
# Usage:
#   ./make-app.sh                       # builds in current dir as ./Nolan.app
#   ./make-app.sh /Applications         # builds + installs to /Applications/Nolan.app
#   ./make-app.sh ~/Desktop             # builds + installs to ~/Desktop/Nolan.app
#
# Uses a Cocoa Swift launcher (launcher/nolan-launcher) so the app registers
# properly with WindowServer — gets the dock indicator dot, no infinite bouncing.
# If the prebuilt binary is missing AND swiftc is available, recompiles it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-$ROOT}"
APP="$TARGET_DIR/Nolan.app"

# Ensure the Swift launcher binary exists (rebuild if user has swiftc)
LAUNCHER_BIN="$ROOT/launcher/nolan-launcher"
if [[ ! -f "$LAUNCHER_BIN" ]] && command -v swiftc >/dev/null 2>&1; then
    echo "▸ Compiling Cocoa launcher…"
    (
        cd "$ROOT/launcher"
        swiftc -O -target arm64-apple-macos11  main.swift -o nolan-launcher-arm64
        swiftc -O -target x86_64-apple-macos11 main.swift -o nolan-launcher-x86_64
        lipo -create nolan-launcher-arm64 nolan-launcher-x86_64 -output nolan-launcher
        rm -f nolan-launcher-arm64 nolan-launcher-x86_64
    )
fi
[[ -f "$LAUNCHER_BIN" ]] || { echo "✗ Missing launcher/nolan-launcher and no swiftc available."; exit 1; }

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

# ── Native Cocoa launcher (Swift) ──
# Copy the prebuilt universal binary into the bundle. It properly registers
# with NSApplication so the dock dot lights up and the bounce stops.
$NEED_SUDO cp "$LAUNCHER_BIN" "$APP/Contents/MacOS/Nolan"
$NEED_SUDO chmod +x "$APP/Contents/MacOS/Nolan"

# Write the repo path next to the binary so the Swift launcher can find main.py
$NEED_SUDO bash -c "echo '$ROOT' > '$APP/Contents/Resources/nolan-root.txt'"

# Legacy bash launcher (kept for emergency fallback — never executed by .app)
$NEED_SUDO tee "$APP/Contents/MacOS/.legacy-bash-launcher" >/dev/null <<EOF
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

# Detect TRUE hardware arch.
# `uname -m` is unreliable: returns x86_64 when shell runs under Rosetta even on
# Apple Silicon. Use sysctl to ask the kernel about the actual CPU.
if /usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null | grep -q "^1\$"; then
    HOST_ARCH="arm64"
else
    HOST_ARCH="x86_64"
fi

# We'll try the "real" arch first, then fall back to the other one if dlopen
# fails (covers users who installed wheels under Rosetta).
ARCH_ORDER=("\$HOST_ARCH")
if [ "\$HOST_ARCH" = "arm64" ]; then ARCH_ORDER+=("x86_64"); else ARCH_ORDER+=("arm64"); fi

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
# Set up logs early so the probe results are captured
mkdir -p "\$LOG_DIR"
DIAG_FILE="\$LOG_DIR/launcher.log"
{
    echo "── Nolan launcher diagnostic ── \$(date) ──"
    echo "PATH: \$PATH"
    echo "Working dir: \$(pwd)"
    echo "User: \$(whoami)"
    echo
} >"\$DIAG_FILE"

echo "Detected hardware arch: \$HOST_ARCH" >>"\$DIAG_FILE"
echo "Will try archs in order: \${ARCH_ORDER[*]}" >>"\$DIAG_FILE"
echo >>"\$DIAG_FILE"

PYTHON_BIN=""
CHOSEN_ARCH=""
for cand in "\${PYTHON_CANDIDATES[@]}"; do
    if [ ! -x "\$cand" ]; then
        echo "✗ \$cand : not executable / missing" >>"\$DIAG_FILE"
        continue
    fi
    for try_arch in "\${ARCH_ORDER[@]}"; do
        PROBE_OUT=\$(arch "-\${try_arch}" "\$cand" -c "import dotenv, fastapi, faster_whisper; print('ok')" 2>&1)
        if [ "\$PROBE_OUT" = "ok" ]; then
            echo "✓ \$cand : works as \$try_arch" >>"\$DIAG_FILE"
            PYTHON_BIN="\$cand"
            CHOSEN_ARCH="\$try_arch"
            break 2
        else
            echo "  - \$cand as \$try_arch failed: \${PROBE_OUT##*$'\n'}" >>"\$DIAG_FILE"
        fi
    done
done

if [ -n "\$CHOSEN_ARCH" ]; then
    ARCH_PREFIX=(arch "-\${CHOSEN_ARCH}")
else
    ARCH_PREFIX=()
fi

if [ -z "\$PYTHON_BIN" ]; then
    osascript -e "display dialog \"Nolan can't find a Python with its dependencies.\n\nDiagnostic log: \$DIAG_FILE\n\nQuick fix:  cd '\$NOLAN_ROOT' && ./install.sh\" buttons {\"OK\"} default button 1 with icon stop"
    exit 1
fi

# Rotate log
[ -f "\$LOG_FILE" ] && mv "\$LOG_FILE" "\$LOG_FILE.prev"

# Record which Python we're using (helps debugging)
echo "Nolan launcher using: \$PYTHON_BIN" >"\$LOG_FILE"
echo "──────────────────────────────────" >>"\$LOG_FILE"

# Start the server in background, log to file. Capture PID.
"\${ARCH_PREFIX[@]}" "\$PYTHON_BIN" main.py >>"\$LOG_FILE" 2>&1 &
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
$NEED_SUDO chmod +x "$APP/Contents/MacOS/.legacy-bash-launcher"

# ── Icon ──
if [[ -f "$ROOT/nolan.icns" ]]; then
    $NEED_SUDO cp "$ROOT/nolan.icns" "$APP/Contents/Resources/nolan.icns"
fi

# ── Touch so Finder picks up new icon ──
$NEED_SUDO touch "$APP"

# Refresh Finder's icon cache
killall Finder 2>/dev/null || true

echo "✓ $APP ready"
