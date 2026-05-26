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

# ── 8. Build & install Nolan.app ────────────────────────────────────
echo
echo "${BOLD}Where would you like Nolan.app installed?${RESET}"
echo "  1) /Applications        ${YELLOW}(recommended — shows up in Launchpad & Spotlight)${RESET}"
echo "  2) ~/Applications       (per-user, no sudo)"
echo "  3) ~/Desktop"
echo "  4) Skip — I'll run from Terminal"
read -p "  Choose [1-4, default 1]: " CHOICE
CHOICE="${CHOICE:-1}"

case "$CHOICE" in
    1) INSTALL_DIR="/Applications" ;;
    2) INSTALL_DIR="$HOME/Applications" ;;
    3) INSTALL_DIR="$HOME/Desktop" ;;
    4) INSTALL_DIR="" ;;
    *) INSTALL_DIR="/Applications" ;;
esac

if [[ -n "$INSTALL_DIR" ]]; then
    mkdir -p "$INSTALL_DIR" 2>/dev/null || true
    bash "$ROOT/make-app.sh" "$INSTALL_DIR"
    ok "Installed: $INSTALL_DIR/Nolan.app"
else
    say "Skipped — start manually with: cd $ROOT && source .venv/bin/activate && python3 main.py"
fi

# ── 9. Done ──────────────────────────────────────────────────────────
echo
echo "${BOLD}${GREEN}Nolan is installed.${RESET}"
echo
echo "Next steps:"
if [[ -n "$INSTALL_DIR" ]]; then
    if [[ "$INSTALL_DIR" == "/Applications" ]] || [[ "$INSTALL_DIR" == "$HOME/Applications" ]]; then
        echo "  1. Open ${BOLD}Launchpad${RESET} or ${BOLD}Spotlight (⌘+Space)${RESET} and search 'Nolan'."
    else
        echo "  1. Double-click ${BOLD}Nolan${RESET} on your Desktop."
    fi
    echo "  2. The first-run wizard walks you through API keys + your first project."
fi
echo "  3. You can also import a project from a friend via the ${BOLD}Import${RESET} button."
echo
echo "Manual start: ${CYAN}cd $ROOT && source .venv/bin/activate && python3 main.py${RESET}"
echo
