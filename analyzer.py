"""
analyzer.py — AI backend for Nolan

Primary:  Claude Haiku via local Claude Code CLI (no API key needed — uses your
          existing Claude subscription through the desktop app OAuth session)
Fallback: Groq llama-3.3-70b-versatile — used if Claude CLI fails

Story Bible uses MAP-REDUCE over ALL clips:
  Map:     Batch all clips → summarise each batch (checkpoint saved to DB)
  Reduce:  Combine summaries → final Story Bible
"""

import json
import logging
import math
import os
import shutil
import subprocess
import time

import anthropic
from groq import Groq
from google import genai as _genai

log = logging.getLogger("nolan")

# ── Find Claude CLI ────────────────────────────────────────────────────────

CLAUDE_CLI = (
    shutil.which("claude")
    or os.path.expanduser("~/.local/bin/claude")
    or "/usr/local/bin/claude"
)
_claude_available = bool(CLAUDE_CLI and os.path.isfile(CLAUDE_CLI))

if _claude_available:
    log.info(f"Claude CLI found at {CLAUDE_CLI} ✓")
else:
    log.warning("Claude CLI not found — will use Groq only")

_groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY")) if os.environ.get("GROQ_API_KEY") else None
_groq_client_2 = Groq(api_key=os.environ.get("GROQ_API_KEY_2")) if os.environ.get("GROQ_API_KEY_2") else None

gemini_key = os.environ.get("GEMINI_API_KEY")
_gemini_client = _genai.Client(api_key=gemini_key) if gemini_key else None

# Direct Anthropic API client (preferred over CLI when ANTHROPIC_API_KEY is set
# to a real sk-ant-* key in .env). Faster, no subprocess overhead.
_anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
_anthropic_client = None
if _anthropic_key.startswith("sk-ant-"):
    try:
        _anthropic_client = anthropic.Anthropic(api_key=_anthropic_key)
        log.info("Anthropic API client ready ✓ (preferred over CLI)")
    except Exception as e:
        log.warning(f"Anthropic SDK init failed: {e}")
CLAUDE_MODEL        = "claude-haiku-4-5"         # Fast + cheap
CLAUDE_SONNET_MODEL = "claude-sonnet-4-5"        # Higher quality
CLAUDE_OPUS_MODEL   = "claude-opus-4-7"          # Top-tier — slowest + priciest
GROQ_MODEL          = "llama-3.3-70b-versatile"  # Fallback

# Model aliases used by the UI → internal routing
# "haiku"  → Claude Haiku via CLI
# "sonnet" → Claude Sonnet via CLI
# "gemini" → Gemini 2.5 Pro
# "groq"   → Groq Llama

# ── Rate-limit / batching config ───────────────────────────────────────────
BATCH_CHARS   = 15_000   # chars per map batch (~3,750 tokens)
COMBINE_CHARS = 28_000   # chars of summaries for reduce (~7,000 tokens)
SLEEP_SECS    = 1        # pause between Claude CLI batches (no rate limit concern)
SLEEP_GROQ    = 22       # pause when Groq fallback handles a batch (12k TPM limit)
CHAT_CHARS    = 20_000   # chars for fallback raw-transcript context in chat
CUT_CHARS     = 30_000   # chars for narrative cut transcript pool
BATCH_SUM_CUT = 18_000   # chars of batch summaries to include in cut context
RETRY_DELAYS  = [10, 30, 60]


# ── Direct Anthropic API call (preferred) ────────────────────────────────

def _try_anthropic_api(user_content: str, system: str | None, max_tokens: int,
                        model: str = CLAUDE_MODEL,
                        history: list[dict] | None = None) -> str:
    """
    Call Claude directly via the Anthropic Python SDK using ANTHROPIC_API_KEY.
    Optional `history` is a list of {role, content} dicts for multi-turn memory.
    """
    if not _anthropic_client:
        raise RuntimeError("Anthropic API client not configured")

    messages = []
    if history:
        # Anthropic requires alternating user/assistant turns starting with user
        for m in history:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_content})

    msg = _anthropic_client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system or "You are a helpful assistant.",
        messages=messages,
    )
    parts = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


# ── Claude CLI call (fallback) ────────────────────────────────────────────

def _try_claude_cli(user_content: str, system: str | None, max_tokens: int,
                    cli_model: str = CLAUDE_MODEL,
                    history: list[dict] | None = None) -> str:
    """
    Call Claude via the local Claude Code CLI.
    Uses the desktop app's OAuth session — no API key required.
    History (if provided) is flattened into the prompt as User:/Assistant: turns.
    """
    if history:
        turns = []
        for m in history:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if not content:
                continue
            if role == "user":
                turns.append(f"User: {content}")
            elif role == "assistant":
                turns.append(f"Assistant: {content}")
        if turns:
            user_content = "\n\n".join(turns) + f"\n\nUser: {user_content}"
    cmd = [
        CLAUDE_CLI,
        "--print",
        "--model", cli_model,
        "--output-format", "text",
        "--no-session-persistence",
        "--dangerously-skip-permissions",
    ]

    # Always override the default Claude Code system prompt so it doesn't
    # refuse non-coding tasks (the CLI ships as a code assistant by default)
    effective_system = system or "You are a helpful assistant. Follow the user's instructions precisely and completely."
    cmd += ["--system-prompt", effective_system]

    # Strip Claude Code env vars that can cause recursive-session conflicts.
    # IMPORTANT: if we're inside a Claude Code session the desktop app provides
    # OAuth auth — but ANTHROPIC_API_KEY in the shell env will override OAuth
    # and fail with "Invalid API key". Strip it so the subprocess uses OAuth.
    _inside_cc = bool(os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"))
    env = os.environ.copy()
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "AI_AGENT",
              "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH"):
        env.pop(k, None)
    if _inside_cc:
        env.pop("ANTHROPIC_API_KEY", None)

    result = subprocess.run(
        cmd,
        input=user_content,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    if result.returncode != 0:
        # Include both stderr and stdout in error to aid debugging
        detail = (result.stderr or result.stdout or "").strip()
        log.debug(f"Claude CLI stdout: {result.stdout[:300]!r}")
        log.debug(f"Claude CLI stderr: {result.stderr[:300]!r}")
        raise RuntimeError(f"Claude CLI exit {result.returncode}: {detail[:300]}")
    return result.stdout.strip()


# ── Groq call ─────────────────────────────────────────────────────────────

def _try_groq(user_content, system, max_tokens, temperature, history):
    """Call Groq with fallback to a secondary API key on rate limits."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    if history:
        msgs.extend(history)
    msgs.append({"role": "user", "content": user_content})

    client = _groq_client
    if not client:
        client = _groq_client_2
        if not client:
            raise RuntimeError("Groq not configured (no API key)")

    last_err = None
    for attempt, delay in enumerate([0] + RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=msgs,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            last_err = e
            s = str(e).lower()
            if "rate_limit" in s or "429" in s or "too many" in s:
                if client is _groq_client and _groq_client_2:
                    log.info("Groq primary key rate-limited — switching to fallback key…")
                    client = _groq_client_2
                    continue
                log.warning("Groq rate-limited (both keys) — waiting 65s…")
                time.sleep(65)
                continue
            if "connection" not in s and "timeout" not in s:
                raise
    raise last_err


# ── Gemini call ────────────────────────────────────────────────────────────

def _try_gemini(user_content, system, max_tokens, temperature, history):
    """Call Gemini 2.5 Pro via google-genai SDK. Fails fast on rate limits."""
    if not _gemini_client:
        raise RuntimeError("Gemini not configured (no GEMINI_API_KEY)")

    prompt = user_content
    if system:
        prompt = system + "\n\n" + prompt
    if history:
        turns = []
        for m in history:
            role = m.get("role", "user")
            content = m.get("content", "")
            turns.append(f"{role.capitalize()}: {content}")
        if turns:
            prompt = "\n".join(turns) + f"\nUser: {prompt}"

    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            response = _gemini_client.models.generate_content(
                model="gemini-2.5-pro",
                contents=prompt,
                config=_genai.types.GenerateContentConfig(
                    max_output_tokens=max_tokens,
                    temperature=temperature,
                ),
            )
            return response.text.strip()
        except Exception as e:
            s = str(e).lower()
            if "429" in s or "quota" in s or "rate limit" in s:
                if attempt < max_attempts - 1:
                    log.info("Gemini rate-limited — retrying once in 5s…")
                    time.sleep(5)
                    continue
                raise  # let _call_ai catch and fall to Groq
            raise  # non-rate-limit error → fall to Groq immediately


# ── Unified AI call ───────────────────────────────────────────────────────

def _call_ai(
    user_content: str,
    system: str | None = None,
    max_tokens: int = 1000,
    temperature: float = 0.3,
    history: list[dict] | None = None,
    model: str = "haiku",           # "haiku" | "sonnet" | "gemini" | "groq"
) -> tuple[str, str]:
    """
    Returns (response_text, backend_used).
    Routes based on `model` param; falls back to next tier on error.
    """
    # ── Claude (haiku, sonnet, opus) — direct API first, then CLI ──
    if model in ("haiku", "sonnet", "opus"):
        api_model = {
            "haiku":  CLAUDE_MODEL,
            "sonnet": CLAUDE_SONNET_MODEL,
            "opus":   CLAUDE_OPUS_MODEL,
        }[model]

        prompt = user_content
        if history:
            turns = []
            for m in history:
                role = m.get("role", "user")
                content = m.get("content", "")
                if role == "user":
                    turns.append(f"User: {content}")
                elif role == "assistant":
                    turns.append(f"Assistant: {content}")
            if turns:
                prompt = "\n".join(turns) + f"\nUser: {user_content}"

        # Priority 1: direct API
        if _anthropic_client:
            try:
                text = _try_anthropic_api(prompt, system, max_tokens, api_model)
                return text, f"claude-api-{model}"
            except Exception as e:
                log.warning(f"Anthropic API ({model}) failed: {e} — trying CLI")

        # Priority 2: Claude Code CLI
        if _claude_available:
            try:
                text = _try_claude_cli(prompt, system, max_tokens, api_model)
                return text, f"claude-cli-{model}"
            except Exception as e:
                log.warning(f"Claude CLI ({model}) failed: {e} — falling back")

    # ── Gemini ──
    if model == "gemini" or (model in ("haiku", "sonnet") and _gemini_client):
        if _gemini_client:
            try:
                text = _try_gemini(user_content, system, max_tokens, temperature, history)
                return text, "gemini"
            except Exception as e:
                log.warning(f"Gemini failed: {e} — falling back to Groq")

    # ── Groq (always last resort) ──
    text = _try_groq(user_content, system, max_tokens, temperature, history)
    return text, "groq"


# ── Prompts ────────────────────────────────────────────────────────────────

BATCH_SYSTEM = """\
You are a documentary story analyst. Read the footage clips provided and write a BRIEF analyst \
note for the editor. Cover:
- Who appears and the main topics/conversations
- 2-3 verbatim quotes (≤12 words) with their clip filename
- Emotional moments, conflicts, revelations, strong story beats
- Any notable themes

Keep it under 280 words. Reference specific clip filenames.\
"""

BATCH_PROMPT = "Analyse these clips:\n\n"  # user-facing prefix; system above handles instructions

COMBINE_PROMPT = """\
You are a senior documentary editor. The notes below cover EVERY clip in this documentary \
project — {total_clips} clips total, analysed in {n_batches} batches.

Synthesise everything into a story bible. Return ONLY valid JSON:

{{
  "story_arc": "<core narrative journey — what is this film really about>",
  "central_conflict": "<main tension, question or contradiction driving the story>",
  "key_characters": [
    {{"name": "<person>", "role": "<their role in the story>"}}
  ],
  "themes": ["<theme>"],
  "three_act_structure": {{
    "act1": "<clips that establish world and characters>",
    "act2": "<clips carrying conflict and development>",
    "act3": "<clips bringing resolution or conclusion>"
  }},
  "must_use_clips": [
    {{"filename": "<filename>", "reason": "<why essential>"}}
  ],
  "missing_elements": "<what footage would strengthen the story>",
  "directors_notes": "<overall vision — tone, style, what makes it worth telling>"
}}

Project: {project_name}

ANALYST NOTES FROM ALL BATCHES:
"""

CHAT_SYSTEM = """\
You are a documentary editor and story consultant on "{project_name}".

You have been given THREE layers of footage access. Use ALL of them:

1. STORY BIBLE — synthesised narrative overview of the whole project
2. BATCH NOTES — analyst notes covering EVERY clip in {n_batches} batches (this is the full footage library)
3. TRANSCRIPT SEARCH RESULTS — actual word-for-word transcripts for clips that match the current question

With these you can:
- Find exact quotes and timestamps from the actual transcripts
- Identify which clips contain specific topics, words, or people
- Suggest specific cuts with exact filenames and timestamps
- Answer questions about what was said, by whom, and at what timecode

When you recommend a clip, ALWAYS give:
  CLIP: [FILENAME.MP4] at [MM:SS] — "exact words from the transcript"

When the user asks to "make a cut" or "create a cut about X":
  1. Reference specific clips from the search results with their timestamps
  2. Describe the narrative arc
  3. Tell them to click "⚡ Use in Cut" or go to the Cut tab with this theme

Be direct and specific. Reference actual filenames and timestamps from the transcript data below.

{bible_section}

━━━ BATCH NOTES — ALL FOOTAGE ({n_batches} batches) ━━━
{batch_summaries}

━━━ TRANSCRIPT SEARCH RESULTS (clips matching this question) ━━━
{relevant_section}

━━━ ADDITIONAL RAW TRANSCRIPTS (highest dialogue clips) ━━━
{raw_transcripts}"""

NARRATIVE_CUT_PROMPT = """\
You are a world-class documentary editor cutting a {duration}-second piece.

THEME / BRIEF: "{theme}"

{project_context}

{format_instructions}

EDITING RULES:
- Each clip segment: minimum 4 seconds, maximum 25 seconds
- Total of ALL clips combined MUST equal approximately {duration} seconds (±5s)
- filename = EXACTLY as it appears in the CSV filename column (case-sensitive, include extension)
- in_time  = the EXACT start_s float from a CSV row — copy it verbatim (e.g. 1371.42)
- out_time = start_s of a later row in the same clip, or in_time + your chosen seconds
- out_time − in_time must be 4–25 seconds
- quote    = the text column of that CSV row
- ONLY select rows that appear in the CSV table below — do NOT invent filenames or times
- DIVERSITY IS MANDATORY: use AT LEAST 3 different source files. Never pull the
  entire cut from a single clip even if it has the strongest material. A cut
  built from one file is REJECTED.
- No 2 consecutive picks from the same file (alternate sources)
- End on the strongest possible line

Return ONLY valid JSON:
{{
  "title": "evocative short title for this cut",
  "narrative_note": "2-3 sentence description of the emotional arc and intended feeling",
  "clips": [
    {{
      "filename": "EXACT_FILENAME.MP4",
      "in_time": 1371.42,
      "out_time": 1386.10,
      "quote": "exact text from that CSV row",
      "narrative_role": "HOOK — describes what this clip does in the story"
    }}
  ]
}}

TRANSCRIPT SEGMENTS (CSV — timestamps are seconds, copy them exactly):
"""

# Build format-specific instructions based on duration
def _get_format_instructions(duration: int) -> str:
    if duration <= 30:
        return f"""\
FORMAT: FLASH CUT / MICRO-TEASER ({duration}s)
Visceral. Punchy. No wasted frames. Think viral hook or festival flash-frame.

STRUCTURE (3-5 clips MAX — no more):
1. COLD OPEN (3-7s): The single most arresting moment in your footage. No setup. Drop straight in.
2. PIVOT (4-8s): One clip that recontextualises or escalates. A contradiction, confession, or sharp reaction.
3. LANDING (3-7s): The final line. One devastating sentence. Make it echo.

PACING: EXTREME. Every clip duration_secs must be 4-9 seconds — nothing longer.
No slow moments, no context-setting, no breathing room. Pure impact. Cut on the word, not after it."""

    if duration <= 60:
        return """\
FORMAT: SHORT-FORM / SOCIAL REEL ({duration}s)
Think TikTok, Instagram Reel, YouTube Short. Visceral, fast, emotionally immediate.

STRUCTURE (4-7 clips total):
1. COLD OPEN (3-6s): Drop the viewer straight into the most arresting moment — \
a raw quote, an intense look, something that stops the scroll. No setup.
2. TENSION (8-15s): 1-2 clips that establish stakes. What's at risk? Why should anyone care?
3. THE PUNCH (8-15s): 2-3 clips building rapid-fire — contradiction, revelation, emotion. \
Cut tight. Let the words hit.
4. LANDING (4-8s): One final line. Devastating, funny, or hauntingly quiet. \
The clip that makes someone rewatch.

PACING: Fast. Average clip length 5-10s. No establishing shots unless they're stunning. \
Prioritise dialogue and reaction. Every second earns its place.""".format(duration=duration)

    elif duration <= 180:
        return """\
FORMAT: TRAILER / SIZZLE ({duration}s)
Think documentary trailer, pitch sizzle, festival teaser. Build a world, then break it open.

STRUCTURE (6-12 clips total):
1. HOOK (6-12s): Open with a moment that defines the tone — \
a striking line, an image that begs questions, the thesis in one breath.
2. WORLD BUILDING (15-25s): 2-3 clips establishing place, people, context. \
Who are we with? What is this world? Let it breathe.
3. RISING TENSION (20-35s): 3-4 clips where stakes become clear — \
conflict, struggle, the thing that makes this story matter. \
Alternate between tight emotional beats and wider contextual moments.
4. THE TURN (10-20s): The moment that changes everything — \
a confession, a contradiction, a revelation. The emotional peak.
5. RESOLUTION / CLIFF (8-15s): Leave them wanting more. \
Either a powerful closing statement or an open question that haunts.

PACING: Start measured, accelerate through the middle, breathe at the end. \
Mix clip lengths: some 6s punches, some 15s moments that land with weight.""".format(duration=duration)

    else:
        return """\
FORMAT: LONG-FORM STORY ({duration}s)
Think mini-documentary, extended trailer, or festival cut. Full emotional architecture.

STRUCTURE (10-20 clips total):
1. OPENING IMAGE (8-15s): The frame that sets the visual and emotional tone. \
Something specific and cinematic.
2. THESIS (10-20s): 1-2 clips that pose the central question or tension. \
What is this story really about? Plant the seed.
3. CHARACTER INTRODUCTION (20-40s): 3-4 clips introducing the key people. \
Let us hear their voices, see their world. Specificity over generality.
4. COMPLICATION (25-45s): 4-5 clips where the real story emerges — \
obstacles, contradictions, the messy human truth underneath. \
This is the engine of your narrative. Build tension gradually.
5. EMOTIONAL CLIMAX (15-30s): 2-3 clips at the heart of the story — \
the most raw, honest, revealing moments. Confession, breakdown, breakthrough.
6. RESOLUTION (10-20s): How does this story land? \
A return to the opening motif, a transformed perspective, \
or a question that stays with the viewer long after.

PACING: Classical documentary rhythm. Let scenes breathe. \
Not every cut needs to be a punch — some clips earn their place through silence and observation. \
Build, release, build higher, release deeper.""".format(duration=duration)


# ── Helpers ────────────────────────────────────────────────────────────────

def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _clip_text(clip: dict) -> str:
    """Formatted text block used for story bible batch analysis."""
    segs   = clip.get("segments", [])
    header = f"=== {clip['filename']} ({len(segs)} segments) ==="
    lines  = [f"[{_fmt(s['start'])}] {s['text']}" for s in segs]
    return header + "\n" + "\n".join(lines)


def build_transcripts_csv(clips: list[dict]) -> str:
    """
    Build a CSV string of all transcript segments with exact float timestamps.
    Used for the narrative cut prompt and auto-saved to disk after processing.

    Each row: filename,start_s,end_s,text
    Timestamps are floats in seconds — the AI copies them verbatim, no conversion.
    """
    import csv, io
    out = io.StringIO()
    w   = csv.writer(out, lineterminator="\n")
    w.writerow(["filename", "start_s", "end_s", "text"])
    for clip in clips:
        fname = clip["filename"]
        for s in clip.get("segments", []):
            w.writerow([
                fname,
                round(float(s.get("start", 0)), 3),
                round(float(s.get("end",   0)), 3),
                s.get("text", "").strip(),
            ])
    out.seek(0)
    return out.getvalue()


def _batch_clips(clips: list[dict], max_chars: int) -> list[list[dict]]:
    batches, cur, cur_len = [], [], 0
    for clip in clips:
        txt = _clip_text(clip)
        if cur_len + len(txt) > max_chars and cur:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(clip)
        cur_len += len(txt)
    if cur:
        batches.append(cur)
    return batches


def _parse_json(raw: str) -> dict:
    if "```" in raw:
        start = raw.find("{", raw.find("```"))
        end   = raw.rfind("}") + 1
        raw   = raw[start:end]
    return json.loads(raw)


# ── Public API ─────────────────────────────────────────────────────────────

def analyze_project(
    clips: list[dict],
    project_name: str,
    progress_cb=None,
    existing_summaries: dict | None = None,
    on_batch_done=None,
    model: str = "haiku",
) -> dict:
    """
    Full map-reduce Story Bible with checkpoint/resume support.
    model: "haiku" | "sonnet" | "gemini" | "groq"
    """
    if not clips:
        return {"directors_notes": "No transcripts found."}

    MODEL_LABELS = {
        "haiku": "Claude Haiku", "sonnet": "Claude Sonnet",
        "gemini": "Gemini 2.5 Pro", "groq": "Groq",
    }
    backend_label = MODEL_LABELS.get(model, model)

    batches = _batch_clips(clips, BATCH_CHARS)
    n       = len(batches)
    existing_summaries = existing_summaries or {}

    if progress_cb:
        already = len(existing_summaries)
        progress_cb(already, n,
                    f"{'Resuming' if already else 'Starting'} — "
                    f"{len(clips)} clips in {n} batches via {backend_label}"
                    + (f" ({already} already done)" if already else ""))

    summaries = {}
    last_backend = model

    for i, batch in enumerate(batches):
        bnum = i + 1

        if bnum in existing_summaries:
            summaries[bnum] = existing_summaries[bnum]
            if progress_cb:
                progress_cb(bnum, n, f"Batch {bnum}/{n} — already done ✓")
            continue

        if progress_cb:
            progress_cb(bnum, n, f"Batch {bnum}/{n} ({len(batch)} clips) via {backend_label}…")

        text  = "\n\n".join(_clip_text(c) for c in batch)
        label = f"\n\n[Batch {bnum}/{n} — {len(batch)} clips]"
        summary, last_backend = _call_ai(
            BATCH_PROMPT + text + label,
            system=BATCH_SYSTEM,
            max_tokens=650,
            temperature=0.3,
            model=model,
        )
        summaries[bnum] = summary

        if on_batch_done:
            on_batch_done(bnum, n, summary)

        if i < n - 1:
            sleep = SLEEP_SECS if "claude" in last_backend else SLEEP_GROQ
            time.sleep(sleep)

    if progress_cb:
        progress_cb(n, n, "Combining all batch summaries into Story Bible…")

    joined = "\n\n---\n\n".join(
        f"BATCH {i+1}/{n}:\n{summaries[i+1]}" for i in range(n)
    )
    if len(joined) > COMBINE_CHARS:
        joined = joined[:COMBINE_CHARS] + "\n\n[... summaries truncated]"

    combine_system = "You are a senior documentary editor. Respond ONLY with valid JSON — no markdown, no commentary."
    prompt = COMBINE_PROMPT.format(
        total_clips=len(clips), n_batches=n, project_name=project_name
    ) + joined

    raw, _ = _call_ai(prompt, system=combine_system, max_tokens=2800, temperature=0.3, model=model)
    try:
        result = _parse_json(raw)
    except json.JSONDecodeError:
        result = {"directors_notes": raw[:3000]}

    result["_meta"] = {
        "total_clips":     len(clips),
        "batches":         n,
        "clips_per_batch": math.ceil(len(clips) / n),
        "model":           model,
    }
    return result


def select_narrative_clips(
    clips: list[dict],
    theme: str,
    duration: int = 90,
    model: str = "haiku",
    relevant_clips: list[dict] | None = None,   # theme-searched clips (full transcripts)
    batch_summaries: list[str] | None = None,   # all-clips overview for context
    csv_text: str | None = None,                # full CSV of all transcripts (preferred)
) -> dict:
    """
    Select specific in/out timestamps for a narrative cut matching the theme.

    Primary strategy: CSV pool (timestamps are exact floats — no conversion).
    The CSV is built in main.py from DB data and saved to disk for the user too.
    """
    # ── Build project context header from batch summaries ──
    if batch_summaries:
        n = len(batch_summaries)
        joined = "\n\n---\n\n".join(f"Batch {i+1}/{n}:\n{s}" for i, s in enumerate(batch_summaries))
        if len(joined) > BATCH_SUM_CUT:
            joined = joined[:BATCH_SUM_CUT] + "\n\n[... batch summaries truncated]"
        project_context = (
            f"PROJECT OVERVIEW — ALL {len(clips)} CLIPS SUMMARISED IN {n} BATCHES:\n"
            f"(Use this to understand the full scope of footage. The CSV rows below "
            f"are the clips most relevant to the theme — use ONLY those.)\n\n{joined}\n"
        )
    else:
        project_context = ""

    # ── Build CSV pool ──
    # Order: relevant (keyword-matched) clips first, then extras by dialogue volume.
    if relevant_clips:
        pool = list(relevant_clips)
        pool_names = {c["filename"] for c in pool}
        extras = sorted(
            [c for c in clips if c["filename"] not in pool_names],
            key=lambda c: len(c.get("segments", [])), reverse=True,
        )
        pool += extras
        pool_label = (
            f"# {len(relevant_clips)} clips matched theme keyword search; "
            f"{len(extras)} extra clips added by dialogue volume\n"
        )
    else:
        pool = sorted(clips, key=lambda c: len(c.get("segments", [])), reverse=True)
        pool_label = ""

    # Build CSV from the ordered pool, limited to CUT_CHARS.
    # IMPORTANT: cap CHARS per clip so a single long clip can't hog the entire pool.
    # Per-clip budget = CUT_CHARS / target_clip_count (aim for 12-25 different files in pool).
    import io, csv as _csv
    csv_buf  = io.StringIO()
    csv_w    = _csv.writer(csv_buf, lineterminator="\n")
    csv_w.writerow(["filename", "start_s", "end_s", "text"])
    pool_valid: dict[str, float] = {}
    csv_header_len = len("filename,start_s,end_s,text\n")
    csv_total = csv_header_len

    TARGET_CLIPS_IN_POOL = max(8, min(20, len(pool)))
    per_clip_cap = max(800, CUT_CHARS // TARGET_CLIPS_IN_POOL)

    log.info(f"Cut CSV: aiming for {TARGET_CLIPS_IN_POOL} clips · {per_clip_cap}ch each")

    for clip in pool:
        fname = clip["filename"]
        clip_chars = 0
        for seg in clip.get("segments", []):
            row = [
                fname,
                round(float(seg.get("start", 0)), 3),
                round(float(seg.get("end",   0)), 3),
                seg.get("text", "").strip(),
            ]
            est = len(fname) + 20 + len(str(row[3]))

            # Per-clip cap — once this clip has used its share, move on
            if clip_chars + est > per_clip_cap:
                break
            # Global cap (overall CSV size)
            if csv_total + est > CUT_CHARS:
                break

            csv_w.writerow(row)
            pool_valid[fname] = max(pool_valid.get(fname, 0), float(row[1]))
            csv_total  += est
            clip_chars += est

        # Stop early if global cap reached
        if csv_total >= CUT_CHARS:
            break

    if not pool_valid:
        raise ValueError("No transcript content available.")

    csv_buf.seek(0)
    csv_pool = csv_buf.getvalue()

    log.info(
        f"Cut CSV pool: {len(pool_valid)} clips, ~{csv_total:,} chars "
        f"| {len(clips)} total clips in project"
    )

    format_instructions = _get_format_instructions(duration)
    prompt = NARRATIVE_CUT_PROMPT.format(
        theme=theme, duration=duration, project_context=project_context,
        format_instructions=format_instructions,
    ) + pool_label + "\n" + csv_pool

    raw, backend = _call_ai(prompt, max_tokens=2200, temperature=0.4, model=model)
    log.info(f"Narrative cut selected via {backend}")

    result = _parse_json(raw)
    validated_clips = []
    for c in result.get("clips", []):
        fname = c.get("filename", "")

        # ── Validate: filename must be in the CSV pool ──
        if fname not in pool_valid:
            log.warning(
                f"Cut validation: '{fname}' not in CSV pool "
                f"(AI hallucinated from batch summary) — skipping"
            )
            continue

        # ── Timestamps are floats from the CSV — just cast, no conversion ──
        try:
            in_time  = float(c.get("in_time",  0))
            out_time = float(c.get("out_time", in_time + 10))
        except (ValueError, TypeError):
            log.warning(f"Cut validation: bad timestamps for '{fname}' — skipping")
            continue

        # Enforce duration bounds (4–25 s) while keeping in_time anchored
        dur = out_time - in_time
        if dur < 4:
            out_time = in_time + 4
        elif dur > 25:
            out_time = in_time + 25

        # Verify in_time is within this clip's actual transcript range (±30s tolerance)
        clip_max_t = pool_valid[fname]
        if in_time > clip_max_t + 30:
            log.warning(
                f"Cut validation: '{fname}' in_time={in_time:.1f}s > "
                f"clip max ~{clip_max_t:.1f}s — skipping (bad timestamp)"
            )
            continue

        c["in_time"]  = in_time
        c["out_time"] = out_time
        validated_clips.append(c)

    result["clips"] = validated_clips
    return result


def chat_about_project(
    clips: list[dict],
    project_name: str,
    messages: list[dict],
    bible: dict | None = None,
    batch_summaries: list[str] | None = None,
    relevant_clips: list[dict] | None = None,   # query-matched clips with full transcripts
    model: str = "haiku",
) -> str:
    """
    Chat with full project coverage:
    - Story Bible (synthesised narrative overview)
    - All batch summaries (every clip summarised)
    - Query-matched transcript excerpts (keyword search result)
    - Sample raw transcripts from richest clips (fallback)
    """
    # ── Bible section ──
    if bible:
        bible_parts = []
        if bible.get("story_arc"):
            bible_parts.append(f"STORY ARC: {bible['story_arc']}")
        if bible.get("central_conflict"):
            bible_parts.append(f"CENTRAL CONFLICT: {bible['central_conflict']}")
        if bible.get("directors_notes"):
            bible_parts.append(f"DIRECTOR'S NOTES: {bible['directors_notes']}")
        if bible.get("themes"):
            bible_parts.append(f"THEMES: {', '.join(bible['themes'])}")
        if bible.get("must_use_clips"):
            clips_str = "; ".join(f"{c['filename']} ({c['reason']})" for c in bible["must_use_clips"][:10])
            bible_parts.append(f"MUST-USE CLIPS: {clips_str}")
        bible_section = "STORY BIBLE SUMMARY:\n" + "\n".join(bible_parts) if bible_parts else ""
    else:
        bible_section = "(Story Bible not yet generated — run Analysis first for richer context)"

    # ── Batch summaries — covers ALL footage ──
    if batch_summaries:
        n_batches = len(batch_summaries)
        joined = "\n\n---\n\n".join(
            f"Batch {i+1}/{n_batches}:\n{s}" for i, s in enumerate(batch_summaries)
        )
        if len(joined) > 50_000:
            joined = joined[:50_000] + "\n\n[... summaries truncated]"
        batch_summary_text = joined
    else:
        batch_summary_text = "(No batch summaries yet — generate the Story Bible first to unlock full coverage)"
        n_batches = 0

    # ── Query-matched transcript excerpts (injected per-query) ──
    if relevant_clips:
        rel_parts = []
        rel_total = 0
        for c in relevant_clips:
            txt = _clip_text(c)
            if rel_total + len(txt) > 25_000:
                break
            rel_parts.append(txt)
            rel_total += len(txt)
        relevant_section = (
            f"The following {len(rel_parts)} clip(s) were found by searching the transcripts "
            f"for keywords in this question. They contain the actual word-for-word dialogue "
            f"with timestamps you can use to cite moments:\n\n" + "\n\n".join(rel_parts)
        ) if rel_parts else "(No keyword matches found — rely on batch notes above)"
    else:
        relevant_section = "(No keyword search performed for this message)"

    # ── Raw transcripts from richest clips (fallback context) ──
    sorted_clips = sorted(clips, key=lambda c: len(c.get("segments", [])), reverse=True)
    raw_parts, raw_total = [], 0
    # Skip clips already in relevant_clips to avoid duplication
    relevant_names = {c['filename'] for c in (relevant_clips or [])}
    for clip in sorted_clips:
        if clip['filename'] in relevant_names:
            continue
        txt = _clip_text(clip)
        if raw_total + len(txt) > CHAT_CHARS:
            break
        raw_parts.append(txt)
        raw_total += len(txt)
    raw_transcripts = "\n\n".join(raw_parts) if raw_parts else "(none beyond search results)"

    system = CHAT_SYSTEM.format(
        project_name=project_name,
        bible_section=bible_section,
        n_batches=n_batches,
        batch_summaries=batch_summary_text,
        relevant_section=relevant_section,
        raw_transcripts=raw_transcripts,
    )

    trimmed = messages[-10:] if len(messages) > 10 else messages
    text, backend = _call_ai(
        trimmed[-1]["content"] if trimmed else "Hello",
        system=system,
        max_tokens=2000,
        temperature=0.5,
        history=trimmed[:-1] if len(trimmed) > 1 else None,
        model=model,
    )
    log.info(f"Chat response via {backend} | {len(relevant_clips or [])} relevant clips injected")
    return text
