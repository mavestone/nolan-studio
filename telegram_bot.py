"""
telegram_bot.py — Personal Telegram bot for Nolan

Lets you query your footage from your phone:
  • Default chat → AI chat about the active project (uses chat_about_project)
  • /projects     → list your projects
  • /use <id>     → switch active project
  • /search <q>   → transcript + scene search
  • /scenes <q>   → search only scenes
  • /stats        → project summary
  • /start        → register / show chat ID

Setup
─────
1. Create a bot with @BotFather in Telegram → get bot token
2. Edit settings.json (or use /api/settings):
     "telegram_token": "<your token>",
     "telegram_chat_ids": [<your chat id>],
     "telegram_default_project_id": <project id>
3. Restart Nolan. Send /start to your bot from Telegram.
"""

import asyncio
import logging
from typing import Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

log = logging.getLogger("nolan.telegram")


# ── State ────────────────────────────────────────────────────────────────
# active_project: {chat_id: project_id}
_active_project: dict[int, int] = {}
_app: Application | None = None
_settings_loader = None      # callable returning current settings dict


# ── Helpers ──────────────────────────────────────────────────────────────

def _allowed(chat_id: int) -> bool:
    s = _settings_loader() if _settings_loader else {}
    allowed = s.get("telegram_chat_ids") or []
    return chat_id in allowed


def _default_project_id() -> int | None:
    s = _settings_loader() if _settings_loader else {}
    return s.get("telegram_default_project_id")


async def _get_active(chat_id: int) -> int | None:
    if chat_id in _active_project:
        return _active_project[chat_id]
    return _default_project_id()


# ── Lean transcript-only chat (fast path for Telegram) ───────────────────

# Stop-words for fallback keyword extraction
_STOPWORDS = set("""
the a an and or but if then so of in on at to for with by from as is are was were be been
being have has had do does did this that these those i you he she we they them us my your
his her our their what when where why how who which whose can could should would will may
might shall must just about over under into onto out up down off any all some no not yes
me him her us them me i'd i'm i'll i've it it's its is am are tell show find give me say
""".split())

def _extract_keywords(text: str, max_n: int = 5) -> list[str]:
    """Literal-keyword fallback if LLM expansion fails."""
    import re
    words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text.lower())
    seen, out = set(), []
    for w in words:
        if w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out[:max_n]


async def _expand_query_with_llm(question: str) -> list[str]:
    """
    Use the LLM to interpret a fuzzy question and produce a list of literal
    search terms that would appear in spoken transcripts.

    Example:
      "something funny"   → ["funny","laugh","joke","haha","hilarious","lol"]
      "deep moments"      → ["realize","meaning","truth","understand","soul","life"]
      "where they argue"  → ["argue","fight","disagree","shut up","wrong"]

    Returns up to 8 single-word lowercase terms.
    """
    from analyzer import _try_anthropic_api, _try_groq, _anthropic_client
    import json as _json
    import re

    system = (
        "You convert a documentary-editor's question into a list of literal words "
        "that would appear in spoken transcripts. Think about synonyms, related "
        "concepts, and how people ACTUALLY talk on camera (not formal terms). "
        "Return ONLY a JSON array of 4-8 single lowercase words/short phrases. "
        "Bias toward common spoken words people actually say."
    )
    user = f"Question: {question}\n\nJSON array of search terms:"

    loop = asyncio.get_event_loop()
    raw = None
    try:
        if _anthropic_client:
            raw = await loop.run_in_executor(
                None, lambda: _try_anthropic_api(user, system, 150)
            )
        else:
            raw = await loop.run_in_executor(
                None, lambda: _try_groq(user, system, 150, 0.3, [])
            )
    except Exception as e:
        log.warning(f"Query expansion failed: {e}; falling back to literal")
        return _extract_keywords(question)

    if not raw:
        return _extract_keywords(question)

    # Extract a JSON array from the response (model may add text around it)
    match = re.search(r"\[[\s\S]*?\]", raw)
    if not match:
        return _extract_keywords(question)
    try:
        arr = _json.loads(match.group(0))
        terms = []
        for t in arr:
            t = str(t).strip().lower()
            if 2 <= len(t) <= 40 and t not in terms:
                terms.append(t)
        if terms:
            return terms[:8]
    except Exception as e:
        log.debug(f"Query expansion JSON parse failed: {e}")
    return _extract_keywords(question)


async def chat_transcripts_only(project_id: int, user_message: str,
                                 model: str = "haiku",
                                 candidate_pool: int = 120,
                                 final_pool: int = 60) -> tuple[str, list[dict]]:
    """
    Smarter transcript-only chat:
      1. LLM expands the query into search terms (semantic, not literal)
      2. We cast a WIDE net (120 candidates) into the transcripts
      3. LLM reasons over the pool with editorial judgement and writes the answer

    Returns (reply_text, cited_clips) where cited_clips is a list of
    {file_id, filename, start_time, end_time} parsed from the answer's citations.
    """
    from database import (
        get_project, search_transcripts, save_chat_message, get_file,
        get_chat_messages,
    )
    from analyzer import (
        _try_groq, _try_claude_cli, _try_anthropic_api,
        _claude_available, _anthropic_client,
        CLAUDE_MODEL, CLAUDE_SONNET_MODEL, CLAUDE_OPUS_MODEL,
    )

    CLAUDE_MODEL_FOR = {
        "haiku":  CLAUDE_MODEL,
        "sonnet": CLAUDE_SONNET_MODEL,
        "opus":   CLAUDE_OPUS_MODEL,
    }

    # ── Load conversation history (last N turns) for memory ──
    HISTORY_TURNS_MAX = 12   # 6 user + 6 assistant
    HISTORY_CHAR_MAX  = 800  # cap per message to keep token usage sane
    past = await get_chat_messages(project_id)
    # `past` is ordered oldest→newest. The just-saved user message is the LAST one — drop it.
    if past and past[-1].get("role") == "user":
        past = past[:-1]
    # Keep most recent N turns
    history_msgs = []
    for m in past[-HISTORY_TURNS_MAX:]:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if len(content) > HISTORY_CHAR_MAX:
            content = content[:HISTORY_CHAR_MAX] + "…"
        history_msgs.append({"role": role, "content": content})

    project = await get_project(project_id)
    if not project:
        return f"⚠️ Project {project_id} not found.", []

    # NB: caller is responsible for saving the user message before invoking us.

    # ── LLM-expanded search terms ──
    keywords = await _expand_query_with_llm(user_message)
    if not keywords:
        keywords = [user_message.strip()[:40]]
    log.info(f"Telegram query: {user_message!r} → keywords: {keywords}")

    # ── Wide net: collect many candidates, dedupe ──
    by_key: dict[tuple, dict] = {}
    for kw in sorted(keywords, key=lambda k: -len(k)):
        rows = await search_transcripts(kw, project_id)
        for r in rows:
            key = (r["file_id"], round(r["start_time"], 1))
            if key in by_key:
                by_key[key]["_score"] += len(kw)
                by_key[key]["_hits"] += 1
            else:
                r["_score"] = len(kw)
                r["_hits"]  = 1
                by_key[key] = r

    if not by_key:
        reply = (f"I searched the transcripts for {', '.join(keywords)} but found nothing. "
                 f"Try rephrasing — the LLM-expanded terms were too narrow.")
        await save_chat_message(project_id, "assistant", reply)
        return reply, []

    # Rank by multi-keyword hits first, then longer keyword score
    ranked = sorted(by_key.values(), key=lambda r: (-r["_hits"], -r["_score"]))
    candidates = ranked[:candidate_pool]
    # Re-sort the chosen ones chronologically by filename for the model
    candidates.sort(key=lambda r: (r["filename"], r["start_time"]))

    # ── Build prompt with rich context ──
    excerpt_lines = []
    for r in candidates[:final_pool]:
        ts = f"{int(r['start_time']//60)}:{int(r['start_time']%60):02d}"
        excerpt_lines.append(f"[{r['filename']} @ {ts}] {r['text']}")
    excerpt_block = "\n".join(excerpt_lines)

    system = (
        "You are part documentary filmmaker, part viral-social-media editor. "
        "Your job: surface the best, most cinematic, most clippable moments from "
        "the transcripts. Think hooks, story beats, vulnerability, conflict, energy.\n\n"
        "CONVERSATIONAL MEMORY:\n"
        "• You have access to the user's prior messages in this thread — use them.\n"
        "• If they reference 'that clip', 'the second one', 'those', etc., resolve to "
        "  what you just discussed. Maintain continuity.\n"
        "• Build on prior selections — if they liked one direction, lean into it. "
        "  If they rejected something, don't surface it again.\n\n"
        "MINDSET:\n"
        "• Generous and decisive — pick the best of what's available, always deliver, "
        "  never lecture the user about how to ask better questions.\n"
        "• Cinematic eye: look for texture, contradiction, raw emotion, specific imagery.\n"
        "• Social-media instinct: what's the HOOK? What 10-15 sec bite stops the scroll?\n"
        "• If the literal match is weak, find the SPIRIT of what they asked for.\n\n"
        "OUTPUT FORMAT — Telegram HTML (very strict):\n"
        "• Use <b>...</b> for bold, <i>...</i> for italic, <code>...</code> for citations.\n"
        "• DO NOT use markdown asterisks (**bold**), hashes (#), or hyphens for bullets.\n"
        "• DO NOT use any HTML tags other than <b>, <i>, <code>, <blockquote>.\n"
        "• Section headers: 🎬 <b>TOP PICK</b> · 🎯 <b>RUNNER-UPS</b> · ✂️ <b>EDIT ANGLE</b>\n"
        "• Quote format: <blockquote>their words here</blockquote>\n"
        "• Cite EVERY moment with EXACT format on its own line: <code>[filename.MP4 @ M:SS]</code>\n"
        "• Be COMPREHENSIVE — cite 6–12 moments when material is rich. Don't artificially "
        "  cap at 3 picks. Every file you reference becomes a tappable button for the user.\n"
        "• For 'bites'/'clips'/'hooks' requests: include cut-points like "
        "  <i>Cut: 2:18 → 2:31, ~13s</i>\n"
        "• Empty line between sections. Tight prose. ~300 words max."
    )
    prompt = (
        f"Project: {project['name']}\n\n"
        f"USER ASKS: {user_message}\n\n"
        f"Below are {len(excerpt_lines)} candidate transcript moments. Read them like "
        f"you're scrubbing through dailies — find the gold.\n\n"
        f"────── CANDIDATES ──────\n{excerpt_block}\n────────────────────────\n\n"
        f"Deliver the picks."
    )

    loop = asyncio.get_event_loop()
    reply = None
    backends = []
    if model in ("haiku", "sonnet", "opus"):
        claude_model = CLAUDE_MODEL_FOR[model]
        if _anthropic_client:
            backends.append((
                f"anthropic-{model}",
                lambda mm=claude_model: _try_anthropic_api(prompt, system, 900, mm, history=history_msgs),
            ))
        if _claude_available:
            backends.append((
                f"claude-cli-{model}",
                lambda mm=claude_model: _try_claude_cli(prompt, system, 900, mm, history=history_msgs),
            ))
        backends.append(("groq", lambda: _try_groq(prompt, system, 900, 0.7, history_msgs)))
    else:
        backends.append(("groq", lambda: _try_groq(prompt, system, 900, 0.7, history_msgs)))
        if _anthropic_client:
            backends.append((
                "anthropic-haiku-fallback",
                lambda: _try_anthropic_api(prompt, system, 900, CLAUDE_MODEL, history=history_msgs),
            ))

    for name, fn in backends:
        try:
            reply = await loop.run_in_executor(None, fn)
            if reply:
                log.info(f"Telegram chat answered via {name}")
                break
        except Exception as e:
            log.warning(f"Telegram chat: {name} failed: {e}")

    if not reply:
        reply = "⚠️ AI unavailable.\n\nRaw matches:\n" + "\n".join(excerpt_lines[:8])

    # ── Parse citations: catch EVERY filename mentioned, attach inline buttons ──
    import re
    cited_clips: list[dict] = []
    seen_files: set[int] = set()

    # Match either bracket-style [FILE.MP4 @ M:SS] OR plain FILE.MP4 anywhere in text
    file_pattern = re.compile(r"([A-Za-z0-9_\.\-]+\.(?:MP4|MOV|mp4|mov))", re.IGNORECASE)
    candidates_by_name = {r["filename"].lower(): r for r in candidates}

    for m in file_pattern.finditer(reply):
        fn = m.group(1)
        cand = candidates_by_name.get(fn.lower())
        if cand and cand["file_id"] not in seen_files:
            seen_files.add(cand["file_id"])
            cited_clips.append({
                "file_id":    cand["file_id"],
                "filename":   cand["filename"],
                "start_time": cand["start_time"],
            })

    await save_chat_message(project_id, "assistant", reply)
    return reply, cited_clips


# ── Telegram handlers ────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _allowed(chat_id):
        await update.message.reply_text(
            f"👋 Hello! Your Telegram chat ID is:\n\n`{chat_id}`\n\n"
            "Ask the owner to add this ID to Nolan's `telegram_chat_ids` setting "
            "to unlock access.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    from database import get_projects
    projects = await get_projects()
    pid = await _get_active(chat_id)
    cur  = next((p for p in projects if p["id"] == pid), None)
    body = "🎬 *Nolan — at your service*\n\n"
    if cur:
        body += f"Active project: *{cur['name']}*  (`{cur['id']}`)\n"
        body += f"_{cur.get('file_count', 0)} clips · {cur.get('done_count', 0)} processed_\n\n"
    else:
        body += "_No active project yet — use_ `/use <id>`\n\n"
    body += (
        "*Project knowledge*\n"
        "/story — narrative rundown of your footage\n"
        "/characters — who appears + their voice\n"
        "/themes — recurring threads\n"
        "/locations — where this was shot\n"
        "/summary — short overview\n\n"
        "*Search & utility*\n"
        "/search `<q>` — transcript matches\n"
        "/scenes `<q>` — shot/setting search (closeup, desert, …)\n"
        "/projects · /use `<id>` · /stats · /model `<name>`\n\n"
        "_Or just send a question — I'll search the transcripts and answer in seconds._"
    )
    await update.message.reply_text(body, parse_mode=ParseMode.MARKDOWN)


async def cmd_projects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update.effective_chat.id):
        return
    from database import get_projects
    projects = await get_projects()
    if not projects:
        await update.message.reply_text("No projects yet.")
        return
    active = await _get_active(update.effective_chat.id)
    lines = ["*Projects:*"]
    for p in projects:
        marker = " ◀ active" if p["id"] == active else ""
        lines.append(f"`{p['id']}` · *{p['name']}* — {p.get('file_count', 0)} clips{marker}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_use(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _allowed(chat_id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /use <project id>")
        return
    try:
        pid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Project id must be a number.")
        return
    from database import get_project
    p = await get_project(pid)
    if not p:
        await update.message.reply_text(f"Project `{pid}` not found.", parse_mode=ParseMode.MARKDOWN)
        return
    _active_project[chat_id] = pid
    await update.message.reply_text(f"✓ Active project → *{p['name']}*", parse_mode=ParseMode.MARKDOWN)


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _allowed(chat_id):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("Usage: /search <query>")
        return
    pid = await _get_active(chat_id)
    if not pid:
        await update.message.reply_text("No active project. Use /use <id>.")
        return

    from database import search_transcripts, search_project_scenes
    tx     = await search_transcripts(q, pid)
    scenes = await search_project_scenes(pid, q)

    parts = []
    if tx:
        parts.append(f"*Transcript matches: {len(tx)}*")
        for r in tx[:8]:
            t = (r.get("text") or "").strip()
            if len(t) > 140: t = t[:140] + "…"
            parts.append(f"`{r['filename']}` @ {_fmt_time(r['start_time'])}\n  _{t}_")
    if scenes:
        parts.append(f"\n*Scene matches: {len(scenes)}*")
        for s in scenes[:8]:
            attrs = " · ".join(filter(None, [
                s.get("shot_size"), s.get("roll_type"), s.get("setting"), s.get("location"),
            ]))
            parts.append(f"`{s['filename']}` @ {_fmt_time(s['start_time'])} — {attrs}")
    if not parts:
        parts.append(f"No matches for *{q}*")
    await update.message.reply_text("\n".join(parts), parse_mode=ParseMode.MARKDOWN)


async def cmd_scenes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _allowed(chat_id):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("Usage: /scenes <closeup | desert | a_roll | …>")
        return
    pid = await _get_active(chat_id)
    if not pid:
        await update.message.reply_text("No active project. Use /use <id>.")
        return
    from database import search_project_scenes
    scenes = await search_project_scenes(pid, q)
    if not scenes:
        await update.message.reply_text(f"No scenes match *{q}*", parse_mode=ParseMode.MARKDOWN)
        return
    lines = [f"*{len(scenes)} scene matches for {q}*"]
    for s in scenes[:15]:
        attrs = " · ".join(filter(None, [
            s.get("shot_size"), s.get("roll_type"), s.get("setting"), s.get("location"),
        ]))
        lines.append(f"`{s['filename']}` @ {_fmt_time(s['start_time'])} — {attrs}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """View or switch the chat model used by the bot."""
    chat_id = update.effective_chat.id
    if not _allowed(chat_id):
        return

    AVAILABLE = {
        "haiku":  "Claude Haiku 4.5 — fast + smart (default)",
        "sonnet": "Claude Sonnet 4.5 — deeper editorial reasoning",
        "opus":   "Claude Opus 4.7 — top-tier, slowest + priciest",
        "groq":   "Groq Llama 3.3 — fastest, less editorial",
    }

    s = _settings_loader() if _settings_loader else {}
    current = s.get("telegram_model") or "haiku"

    if not ctx.args:
        lines = [f"🤖 <b>Current model:</b> <code>{current}</code>\n", "<b>Available:</b>"]
        for k, desc in AVAILABLE.items():
            mark = " ✓" if k == current else ""
            lines.append(f"  <code>{k}</code>{mark} — {desc}")
        lines.append("\nSwitch with <code>/model haiku</code>, <code>/model sonnet</code>, or <code>/model groq</code>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    choice = ctx.args[0].strip().lower()
    # Aliases
    aliases = {
        "h": "haiku", "fast": "haiku", "claude": "haiku",
        "s": "sonnet", "deep": "sonnet", "smart": "sonnet",
        "o": "opus",  "best": "opus", "top": "opus",
        "g": "groq", "llama": "groq",
    }
    choice = aliases.get(choice, choice)

    if choice not in AVAILABLE:
        await update.message.reply_text(
            f"Unknown model <code>{choice}</code>. Use: haiku, sonnet, or groq.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Persist via the same settings file
    import json
    from pathlib import Path
    settings_path = Path("settings.json")
    try:
        data = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except Exception:
        data = {}
    data["telegram_model"] = choice
    settings_path.write_text(json.dumps(data, indent=2))

    await update.message.reply_text(
        f"✓ Model switched to <b>{choice}</b> — <i>{AVAILABLE[choice]}</i>",
        parse_mode=ParseMode.HTML,
    )


async def _broad_project_analysis(pid: int, focus: str, model: str = "haiku") -> str:
    """
    Give the LLM a sweeping view of all transcripts (sampled) and ask for a
    focused breakdown — story, characters, themes, etc. Used by /story, /chars,
    /themes, /summary.
    """
    from database import get_project, get_project_files, get_transcript
    from analyzer import (
        _try_anthropic_api, _try_claude_cli, _try_groq,
        _claude_available, _anthropic_client,
        CLAUDE_MODEL, CLAUDE_SONNET_MODEL, CLAUDE_OPUS_MODEL,
    )

    project = await get_project(pid)
    if not project:
        return "Project not found."

    files = await get_project_files(pid)
    # Sample broadly: take all clips with transcripts, cap total chars
    BUDGET = 28000  # ~7k tokens
    snippets = []
    used = 0
    for pf in files:
        segs = await get_transcript(pf["id"])
        if not segs:
            continue
        # Compact this clip's transcript
        joined = " ".join((s.get("text") or "").strip() for s in segs if s.get("text"))
        joined = joined.strip()
        if not joined:
            continue
        # Allow up to ~600 chars per clip to maximise breadth
        block = f"[{pf['filename']}] {joined[:600]}"
        if used + len(block) > BUDGET:
            break
        snippets.append(block)
        used += len(block)

    if not snippets:
        return "No transcripts available — process clips first."

    excerpt_block = "\n\n".join(snippets)

    FOCUS_PROMPTS = {
        "story": (
            "Give a tight 'story rundown' of this footage. What's it actually about? "
            "Who's involved, what tension drives it, and what's the emotional arc? "
            "Be specific — quote one or two memorable lines verbatim with [filename] citations. "
            "Under 280 words. No preamble."
        ),
        "characters": (
            "List the people who appear in this footage based on the transcripts. "
            "For each: a name (or descriptor if no name is given), 1-2 sentence personality "
            "sketch, and one quote that captures their voice with [filename] citation. "
            "Skip strangers in passing. Be concrete. Under 280 words."
        ),
        "themes": (
            "What are the recurring themes? Identify 4-6 substantial threads (not generic "
            "stuff like 'travel'). For each: a one-sentence framing, then a representative "
            "quote with [filename] citation. Order by how central it is to the project."
        ),
        "summary": (
            "Give a 4-6 sentence overview of this entire project. Locations, key moments, "
            "voice, and what makes it interesting. End with the strongest one-line tagline."
        ),
        "locations": (
            "List the locations / settings present in the footage based on transcripts. "
            "For each: one-line description, sample [filename] citation."
        ),
    }

    instruction = FOCUS_PROMPTS.get(focus, FOCUS_PROMPTS["story"])

    system = (
        "You're a documentary story consultant. Analyse the supplied transcript "
        "excerpts and answer in Telegram-safe HTML: use <b>bold</b>, <i>italic</i>, "
        "<code>inline</code>, <blockquote>quotes</blockquote>. No markdown asterisks. "
        "Always cite using <code>[FILENAME.MP4]</code>."
    )
    prompt = (
        f"Project: {project['name']}\n\n"
        f"Below are transcript excerpts from {len(snippets)} clips ("
        f"~{used:,} chars).\n\n"
        f"────── TRANSCRIPTS ──────\n{excerpt_block}\n─────────────────────────\n\n"
        f"{instruction}"
    )

    CLAUDE_MODEL_FOR = {"haiku": CLAUDE_MODEL, "sonnet": CLAUDE_SONNET_MODEL, "opus": CLAUDE_OPUS_MODEL}

    loop = asyncio.get_event_loop()
    backends = []
    if model in ("haiku", "sonnet", "opus"):
        cm = CLAUDE_MODEL_FOR[model]
        if _anthropic_client:
            backends.append(("anthropic", lambda: _try_anthropic_api(prompt, system, 1200, cm)))
        if _claude_available:
            backends.append(("cli", lambda: _try_claude_cli(prompt, system, 1200, cm)))
        backends.append(("groq", lambda: _try_groq(prompt, system, 1200, 0.5, [])))
    else:
        backends.append(("groq", lambda: _try_groq(prompt, system, 1200, 0.5, [])))

    for name, fn in backends:
        try:
            r = await loop.run_in_executor(None, fn)
            if r:
                log.info(f"Project analysis ({focus}) answered via {name}")
                return r
        except Exception as e:
            log.warning(f"Project analysis {name} failed: {e}")
    return "AI unavailable."


async def cmd_story(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _run_broad_command(update, ctx, "story")
async def cmd_characters(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _run_broad_command(update, ctx, "characters")
async def cmd_themes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _run_broad_command(update, ctx, "themes")
async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _run_broad_command(update, ctx, "summary")
async def cmd_locations(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _run_broad_command(update, ctx, "locations")


async def _run_broad_command(update, ctx, focus: str):
    chat_id = update.effective_chat.id
    if not _allowed(chat_id):
        return
    pid = await _get_active(chat_id)
    if not pid:
        await update.message.reply_text("Pick a project first: /use <id>")
        return
    s = _settings_loader() if _settings_loader else {}
    if s.get("offline_mode"):
        await update.message.reply_text("🔌 Offline mode — AI features disabled.")
        return
    model = s.get("telegram_model") or "haiku"

    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    keep_typing = asyncio.create_task(_keep_typing(ctx, chat_id))
    try:
        reply = await _broad_project_analysis(pid, focus, model=model)
    except Exception as e:
        log.exception("broad analysis failed")
        await update.message.reply_text(f"⚠️ Error: {e}")
        return
    finally:
        keep_typing.cancel()

    clean = _clean_telegram_html(reply)
    for chunk in _split_long(clean, 4000):
        try:
            await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
        except Exception:
            await update.message.reply_text(_strip_html(chunk))


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _allowed(chat_id):
        return
    pid = await _get_active(chat_id)
    if not pid:
        await update.message.reply_text("No active project. /use <id>.")
        return
    from database import get_project, get_project_stats
    p = await get_project(pid)
    s = await get_project_stats(pid)
    await update.message.reply_text(
        f"*{p['name']}*\n"
        f"Total: `{s.get('total', 0)}`\n"
        f"Done:  `{s.get('done', 0)}`\n"
        f"With transcript: `{s.get('with_transcript', 0)}`\n"
        f"Errors: `{s.get('errors', 0)}`",
        parse_mode=ParseMode.MARKDOWN,
    )


import re as _re_module
CUT_INTENT_RE_TG = _re_module.compile(
    r"\b(?:make|generate|build|create|edit|cut|give)\b.{0,40}\b(?:cut|edit|reel|montage|fcpxml|fcp|xml|video|sequence|timeline|these|this)\b"
    r"|^/?cut\b|\bnarrative cut\b|\b\d+\s*(?:s|sec|second)s?\s+cut\b"
    r"|^(?:do it|make it|export|render|use these|xml please|make xml|export xml|the cut|build cut|build me|build it)\b",
    _re_module.IGNORECASE,
)
FIND_INTENT_RE_TG = _re_module.compile(
    r"\b(?:find|search|look\s+for|show\s+me|where\s+is|get\s+me)\b.{0,30}\b(?:footage|clips?|shots?|moments?|takes?)\b"
    r"|\bfind\s+(?:me\s+)?\w+",
    _re_module.IGNORECASE,
)
DURATION_RE_TG = _re_module.compile(r"(\d{1,3})\s*(?:s|sec|second)s?\b", _re_module.IGNORECASE)

# Per-chat conversational state for the find→confirm→style→cut flow
# {chat_id: {"stage": "awaiting_confirm"|"awaiting_style", "theme": str, "files": [filename]}}
_pending_cut: dict[int, dict] = {}

YES_WORDS = {"y", "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "go", "do it", "make it", "please"}
NO_WORDS  = {"n", "no", "nah", "nope", "cancel", "stop", "skip"}


def _detect_cut_intent_telegram(text: str) -> dict | None:
    if not CUT_INTENT_RE_TG.search(text):
        return None
    m = DURATION_RE_TG.search(text)
    duration = int(m.group(1)) if m else 60
    return {"theme": text.strip(), "duration": max(15, min(600, duration))}


def _detect_find_intent_telegram(text: str) -> str | None:
    if not FIND_INTENT_RE_TG.search(text):
        return None
    return text.strip()


def _escape_html(s: str) -> str:
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


async def _handle_find_footage_in_telegram(update, ctx, pid: int, text: str) -> bool:
    """
    Find footage matching a theme with WORD-BOUNDARY accuracy. List candidate clips,
    each with an 'Open in Finder' button. Split into multiple messages if needed.
    """
    theme = _detect_find_intent_telegram(text)
    if not theme:
        return False

    from database import get_theme_relevant_clips, get_file
    chat_id = update.effective_chat.id

    # Strip leading "find / search for / show me" so the theme is just the topic
    clean_theme = _re_module.sub(
        r"^\s*(?:find|search|look\s+for|show\s+me|where\s+is|get\s+me)\s+(?:me\s+)?(?:some\s+)?"
        r"(?:footage|clips?|shots?|moments?)?\s*(?:of|about|on|with|where)?\s*",
        "", theme, flags=_re_module.IGNORECASE,
    ).strip() or theme

    await update.message.reply_text(
        f"🔍 Searching transcripts for <i>{_escape_html(clean_theme)}</i>…",
        parse_mode=ParseMode.HTML,
    )

    # Semantic expansion
    expanded = await _expand_query_with_llm(clean_theme)

    # Build a set of MUST-MATCH keywords. The raw theme + LLM-expanded variants.
    raw_kws = [clean_theme.lower()] + [k.lower() for k in expanded]
    # Split multi-word keywords into individual words (and keep the phrases as-is too)
    word_kws = set()
    phrase_kws = []
    for kw in raw_kws:
        kw = kw.strip()
        if not kw:
            continue
        if " " in kw:
            phrase_kws.append(kw)
        if len(kw) >= 3:
            for w in _re_module.findall(r"[a-z']+", kw):
                if len(w) >= 3 and w not in _STOPWORDS:
                    word_kws.add(w)

    # Compile a regex that matches any keyword as a WHOLE WORD (case-insensitive)
    # \biron\b — won't match "environment" anymore
    pattern_parts = []
    for w in word_kws:
        pattern_parts.append(rf"\b{_re_module.escape(w)}\b")
    for ph in phrase_kws:
        pattern_parts.append(_re_module.escape(ph))
    if not pattern_parts:
        await update.message.reply_text("Couldn't parse a search term.")
        return True
    word_re = _re_module.compile("|".join(pattern_parts), _re_module.IGNORECASE)

    # Wide net (loose SQL LIKE) → tight Python filter (word boundary)
    pool: dict[str, dict] = {}
    candidate_pool: dict[str, dict] = {}
    for kw in word_kws:
        # Pull candidates with loose match
        rows = await get_theme_relevant_clips(pid, kw, max_clips=25)
        for r in rows:
            if r["filename"] in candidate_pool:
                continue
            candidate_pool[r["filename"]] = r

    # Tight filter: only keep clips where at least one segment matches word-boundary
    for filename, r in candidate_pool.items():
        matching_segs = []
        match_hits = 0
        for s in (r.get("segments") or []):
            t = (s.get("text") or "").strip()
            if not t:
                continue
            if word_re.search(t):
                matching_segs.append(s)
                match_hits += 1
        if matching_segs:
            r["_matching_segs"] = matching_segs
            r["_match_hits"]    = match_hits
            pool[filename] = r

    ranked = sorted(pool.values(), key=lambda r: -r.get("_match_hits", 0))
    if not ranked:
        await update.message.reply_text(
            f"❌ No transcripts mention <i>{_escape_html(clean_theme)}</i> as a whole word.\n\n"
            f"Tried: {', '.join(sorted(word_kws)[:8])}",
            parse_mode=ParseMode.HTML,
        )
        return True

    def _best_quote(segs: list[dict]) -> str:
        """Pick the matching segment with the most signal (longest)."""
        if not segs:
            return ""
        chosen = max(segs, key=lambda s: len((s.get("text") or "").strip()))
        return (chosen.get("text") or "").strip()

    # Resolve file IDs for buttons
    for r in ranked:
        try:
            fobj = await get_file(int(r.get("file_id"))) if r.get("file_id") else None
            r["_file_id"] = fobj["id"] if fobj else r.get("file_id")
        except Exception:
            r["_file_id"] = r.get("file_id")

    # ── Split into batches: header + 10 clips per message ──
    BATCH = 10
    total = len(ranked)
    total_batches = (total + BATCH - 1) // BATCH

    for batch_idx in range(0, total, BATCH):
        batch = ranked[batch_idx : batch_idx + BATCH]
        start, end = batch_idx + 1, batch_idx + len(batch)

        if batch_idx == 0:
            header = (f"📂 <b>Found {total} clips for</b> <i>{_escape_html(clean_theme)}</i>"
                      + (f" — part 1/{total_batches}" if total_batches > 1 else "") + ":")
        else:
            header = (f"📂 <b>Continued</b> · clips {start}–{end} of {total} "
                      f"(part {batch_idx // BATCH + 1}/{total_batches})")

        lines = [header, ""]
        buttons, row = [], []
        for i, m_ in enumerate(batch, start):
            quote = _best_quote(m_.get("_matching_segs") or [])
            if len(quote) > 140:
                quote = quote[:140].rsplit(" ", 1)[0] + "…"
            lines.append(
                f"<b>{i}.</b> <code>{_escape_html(m_['filename'])}</code> "
                f"<i>· {m_.get('_match_hits', 1)} hit{'s' if m_.get('_match_hits',1) > 1 else ''}</i>\n"
                f"     <i>{_escape_html(quote)}</i>"
            )
            # Open-in-Finder button per clip
            fid = m_.get("_file_id")
            if fid:
                label = f"📂 {m_['filename']}"
                if len(label) > 30:
                    label = label[:28] + "…"
                row.append(InlineKeyboardButton(label, callback_data=f"open:{fid}"))
                if len(row) == 2:
                    buttons.append(row); row = []
        if row:
            buttons.append(row)

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
            parse_mode=ParseMode.HTML,
        )
    return True


_STOPWORDS = {
    "the","a","an","and","or","but","if","then","of","in","on","at","to","for","with",
    "by","from","as","is","are","was","were","be","been","have","has","had","do","does",
    "did","this","that","these","those","i","you","he","she","we","they","them","us","my",
    "your","his","her","our","their","what","when","where","why","how","who","which",
    "would","could","should","will","can","clip","clips","footage","video","shot","shots",
    "scene","scenes","moment","moments","find","show","tell","give","make","get","want",
    "need","like","just","really","very","some","any","all","some","more",
}


async def _handle_pending_cut_response(update, ctx, pid: int, text: str, model: str) -> bool:
    """
    If we previously asked the user if they want a cut, handle their reply here.
    Two stages: awaiting_confirm (yes/no/style-description) → awaiting_style (duration).
    """
    chat_id = update.effective_chat.id
    pending = _pending_cut.get(chat_id)
    if not pending:
        return False

    low = text.strip().lower()

    # Cancel
    if any(w == low for w in NO_WORDS):
        _pending_cut.pop(chat_id, None)
        await update.message.reply_text("👍 Cancelled. Send a new query anytime.")
        return True

    if pending["stage"] == "awaiting_confirm":
        # Either a plain "yes" → ask for style, OR a style description → proceed
        if any(w == low for w in YES_WORDS):
            pending["stage"] = "awaiting_style"
            await update.message.reply_text(
                "✂️ <b>What style?</b>\n\n"
                "Reply with a duration + vibe. Examples:\n"
                "• <code>30s, fast cuts, hook-first</code>\n"
                "• <code>60s slow burn, emotional</code>\n"
                "• <code>90s, build tension, end on a quote</code>",
                parse_mode=ParseMode.HTML,
            )
            return True

        # Treated as a style description — go straight to cut generation
        pending["stage"] = "awaiting_style"
        return await _produce_cut_from_pending(update, ctx, pid, text, model)

    if pending["stage"] == "awaiting_style":
        return await _produce_cut_from_pending(update, ctx, pid, text, model)

    return False


async def _produce_cut_from_pending(update, ctx, pid: int, style_desc: str, model: str) -> bool:
    """
    Generate the cut using ONLY the candidate files surfaced during 'find footage'.
    This guarantees the cut comes from what the user just saw.
    """
    chat_id = update.effective_chat.id
    pending = _pending_cut.pop(chat_id, None)
    if not pending:
        return False

    m = DURATION_RE_TG.search(style_desc)
    duration = int(m.group(1)) if m else 60
    duration = max(15, min(600, duration))

    combined_theme = f"{pending['theme']}\n\nStyle/vibe: {style_desc}"
    candidate_files = pending.get("files") or []

    await _handle_cut_in_telegram(
        update, ctx, pid, combined_theme, model,
        forced_duration=duration,
        restrict_to_files=candidate_files,
    )
    return True


async def _handle_cut_in_telegram(
    update, ctx, pid: int, text: str, model: str,
    forced_duration: int | None = None,
    restrict_to_files: list[str] | None = None,
) -> bool:
    """Generate FCPXML, send a chat summary + the .fcpxml file. Returns True if handled.

    If `restrict_to_files` is provided, the cut is built ONLY from those files
    (used when the user confirmed a 'find footage' result and asked for a cut).
    """
    # Detect intent ONLY if we aren't being called explicitly with forced params
    if forced_duration is None and not restrict_to_files:
        cut_intent = _detect_cut_intent_telegram(text)
        if not cut_intent:
            return False
        theme = cut_intent["theme"]
        duration = cut_intent["duration"]
    else:
        theme = text  # already the theme/style description
        duration = forced_duration or 60

    from database import (
        save_chat_message, get_project, get_project_files, get_transcript,
        get_all_batch_summaries, get_theme_relevant_clips,
    )
    from analyzer import select_narrative_clips
    from fcpxml import generate_fcpxml
    from pathlib import Path
    from datetime import datetime

    chat_id = update.effective_chat.id
    project = await get_project(pid)
    if not project:
        await update.message.reply_text("⚠️ Project not found")
        return True

    msg_suffix = f" from {len(restrict_to_files)} pre-selected clips" if restrict_to_files else ""
    await update.message.reply_text(
        f"🎬 Building <b>{duration}s</b> cut on <i>{_escape_html(theme[:80])}</i>{msg_suffix}…",
        parse_mode=ParseMode.HTML,
    )

    all_files = await get_project_files(pid)

    # Apply restriction (case-sensitive filename match)
    if restrict_to_files:
        restrict_set = set(restrict_to_files)
        scoped_files = [pf for pf in all_files if pf["filename"] in restrict_set]
        # Fallback: if restriction matched 0 files, drop it
        if not scoped_files:
            scoped_files = all_files
            restrict_set = None
    else:
        scoped_files = all_files
        restrict_set = None

    clips = []
    file_map = {}
    for pf in scoped_files:
        db_segs = await get_transcript(pf["id"])
        if db_segs:
            clips.append({
                "filename": pf["filename"],
                "segments": [{"start": s["start_time"], "end": s["end_time"], "text": s["text"]}
                             for s in db_segs],
            })
        file_map[pf["filename"]] = {
            "path":             pf["path"],
            "duration_seconds": pf.get("duration_seconds") or 0,
        }
    if not clips:
        await update.message.reply_text("⚠️ No transcripts in the selected clips.")
        return True

    # Build the "relevant_clips" pool
    if restrict_set:
        # Use ONLY the user-picked files; full transcripts already loaded above
        relevant_clips = [{"filename": c["filename"], "segments": c["segments"],
                           "match_count": 100} for c in clips]
    else:
        # Semantic + literal pool for diversity (free-form cut request)
        expanded = await _expand_query_with_llm(theme)
        pool_files = {}
        for kw in expanded:
            for m_ in await get_theme_relevant_clips(pid, kw, max_clips=12):
                pool_files.setdefault(m_["filename"], m_)
        for m_ in await get_theme_relevant_clips(pid, theme, max_clips=15):
            pool_files.setdefault(m_["filename"], m_)
        relevant_clips = list(pool_files.values())[:40]

    batch_sums = await get_all_batch_summaries(pid)
    loop = asyncio.get_event_loop()
    try:
        cut_info = await loop.run_in_executor(
            None,
            lambda: select_narrative_clips(
                clips, theme, duration, model,
                relevant_clips=relevant_clips, batch_summaries=batch_sums,
            ),
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Selection failed: {e}")
        return True

    cut_info["clips"] = [c for c in cut_info.get("clips", []) if c["filename"] in file_map]
    if not cut_info["clips"]:
        await update.message.reply_text("⚠️ AI returned no valid clips — try a different theme.")
        return True

    # Build + save FCPXML
    try:
        xml_str = generate_fcpxml(cut_info, file_map, project["name"])
    except Exception as e:
        await update.message.reply_text(f"⚠️ FCPXML build failed: {e}")
        return True

    cut_dir = Path("narrative_cuts")
    cut_dir.mkdir(exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in cut_info.get("title", "cut"))[:50]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    xml_path = cut_dir / f"{safe}_{ts}.fcpxml"
    xml_path.write_text(xml_str, encoding="utf-8")

    total_dur = sum(c.get("out_time", 0) - c.get("in_time", 0) for c in cut_info["clips"])
    unique_files = {c["filename"] for c in cut_info["clips"]}
    lines = [
        f"🎬 <b>{_escape_html(cut_info.get('title','Cut'))}</b>",
        f"<i>{_escape_html((cut_info.get('narrative_note') or '')[:300])}</i>",
        "",
        f"<b>{len(cut_info['clips'])} clips · {round(total_dur,1)}s · {len(unique_files)} files</b>",
        "",
    ]
    for i, c in enumerate(cut_info["clips"][:12], 1):
        in_t  = f"{int(c['in_time']//60)}:{int(c['in_time']%60):02d}"
        out_t = f"{int(c['out_time']//60)}:{int(c['out_time']%60):02d}"
        lines.append(f"<b>{i}.</b> <code>{_escape_html(c['filename'])} {in_t}→{out_t}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # Send the FCPXML file
    with open(xml_path, "rb") as f:
        await ctx.bot.send_document(
            chat_id=chat_id,
            document=f,
            filename=xml_path.name,
            caption="📁 Drag into DaVinci Resolve.",
        )

    await save_chat_message(pid, "assistant", f"[Cut: {cut_info.get('title')} — {len(cut_info['clips'])} clips, {len(unique_files)} files]")
    return True


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Top-level try so an exception in this handler never kills the polling loop
    try:
        chat_id = update.effective_chat.id
        if not _allowed(chat_id):
            return
        text = (update.message.text or "").strip()
        if not text:
            return

        # Block AI in offline mode
        s = _settings_loader() if _settings_loader else {}
        if s.get("offline_mode"):
            await update.message.reply_text("🔌 Offline mode — AI features disabled in Nolan settings.")
            return

        pid = await _get_active(chat_id)
        if not pid:
            await update.message.reply_text(
                "Pick a project first. Send /projects to list, then /use <id>.")
            return

        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

        # Use Claude haiku by default now that direct-API key is wired
        model = (s.get("telegram_model") or "haiku")

        # Save the inbound user message before invoking the chat helper
        from database import save_chat_message
        await save_chat_message(pid, "user", text)

        # ── Find Footage intent: search and list candidate clips (no XML — chat only) ──
        if await _handle_find_footage_in_telegram(update, ctx, pid, text):
            return

        # NB: XML/cut generation removed in v36 — use chat for everything.

        # Keep typing indicator alive while we wait for AI (it expires after 5s)
        keep_typing = asyncio.create_task(_keep_typing(ctx, chat_id))
        try:
            reply, cited = await asyncio.shield(
                asyncio.create_task(chat_transcripts_only(pid, text, model=model))
            )
        finally:
            keep_typing.cancel()

        # Sanitize the reply for Telegram HTML — strip markdown that leaked through
        clean = _clean_telegram_html(reply)

        # ── Send the text reply (split if >4000 chars) ──
        chunks = _split_long(clean, 4000)
        for chunk in chunks:
            try:
                await update.message.reply_text(chunk, parse_mode=ParseMode.HTML)
            except Exception as html_err:
                log.warning(f"HTML parse failed ({html_err}); sending as plain")
                await update.message.reply_text(_strip_html(chunk))

        # ── Send ALL cited clips as button batches (one message per 12 buttons) ──
        if cited:
            BATCH_SIZE = 12  # 12 buttons = 6 rows × 2 buttons
            for batch_idx in range(0, len(cited), BATCH_SIZE):
                batch = cited[batch_idx : batch_idx + BATCH_SIZE]
                buttons, row = [], []
                for c in batch:
                    label = f"📂 {c['filename']}"
                    if len(label) > 32:
                        label = label[:30] + "…"
                    row.append(InlineKeyboardButton(label, callback_data=f"open:{c['file_id']}"))
                    if len(row) == 2:
                        buttons.append(row); row = []
                if row:
                    buttons.append(row)

                # Header text for the button message
                if len(cited) <= BATCH_SIZE:
                    header = f"📎 <b>Open clips</b> ({len(cited)})"
                else:
                    total_batches = (len(cited) + BATCH_SIZE - 1) // BATCH_SIZE
                    this_batch    = batch_idx // BATCH_SIZE + 1
                    start, end    = batch_idx + 1, batch_idx + len(batch)
                    header = f"📎 <b>Open clips</b> {start}–{end} of {len(cited)} · part {this_batch}/{total_batches}"

                await update.message.reply_text(
                    header,
                    reply_markup=InlineKeyboardMarkup(buttons),
                    parse_mode=ParseMode.HTML,
                )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("Telegram message handler failed")
        try:
            await update.message.reply_text(f"⚠️ Error: {e}")
        except Exception:
            pass


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle inline-button taps: 'open:<file_id>' → reveal file in Finder on the host."""
    query = update.callback_query
    if not query:
        return
    if not _allowed(query.from_user.id):
        await query.answer("Not allowed", show_alert=True)
        return

    data = query.data or ""
    if data.startswith("open:"):
        try:
            file_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Bad request"); return

        # Call Nolan's reveal endpoint on localhost (we're in the same process)
        from database import get_file
        import subprocess
        from pathlib import Path
        f = await get_file(file_id)
        if not f:
            await query.answer("File not found", show_alert=True)
            return
        p = Path(f["path"])
        if not p.exists():
            await query.answer(f"Missing on disk:\n{f['path']}", show_alert=True)
            return
        try:
            subprocess.Popen(["open", "-R", str(p)])
            await query.answer(f"✓ Opened {f['filename']} in Finder")
        except Exception as e:
            await query.answer(f"Error: {e}", show_alert=True)
        return

    await query.answer()


async def _keep_typing(ctx, chat_id):
    """Ping 'typing…' every 4s while Claude works (Telegram's indicator expires at 5s)."""
    try:
        while True:
            await asyncio.sleep(4)
            try:
                await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                return
    except asyncio.CancelledError:
        pass


# ── Utility ──────────────────────────────────────────────────────────────

def _fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "0:00"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}:{s:02d}"


def _clean_telegram_html(text: str) -> str:
    """
    Normalise model output to Telegram-safe HTML:
      • Convert lingering **bold** / *italic* markdown into <b>/<i>
      • Strip stray markdown headers (#) and bullet asterisks
      • Escape <, >, & that AREN'T part of our allowed tags
      • Keep allowed tags: <b> <i> <code> <blockquote>
    """
    import re

    s = text

    # 1. Convert markdown bold/italic to HTML BEFORE escaping
    s = re.sub(r"\*\*([^\n*][^\n]*?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![*\w])\*([^\n*][^\n]*?)\*(?!\w)", r"<i>\1</i>", s)
    # Strip leading "# Heading"
    s = re.sub(r"(?m)^\s*#{1,6}\s*", "", s)
    # Strip leading "- " bullets at line start
    s = re.sub(r"(?m)^[\-•]\s+", "", s)

    # 2. Save our allowed tags, escape everything else
    PLACEHOLDERS = {}
    def stash(m):
        key = f"§§{len(PLACEHOLDERS)}§§"
        PLACEHOLDERS[key] = m.group(0)
        return key
    s = re.sub(r"</?(?:b|i|code|blockquote)>", stash, s, flags=re.IGNORECASE)
    # Escape remaining HTML specials
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Restore tags
    for k, v in PLACEHOLDERS.items():
        s = s.replace(k, v.lower())

    # 3. Collapse 3+ blank lines
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _strip_html(text: str) -> str:
    """Last-resort fallback: remove all HTML tags."""
    import re
    return re.sub(r"<[^>]+>", "", text)


def _split_long(s: str, limit: int) -> list[str]:
    if len(s) <= limit:
        return [s]
    out, buf = [], ""
    for line in s.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            out.append(buf)
            buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        out.append(buf)
    return out


# ── Launcher ─────────────────────────────────────────────────────────────

async def run_telegram_bot(token: str, settings_loader):
    """Long-running coroutine — call asyncio.create_task() on this."""
    global _app, _settings_loader
    _settings_loader = settings_loader

    _app = ApplicationBuilder().token(token).build()
    _app.add_handler(CommandHandler("start",    cmd_start))
    _app.add_handler(CommandHandler("projects", cmd_projects))
    _app.add_handler(CommandHandler("use",      cmd_use))
    _app.add_handler(CommandHandler("search",   cmd_search))
    _app.add_handler(CommandHandler("scenes",   cmd_scenes))
    _app.add_handler(CommandHandler("stats",    cmd_stats))
    _app.add_handler(CommandHandler("model",    cmd_model))
    _app.add_handler(CommandHandler("story",     cmd_story))
    _app.add_handler(CommandHandler("characters", cmd_characters))
    _app.add_handler(CommandHandler("chars",     cmd_characters))
    _app.add_handler(CommandHandler("themes",    cmd_themes))
    _app.add_handler(CommandHandler("summary",   cmd_summary))
    _app.add_handler(CommandHandler("locations", cmd_locations))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    _app.add_handler(CallbackQueryHandler(on_callback))

    log.info("Telegram bot starting (polling)…")
    try:
        await _app.initialize()
        await _app.start()
        await _app.updater.start_polling(drop_pending_updates=True)
        # Keep the task alive forever
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        try:
            await _app.updater.stop()
            await _app.stop()
            await _app.shutdown()
        except Exception:
            pass
        log.info("Telegram bot stopped")
