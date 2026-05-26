# Nolan Studio

Local-first documentary studio. Whisper transcribes every clip, OpenCV + AI
tag every shot, a Telegram bot lets you query the project from your phone, and
collaborators can share fully-processed projects so nobody has to transcribe
the same 30 hours of footage twice.

## What it does

- **Local transcription** — `faster-whisper` on your Mac. No upload, no API key needed.
- **Scene detection + shot classification** — PySceneDetect cuts, OpenCV heuristics tag
  shot size (CU → EWS), angle, indoor/outdoor, A-roll vs B-roll. Optional Claude
  vision pass adds specific locations.
- **Mac Finder-style browser** — list + gallery views, sort by shot type / roll
  type / setting / location, search by anything (transcripts, shot type, tags).
- **Telegram bot** — `/story`, `/characters`, `/themes`, plus natural-language
  search with Finder-link buttons for every result.
- **Project sharing** — export a `.nolanproj` bundle (transcripts + scenes + thumbnails)
  and send it to a collaborator. They import it and start editing immediately.

## Install (Mac)

```bash
git clone https://github.com/<your-user>/nolan-studio.git
cd nolan-studio
./install.sh
```

The script:

1. Installs Homebrew (if missing)
2. Installs `ffmpeg`
3. Sets up a Python 3.11 virtualenv with all dependencies
4. Creates a blank `.env`
5. Drops a `Nolan.app` on your Desktop

The installer asks where to put `Nolan.app`:

- **`/Applications`** *(recommended)* — Nolan shows up in Launchpad and Spotlight, just like any
  other Mac app
- `~/Applications` — per-user install, no sudo
- `~/Desktop` — quick access
- Skip if you'd rather start from Terminal

Open Nolan from Spotlight (`⌘+Space → "Nolan"`) → the first-run wizard walks you
through API keys, the optional Telegram bot, and your first project.

If you ever want to rebuild the `.app` (e.g. moved the repo to a new path):

```bash
./make-app.sh /Applications   # or ~/Desktop, ~/Applications, etc.
```

## Optional API keys

Transcription + scene detection run fully offline. AI features (chat, story
analysis, intelligent search) need at least one of:

- **Anthropic Claude** — best editorial reasoning. Get a key at
  [console.anthropic.com](https://console.anthropic.com/).
- **Groq Llama** — fastest fallback. Get a free key at
  [console.groq.com/keys](https://console.groq.com/keys).
- **Google Gemini** — additional fallback.

Paste them in the **Settings** gear (top toolbar) or directly into `.env`.

## Sharing a project

**Export** — Hover any project card on the home screen → click the ⬆ icon → save
the `.nolanproj` file. It contains:

- All transcript segments + timecodes
- All detected scenes + shot classifications
- All thumbnails
- Chat history (optional)
- Story-analysis bible

**Import** — On the home screen, click **Import** → pick the `.nolanproj` →
when prompted, point Nolan at the folder where the source MP4s live on your
Mac. Nolan walks that folder, matches by filename, and relinks every clip.

## Telegram bot

Create a bot with [@BotFather](https://t.me/BotFather) → paste the token in
Settings or via:

```bash
curl -X PATCH http://localhost:8765/api/settings \
  -H "Content-Type: application/json" \
  -d '{"telegram_token":"YOUR_TOKEN"}'
```

Send `/start` to your bot — it'll reply with your chat ID, which you add to
the **Allowed chat IDs** field in Settings to authorise yourself.

Commands:

| Command | What it does |
|---|---|
| `/story` | Narrative rundown of the project |
| `/characters` | People + their voices |
| `/themes` | Recurring threads |
| `/locations` | Where this was shot |
| `/summary` | 4-6 sentence overview |
| `/search <q>` | Transcript matches |
| `/scenes <q>` | Shot-attribute search (closeup, desert, …) |
| `/model haiku · sonnet · opus · groq` | Switch AI model |
| any text | Full chat with footage knowledge |

## Architecture

```
main.py            — FastAPI app, transcription pipeline, REST endpoints
analyzer.py        — AI router (Anthropic → CLI → Groq fallback chain)
transcriber.py     — faster-whisper + ffprobe scanning
scene_detector.py  — PySceneDetect + OpenCV classifier
fcpxml.py          — DaVinci-compatible XML export (legacy, not in UI)
telegram_bot.py    — Bot polling + smart search/chat
database.py        — SQLite schema + queries (via aiosqlite)
static/            — Web UI (vanilla HTML/CSS/JS, no build step)
```

## Manual run

```bash
source .venv/bin/activate
python3 main.py
# → http://localhost:8765
```

`NOLAN_DEV=1 python3 main.py` enables uvicorn auto-reload for development.

## Licence

Personal use only. If you build something cool with this, send a link.
