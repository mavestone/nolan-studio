#!/usr/bin/env bash
# Nolan Studio — Mac installer
# Usage: ./install.sh
set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────
RESET=$'\033[0m'; BOLD=$'\033[1m'
GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RED=$'\033[31m'

say()  { echo "${CYAN}▸${RESET} $*"; }
ok()   { echo "${GREEN}✓${RESET} $*"; }
warn() { echo "${YELLOW}!${RESET} $*"; }
err()  { echo "${RED}✗${RESET} $*"; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "${BOLD}─── Nolan Studio install ───${RESET}"
echo "  Target: $ROOT"
echo

# ── 1. Ensure macOS ──────────────────────────────────────────────────
[[ "$(uname -s)" == "Darwin" ]] || err "This installer is macOS-only for now."

# ── 2. Ensure Homebrew ───────────────────────────────────────────────
if ! command -v brew >/dev/null 2>&1; then
    warn "Homebrew not found. Installing it now…"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
ok "Homebrew ready"

# ── 3. Ensure ffmpeg ─────────────────────────────────────────────────
if ! command -v ffmpeg >/dev/null 2>&1; then
    say "Installing ffmpeg (required for thumbnails + audio)…"
    brew install ffmpeg
fi
ok "ffmpeg ready"

# ── 4. Ensure Python 3.11+ ──────────────────────────────────────────
PY=""
for c in python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then
        ver=$("$c" -c "import sys; print(sys.version_info[:2] >= (3, 11))" 2>/dev/null || echo "False")
        if [[ "$ver" == "True" ]]; then PY="$c"; break; fi
    fi
done
if [[ -z "$PY" ]]; then
    warn "Python 3.11+ not found. Installing via Homebrew…"
    brew install python@3.11
    PY="python3.11"
fi
ok "Using Python: $(command -v "$PY")"

# ── 5. Build venv ────────────────────────────────────────────────────
if [[ ! -d ".venv" ]]; then
    say "Creating virtualenv at .venv/…"
    "$PY" -m venv .venv
fi
# shellcheck source=/dev/null
source .venv/bin/activate
ok "venv active"

# ── 6. Install Python deps ──────────────────────────────────────────
say "Installing Python packages (this can take a few minutes the first time)…"
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
ok "Dependencies installed"

# ── 7. Bootstrap .env if missing ─────────────────────────────────────
if [[ ! -f ".env" ]]; then
    cat > .env <<'EOF'
# API keys — Nolan stores them here. You can also paste them in Settings (gear icon).
# Get keys from:
#   Anthropic  https://console.anthropic.com/
#   Groq       https://console.groq.com/keys
#   Gemini     https://aistudio.google.com/apikey

ANTHROPIC_API_KEY=
GROQ_API_KEY=
GEMINI_API_KEY=
EOF
    ok "Created blank .env  — open Nolan and use the gear icon to add keys"
else
    ok ".env preserved"
fi

# ── 8. Build the Desktop launcher (.app) ────────────────────────────
LAUNCHER="$HOME/Desktop/Nolan.app"
if [[ ! -d "$LAUNCHER" ]]; then
    say "Creating Desktop launcher…"
    mkdir -p "$LAUNCHER/Contents/MacOS"
    cat > "$LAUNCHER/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>Nolan</string>
  <key>CFBundleIdentifier</key><string>studio.nolan</string>
  <key>CFBundleName</key><string>Nolan</string>
  <key>LSUIElement</key><false/>
</dict></plist>
EOF
    cat > "$LAUNCHER/Contents/MacOS/Nolan" <<EOF
#!/bin/bash
cd "$ROOT"
lsof -ti :8765 | xargs kill -9 2>/dev/null
sleep 0.5
osascript <<APPLESCRIPT
tell application "Terminal"
    activate
    set w to do script "cd '$ROOT' && source .venv/bin/activate && python3 main.py"
    set custom title of w to "Nolan"
end tell
APPLESCRIPT
sleep 3
open http://localhost:8765/
EOF
    chmod +x "$LAUNCHER/Contents/MacOS/Nolan"
    ok "Launcher: $LAUNCHER"
else
    ok "Launcher already exists"
fi

# ── 9. Done ──────────────────────────────────────────────────────────
echo
echo "${BOLD}${GREEN}Nolan is installed.${RESET}"
echo
echo "Next steps:"
echo "  1. Double-click ${BOLD}Nolan.app${RESET} on your Desktop."
echo "  2. The first-run wizard will walk you through API keys + project setup."
echo "  3. You can also import a project from a friend via the ${BOLD}Import${RESET} button."
echo
echo "Manual start: ${CYAN}cd $ROOT && source .venv/bin/activate && python3 main.py${RESET}"
echo
