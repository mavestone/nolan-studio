import asyncio
import logging
import os
import re
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
# override=True so .env wins over stale/empty shell variables
load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("nolan")

from fastapi import FastAPI, BackgroundTasks, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import (
    init_db,
    create_project, get_projects, get_project, delete_project,
    add_project_folder, get_project_folders, update_folder_scanned, remove_project_folder,
    upsert_file, update_file_status, save_segments, save_analysis,
    get_project_files, get_file, get_transcript, get_analysis, search_transcripts,
    save_project_analysis, get_project_analysis, get_project_stats,
    save_batch_checkpoint, get_batch_checkpoints, clear_batch_checkpoints,
    get_checkpoint_status,
    save_chat_message, get_chat_messages, get_pinned_chat_messages,
    set_chat_message_pinned, clear_chat_messages, get_all_batch_summaries,
    check_analysis_stale, get_theme_relevant_clips,
    save_scenes, get_scenes, has_scenes, save_poster_path, get_project_poster_paths,
    search_project_scenes, update_file_scene_summary, update_scene_classification,
)
from transcriber import scan_folder, transcribe_file, get_video_duration, NoAudioError
from analyzer import analyze_project, chat_about_project, select_narrative_clips
from fcpxml import generate_fcpxml
from scene_detector import detect_scenes, extract_clip_poster, classify_shot_with_claude

job_progress: dict[int | str, dict] = {}
_stop_requested = False
_telegram_task = None    # strong reference — keeps the bot from being GC'd


async def reset_stuck_states():
    """Reset clips left mid-process from a previous crash/restart."""
    import aiosqlite
    from database import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        # transcribed/analyzing → done (they finished but state was stale)
        cur = await db.execute(
            "UPDATE files SET status = 'done' WHERE status IN ('analyzing', 'transcribed')"
        )
        done_count = cur.rowcount or 0
        # transcribing/extracting/queued → pending (re-queueable)
        cur = await db.execute(
            "UPDATE files SET status = 'pending' WHERE status IN ('transcribing', 'extracting_audio', 'queued')"
        )
        pending_count = cur.rowcount or 0
        if done_count or pending_count:
            log.info(f"Startup: reset {done_count} stuck → done, {pending_count} stuck → pending")
        await db.commit()


async def reclassify_all_scenes():
    """
    Re-run the (improved) OpenCV scene classifier on every existing scene that
    has a thumbnail. Fixes indoor/outdoor on scenes detected before v36.
    Skips AI-classified scenes (those have richer location data we shouldn't overwrite).
    """
    import aiosqlite, json
    from database import DB_PATH
    from pathlib import Path
    from scene_detector import _classify_shot_opencv

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT id, file_id, thumbnail_path
            FROM scenes
            WHERE thumbnail_path IS NOT NULL AND (ai_classified IS NULL OR ai_classified = 0)
        """) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    if not rows:
        return

    log.info(f"Reclassify: re-running setting/shot heuristic on {len(rows)} scenes…")
    loop = asyncio.get_event_loop()
    done = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for r in rows:
            thumb = Path("static") / (r["thumbnail_path"] or "")
            if not thumb.exists():
                continue
            try:
                cls = await loop.run_in_executor(None, lambda p=str(thumb): _classify_shot_opencv(p))
                await db.execute("""
                    UPDATE scenes SET
                        shot_type = ?, shot_size = ?, shot_angle = ?,
                        setting = ?, tags = ?
                    WHERE id = ?
                """, (
                    cls.get("shot_type"), cls.get("shot_size"), cls.get("shot_angle"),
                    cls.get("setting"), json.dumps(cls.get("tags") or []),
                    r["id"],
                ))
                done += 1
                if done % 200 == 0:
                    await db.commit()
                    log.info(f"Reclassify: {done}/{len(rows)}")
            except Exception as e:
                log.debug(f"Reclassify scene {r['id']} failed: {e}")
        await db.commit()
    log.info(f"Reclassify: {done} scenes updated")

    # Refresh per-file summaries
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT DISTINCT file_id FROM scenes") as cur:
            fids = [r[0] for r in await cur.fetchall()]
    from database import get_scenes
    for fid in fids:
        scs = await get_scenes(fid)
        if scs:
            await update_file_scene_summary(fid, scs)
    log.info(f"Reclassify: file summaries refreshed for {len(fids)} files")


async def cleanup_db_hidden_and_dupes():
    """
    One-off cleanup on every startup:
      • DELETE rows whose filename starts with `._` (macOS metadata sidecars)
      • DELETE rows whose path no longer exists AND another row with same filename DOES exist
        (i.e. the file moved — keep the entry that still resolves on disk)
    """
    import aiosqlite, os
    from database import DB_PATH

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # 1. Hidden / metadata files
        cur = await db.execute("DELETE FROM files WHERE filename LIKE '._%' OR filename LIKE '.%'")
        hidden_removed = cur.rowcount or 0

        # 2. Stale duplicates (same filename, multiple entries, only one resolves)
        async with db.execute("""
            SELECT id, project_id, filename, path
            FROM files
            WHERE filename IN (
                SELECT filename FROM files GROUP BY project_id, filename HAVING COUNT(*) > 1
            )
            ORDER BY project_id, filename, id
        """) as c:
            rows = [dict(r) for r in await c.fetchall()]

        # Group by (project_id, filename)
        from collections import defaultdict
        groups = defaultdict(list)
        for r in rows:
            groups[(r["project_id"], r["filename"])].append(r)

        dupes_removed = 0
        for (_proj, _fname), entries in groups.items():
            existing = [r for r in entries if r["path"] and os.path.exists(r["path"])]
            missing  = [r for r in entries if not (r["path"] and os.path.exists(r["path"]))]
            # If at least one entry has a valid on-disk path, delete the broken ones
            if existing and missing:
                for r in missing:
                    await db.execute("DELETE FROM files WHERE id = ?", (r["id"],))
                    dupes_removed += 1
            # If multiple valid entries (true dupes), keep the lowest id
            elif len(existing) > 1:
                for r in existing[1:]:
                    await db.execute("DELETE FROM files WHERE id = ?", (r["id"],))
                    dupes_removed += 1

        await db.commit()

    if hidden_removed:
        log.info(f"Cleanup: removed {hidden_removed} hidden/metadata file rows")
    if dupes_removed:
        log.info(f"Cleanup: removed {dupes_removed} duplicate/moved-file rows")


async def migrate_error_clips_to_silent():
    """One-time migration: clips marked 'error' that actually have no audio → 'silent'."""
    import aiosqlite
    from database import DB_PATH
    from transcriber import has_audio_stream

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, path FROM files WHERE status = 'error'") as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    if not rows:
        return

    log.info(f"Migration: checking {len(rows)} error clips for silent reclassification…")
    loop = asyncio.get_event_loop()
    moved = 0
    async with aiosqlite.connect(DB_PATH) as db:
        for r in rows:
            try:
                has_audio = await loop.run_in_executor(None, lambda p=r["path"]: has_audio_stream(p))
                if not has_audio:
                    await db.execute("UPDATE files SET status = 'silent' WHERE id = ?", (r["id"],))
                    moved += 1
            except Exception:
                pass
        await db.commit()
    if moved:
        log.info(f"Migration: reclassified {moved} error → silent")


async def backfill_scene_classifications():
    """Classify any existing scenes that don't have shot_type set yet."""
    import aiosqlite, json
    from database import DB_PATH
    from scene_detector import classify_shot
    from pathlib import Path

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, thumbnail_path FROM scenes WHERE shot_type IS NULL AND thumbnail_path IS NOT NULL"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

    if not rows:
        return

    log.info(f"Backfill: classifying {len(rows)} unclassified scenes…")
    loop = asyncio.get_event_loop()
    classified = 0

    async with aiosqlite.connect(DB_PATH) as db:
        for row in rows:
            thumb_abs = Path("static") / row["thumbnail_path"]
            if not thumb_abs.exists():
                continue
            try:
                result = await loop.run_in_executor(
                    None, lambda p=str(thumb_abs): classify_shot(p)
                )
                await db.execute(
                    "UPDATE scenes SET shot_type=?, tags=? WHERE id=?",
                    (result["shot_type"], json.dumps(result["tags"]), row["id"])
                )
                classified += 1
            except Exception as e:
                log.debug(f"Backfill classify failed for scene {row['id']}: {e}")
        await db.commit()

    log.info(f"Backfill: classified {classified} scenes")

    # ── Also backfill file-level scene summaries ──────────────────────────
    import aiosqlite, json
    from database import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT DISTINCT f.id FROM files f
            JOIN scenes s ON s.file_id = f.id
            WHERE f.primary_shot_type IS NULL AND s.shot_type IS NOT NULL
        """) as cur:
            file_ids = [r[0] for r in await cur.fetchall()]

    if file_ids:
        log.info(f"Backfill: summarising shot data on {len(file_ids)} files…")
        for fid in file_ids:
            scenes = await get_scenes(fid)
            if scenes:
                await update_file_scene_summary(fid, scenes)
        log.info(f"Backfill: file shot summaries done")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await reset_stuck_states()
    asyncio.create_task(backfill_scene_classifications())
    asyncio.create_task(migrate_error_clips_to_silent())
    asyncio.create_task(cleanup_db_hidden_and_dupes())
    asyncio.create_task(reclassify_all_scenes())

    # ── Telegram bot (optional, if token is set in settings.json) ─────
    settings = _load_settings()
    token = settings.get("telegram_token")
    global _telegram_task
    _telegram_task = None
    if token:
        try:
            from telegram_bot import run_telegram_bot
            # IMPORTANT: hold strong reference so GC doesn't kill the task
            _telegram_task = asyncio.create_task(
                run_telegram_bot(token, _load_settings),
                name="telegram-bot",
            )
            log.info("Telegram bot scheduled")
        except Exception as e:
            log.warning(f"Telegram bot failed to start: {e}")
    else:
        log.info("Telegram bot disabled (no token in settings.json)")

    yield

    # Shutdown
    if _telegram_task and not _telegram_task.done():
        _telegram_task.cancel()


app = FastAPI(title="Nolan", lifespan=lifespan)


# ── Models ──

class ProjectCreate(BaseModel):
    name: str


class FolderAdd(BaseModel):
    path: str


class TranscribeRequest(BaseModel):
    model_size: str = "base"


class BatchRequest(BaseModel):
    model_size: str = "base"


class ChatRequest(BaseModel):
    messages: list[dict]   # [{role: "user"|"assistant", content: str}]
    ai_model: str = "haiku"


class AnalyseRequest(BaseModel):
    ai_model: str = "haiku"   # "haiku" | "sonnet" | "gemini" | "groq"


class NarrativeCutRequest(BaseModel):
    theme:    str
    duration: int = 90    # seconds
    ai_model: str = "haiku"


# ── Background jobs ──

async def run_transcription(file_id: int, path: str, filename: str, model_size: str):
    """Transcribe one clip. Analysis is handled separately in run_batch_analysis."""
    global _stop_requested
    if _stop_requested:
        await update_file_status(file_id, "pending")
        job_progress.pop(file_id, None)
        return

    job_progress[file_id] = {
        "status": "transcribing", "progress": 0,
        "stage": "Loading Whisper model…", "filename": filename,
    }
    log.info(f"[{filename}] Transcribing (model={model_size})")

    is_silent = False

    try:
        loop = asyncio.get_event_loop()

        existing = await get_transcript(file_id)
        if existing:
            log.info(f"[{filename}] Already transcribed ({len(existing)} segments), skipping Whisper")
        else:
            await update_file_status(file_id, "transcribing")

            def on_progress(stage: str):
                job_progress[file_id]["status"] = stage
                if stage == "extracting_audio":
                    job_progress[file_id]["stage"] = "Extracting audio…"
                    job_progress[file_id]["progress"] = 10
                elif stage == "transcribing":
                    job_progress[file_id]["stage"] = "Transcribing with Whisper…"
                    job_progress[file_id]["progress"] = 25

            try:
                segments = await loop.run_in_executor(
                    None, lambda p=path, m=model_size: transcribe_file(p, m, on_progress)
                )
            except NoAudioError:
                log.info(f"[{filename}] · no audio stream → marking silent")
                segments = []
                is_silent = True

            if not segments:
                is_silent = True
                log.info(f"[{filename}] · 0 segments → marking silent")
            else:
                log.info(f"[{filename}] ✓ {len(segments)} segments")
                job_progress[file_id]["stage"] = f"Saving {len(segments)} segments…"
                job_progress[file_id]["progress"] = 45
                await save_segments(file_id, segments)

        job_progress[file_id]["stage"] = "Reading video metadata…"
        job_progress[file_id]["progress"] = 55
        duration = await loop.run_in_executor(None, lambda p=path: get_video_duration(p))
        now = datetime.now(timezone.utc).isoformat()
        if is_silent:
            await update_file_status(file_id, "silent", duration_seconds=duration, transcribed_at=now)
            job_progress[file_id] = {
                "status": "silent", "progress": 60,
                "stage": "Silent · detecting scenes…", "filename": filename,
            }
        else:
            await update_file_status(file_id, "transcribed", duration_seconds=duration, transcribed_at=now)
            job_progress[file_id] = {
                "status": "transcribed", "progress": 60,
                "stage": "Detecting scenes…", "filename": filename,
            }

        # ── Scene detection ──────────────────────────────────────
        already = await has_scenes(file_id)
        if not already:
            try:
                job_progress[file_id]["stage"] = "Detecting scene cuts…"
                job_progress[file_id]["progress"] = 70
                tx_segs = await get_transcript(file_id)
                scenes = await loop.run_in_executor(
                    None,
                    lambda fid=file_id, p=path, t=tx_segs: detect_scenes(fid, p, transcript_segments=t),
                )
                if scenes:
                    job_progress[file_id]["stage"] = f"Classifying {len(scenes)} scenes…"
                    job_progress[file_id]["progress"] = 85
                    await save_scenes(file_id, scenes)
                    await update_file_scene_summary(file_id, scenes)
                    first_thumb = next((s["thumbnail_path"] for s in scenes if s.get("thumbnail_path")), None)
                    if first_thumb:
                        await save_poster_path(file_id, first_thumb)
                log.info(f"[{filename}] Scenes: {len(scenes)} detected")
            except Exception as se:
                log.warning(f"[{filename}] Scene detection failed (non-fatal): {se}")

        job_progress[file_id]["progress"] = 100
        job_progress[file_id]["stage"] = "Complete"

    except Exception as e:
        log.error(f"[{filename}] Transcription ERROR: {e}")
        await update_file_status(file_id, "error")
        job_progress[file_id] = {"status": "error", "error": str(e)}


async def run_batch_analysis(file_ids: list[int], project_id: int | None = None):
    """Phase 2: mark transcribed clips as done. No per-clip AI — use Story Bible for that."""
    global _stop_requested
    log.info(f"Marking {len(file_ids)} transcribed clips as done")
    for file_id in file_ids:
        if _stop_requested:
            break
        db_file = await get_file(file_id)
        if not db_file:
            continue
        # Leave 'silent' and 'done' alone — they are already terminal states
        if db_file["status"] in ("done", "silent"):
            continue
        await update_file_status(file_id, "done")
        job_progress[file_id] = {"status": "done", "progress": 100}



async def run_project_bible(project_id: int, ai_model: str = "haiku"):
    """Build project-level story bible via map-reduce. Checkpoints every batch so WiFi drops resume."""
    try:
        log.info(f"[Project {project_id}] Building story bible (model={ai_model})…")
        job_progress["__project_analysis__"] = {
            "status": "analyzing", "batch_current": 0, "batch_total": 0,
            "stage": "Loading transcripts…", "model": ai_model,
        }

        all_project_files = await get_project_files(project_id)
        clips = []
        for pf in all_project_files:
            db_segs = await get_transcript(pf["id"])
            if db_segs:
                segs = [{"start": s["start_time"], "end": s["end_time"], "text": s["text"]} for s in db_segs]
                clips.append({"filename": pf["filename"], "segments": segs})

        if not clips:
            job_progress["__project_analysis__"] = {"status": "error", "error": "No transcripts found"}
            return

        project      = await get_project(project_id)
        project_name = project["name"] if project else str(project_id)

        # ── Compute expected batch count to check checkpoint validity ──
        from analyzer import _batch_clips, BATCH_CHARS
        expected_batches = len(_batch_clips(clips, BATCH_CHARS))

        # Load any saved checkpoints that match the current batch layout
        existing = await get_batch_checkpoints(project_id, expected_batches)
        if existing:
            log.info(f"[Project {project_id}] Resuming — {len(existing)}/{expected_batches} batches already done")

        log.info(f"[Project {project_id}] {len(clips)} clips, {expected_batches} batches")

        def on_progress(current, total, stage):
            job_progress["__project_analysis__"] = {
                "status":        "analyzing",
                "batch_current": current,
                "batch_total":   total,
                "stage":         stage,
            }
            log.info(f"[Bible] {stage}")

        # Sync checkpoint saver (runs inside thread executor)
        import asyncio as _asyncio
        loop = asyncio.get_event_loop()

        saved_summaries_cache = dict(existing)

        def on_batch_done_sync(batch_num, total, summary):
            """Called from the background thread — schedule DB save on the event loop."""
            saved_summaries_cache[batch_num] = summary
            future = asyncio.run_coroutine_threadsafe(
                save_batch_checkpoint(project_id, batch_num, total, summary),
                loop,
            )
            try:
                future.result(timeout=10)
            except Exception as e:
                log.warning(f"[Bible] Failed to save checkpoint for batch {batch_num}: {e}")

        bible = await loop.run_in_executor(
            None,
            lambda c=clips, pn=project_name, ex=existing, m=ai_model: analyze_project(
                c, pn,
                progress_cb=on_progress,
                existing_summaries=ex,
                on_batch_done=on_batch_done_sync,
                model=m,
            ),
        )

        await save_project_analysis(project_id, bible)
        # NOTE: keep batch checkpoints — they're used as full-coverage chat context

        meta = bible.get("_meta", {})
        log.info(
            f"[Project {project_id}] Story bible complete — "
            f"{meta.get('total_clips', len(clips))} clips in {meta.get('batches', '?')} batches"
        )
        job_progress["__project_analysis__"] = {"status": "done"}

    except Exception as e:
        log.error(f"[Project {project_id}] Story bible ERROR: {e}")
        # Preserve checkpoint count in error state so UI can show resume info
        cp = await get_checkpoint_status(project_id)
        job_progress["__project_analysis__"] = {
            "status":        "error",
            "error":         str(e),
            "batches_saved": cp["last_batch"]   if cp else 0,
            "batches_total": cp["total_batches"] if cp else 0,
        }


# ── Projects ──

@app.post("/api/projects")
async def api_create_project(req: ProjectCreate):
    try:
        pid = await create_project(req.name)
        return {"id": pid, "name": req.name}
    except Exception:
        raise HTTPException(400, f"Project '{req.name}' already exists")


@app.get("/api/projects")
async def api_list_projects():
    return await get_projects()


@app.delete("/api/projects/{project_id}")
async def api_delete_project(project_id: int):
    await delete_project(project_id)
    return {"ok": True}


# ── Folders ──

@app.get("/api/pick-folder")
async def pick_folder():
    # Use osascript (always available on macOS) instead of tkinter which
    # is NOT bundled with Homebrew Python and silently fails on many installs.
    applescript = (
        'tell application "Finder"\n'
        '    activate\n'
        '    set folderRef to choose folder with prompt "Select footage folder"\n'
        '    set folderPath to POSIX path of folderRef\n'
        'end tell\n'
        'return folderPath'
    )
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True, text=True, timeout=120
        )
    )
    path = result.stdout.strip()
    if not path:
        raise HTTPException(400, "No folder selected")
    # osascript returns paths with trailing slash — normalise
    return {"path": path.rstrip("/")}


@app.post("/api/projects/{project_id}/folders")
async def api_add_folder(project_id: int, req: FolderAdd):
    if not os.path.isdir(req.path):
        raise HTTPException(400, f"Not a directory: {req.path}")
    folder_id = await add_project_folder(project_id, req.path)
    return {"id": folder_id, "path": req.path}


@app.get("/api/projects/{project_id}/folders")
async def api_get_folders(project_id: int):
    return await get_project_folders(project_id)


@app.delete("/api/projects/{project_id}/folders/{folder_id}")
async def api_remove_folder(project_id: int, folder_id: int):
    await remove_project_folder(folder_id)
    return {"ok": True}


# ── Scan ──

@app.post("/api/projects/{project_id}/folders/{folder_id}/scan")
async def api_scan_folder(project_id: int, folder_id: int):
    folders = await get_project_folders(project_id)
    folder = next((f for f in folders if f["id"] == folder_id), None)
    if not folder:
        raise HTTPException(404, "Folder not found")

    job_progress["__scan__"] = {"status": "scanning", "found": 0}
    log.info(f"Scanning: {folder['path']}")

    loop = asyncio.get_event_loop()
    try:
        found = await loop.run_in_executor(None, lambda: scan_folder(folder["path"]))
    except Exception as e:
        job_progress["__scan__"] = {"status": "error", "error": str(e)}
        raise HTTPException(500, str(e))

    job_progress["__scan__"] = {"status": "registering", "found": len(found)}

    new_count = 0
    for f in found:
        _, is_new = await upsert_file(project_id, f["path"], f["filename"], f["size_bytes"])
        if is_new:
            new_count += 1

    await update_folder_scanned(folder_id, len(found))
    job_progress["__scan__"] = {"status": "done", "found": len(found), "new": new_count}
    log.info(f"Scan complete — {len(found)} clips found, {new_count} new")
    return {"found": len(found), "new": new_count}


@app.get("/api/scan/status")
async def scan_status():
    return job_progress.get("__scan__", {"status": "idle"})


# ── Batch process ──

@app.post("/api/projects/{project_id}/process")
async def api_batch_process(project_id: int, req: BatchRequest, background_tasks: BackgroundTasks):
    global _stop_requested
    _stop_requested = False

    files = await get_project_files(project_id)

    # 1. Clips that need full transcription
    to_transcribe = []
    for f in files:
        if f["status"] in ("done", "transcribing", "queued", "silent"):
            continue
        job_progress[f["id"]] = {"status": "queued", "progress": 0,
                                 "stage": "Queued for transcription…", "filename": f["filename"]}
        background_tasks.add_task(run_transcription, f["id"], f["path"], f["filename"], req.model_size)
        to_transcribe.append(f["id"])

    if to_transcribe:
        background_tasks.add_task(run_batch_analysis, to_transcribe, project_id)

    # 2. Done/silent clips missing scenes — queue scene-only jobs to fill thumbnails
    to_scene_only = []
    for f in files:
        if f["status"] not in ("done", "silent"):
            continue
        if not await has_scenes(f["id"]):
            to_scene_only.append(f)

    if to_scene_only:
        log.info(f"Queueing scene-only detection for {len(to_scene_only)} clips missing thumbnails")
        async def _scene_only_runner():
            loop = asyncio.get_event_loop()
            for idx, f in enumerate(to_scene_only, 1):
                if _stop_requested:
                    break
                fid = f["id"]
                job_progress[fid] = {
                    "status": "transcribed", "progress": 70,
                    "stage": f"Detecting scenes ({idx}/{len(to_scene_only)})…",
                    "filename": f["filename"],
                }
                try:
                    # Use auto-resolved path in case the folder was renamed
                    from fcpxml import _resolve_actual_path
                    real_path = _resolve_actual_path(f["path"])
                    tx_segs = await get_transcript(fid)
                    scenes = await loop.run_in_executor(
                        None,
                        lambda fi=fid, p=real_path, t=tx_segs: detect_scenes(fi, p, transcript_segments=t),
                    )
                    if scenes:
                        await save_scenes(fid, scenes)
                        await update_file_scene_summary(fid, scenes)
                        first_thumb = next(
                            (s["thumbnail_path"] for s in scenes if s.get("thumbnail_path")), None
                        )
                        if first_thumb:
                            await save_poster_path(fid, first_thumb)
                        log.info(f"[{f['filename']}] retro-detect: {len(scenes)} scenes")
                    job_progress[fid] = {"status": f["status"], "progress": 100,
                                          "stage": "Complete", "filename": f["filename"]}
                except Exception as e:
                    log.warning(f"[{f['filename']}] retro-scene failed: {e}")
                    job_progress.pop(fid, None)
            log.info("Scene-only backfill finished")

        background_tasks.add_task(_scene_only_runner)

    total = len(to_transcribe) + len(to_scene_only)
    log.info(f"Queued {len(to_transcribe)} transcriptions + {len(to_scene_only)} scene-detections")
    return {
        "queued": total,
        "file_ids": to_transcribe,
        "transcribing": len(to_transcribe),
        "scene_only": len(to_scene_only),
    }


@app.post("/api/stop")
async def api_stop():
    """
    Cooperative stop:
      • Sets _stop_requested = True (all background loops bail on next iteration)
      • Reverts every clip currently in 'queued'/'transcribing'/'extracting_audio' to 'pending'
        so the UI immediately shows them as stoppable
      • Clears in-flight job_progress entries
    """
    global _stop_requested
    _stop_requested = True

    import aiosqlite
    from database import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE files SET status = 'pending' "
            "WHERE status IN ('queued', 'transcribing', 'extracting_audio')"
        )
        reverted = cur.rowcount or 0
        await db.commit()

    # Clear progress for any non-active jobs
    cleared = 0
    for k in list(job_progress.keys()):
        if isinstance(k, int):
            st = job_progress[k].get("status")
            if st in ("queued", "transcribing", "extracting_audio"):
                job_progress.pop(k, None)
                cleared += 1

    log.info(f"Stop requested — reverted {reverted} clips, cleared {cleared} job-progress entries")
    return {"ok": True, "reverted": reverted, "cleared": cleared}


@app.post("/api/admin/reclassify-scenes")
async def admin_reclassify(background_tasks: BackgroundTasks):
    """Trigger a manual reclassification of all scenes (indoor/outdoor + tags)."""
    background_tasks.add_task(reclassify_all_scenes)
    return {"queued": True}


@app.get("/api/admin/version")
async def admin_version():
    """Return the current git commit + the latest available on origin/main."""
    import subprocess as _sub
    root = str(Path(__file__).resolve().parent)
    def _git(*args):
        try:
            return _sub.check_output(["git", "-C", root, *args], stderr=_sub.DEVNULL).decode().strip()
        except Exception:
            return None
    current      = _git("rev-parse", "--short", "HEAD")
    current_msg  = _git("log", "-1", "--format=%s")
    return {
        "current":     current,
        "current_msg": current_msg,
        "branch":      _git("rev-parse", "--abbrev-ref", "HEAD"),
        "is_git_repo": current is not None,
    }


@app.post("/api/admin/update")
async def admin_update():
    """
    Pull latest from GitHub origin/main and rebuild the .app.
    Requires the source to be a git checkout. Returns the new commit info;
    user must restart the server for code changes to take effect.
    """
    import subprocess as _sub
    root = str(Path(__file__).resolve().parent)

    if not (Path(root) / ".git").is_dir():
        raise HTTPException(400, "Not a git checkout — cannot auto-update.")

    def _run(*args, check=True):
        return _sub.check_output(args, cwd=root, stderr=_sub.STDOUT).decode()

    try:
        old = _sub.check_output(["git", "-C", root, "rev-parse", "--short", "HEAD"]).decode().strip()
        _run("git", "-C", root, "fetch", "--quiet", "origin", "main")
        # Hard-update to origin/main — wipes local-only edits, which is what we want for an "update from GitHub" button
        _run("git", "-C", root, "reset", "--hard", "origin/main")
        new = _sub.check_output(["git", "-C", root, "rev-parse", "--short", "HEAD"]).decode().strip()
        new_msg = _sub.check_output(["git", "-C", root, "log", "-1", "--format=%s"]).decode().strip()
        # Rebuild the .app bundle so the launcher script reflects any changes
        try:
            target = "/Applications" if Path("/Applications/Nolan.app").is_dir() else str(Path.home() / "Applications")
            _sub.run(["bash", str(Path(root) / "make-app.sh"), target], cwd=root, check=False, stdout=_sub.DEVNULL, stderr=_sub.DEVNULL)
        except Exception:
            pass
        # Bump Python deps if requirements.txt changed
        try:
            venv_pip = Path(root) / ".venv" / "bin" / "pip"
            if venv_pip.exists():
                _sub.run([str(venv_pip), "install", "-r", str(Path(root) / "requirements.txt"), "--quiet"], cwd=root, check=False)
        except Exception:
            pass
        return {
            "ok": True,
            "old_commit": old,
            "new_commit": new,
            "new_message": new_msg,
            "restart_required": old != new,
        }
    except _sub.CalledProcessError as e:
        raise HTTPException(500, f"Update failed: {e.output.decode()[-300:] if e.output else e}")


# ── Files ──

@app.get("/api/projects/{project_id}/files")
async def api_project_files(project_id: int):
    files = await get_project_files(project_id)
    for f in files:
        f["progress"] = job_progress.get(f["id"])
    return files


@app.post("/api/files/{file_id}/transcribe")
async def transcribe_one(file_id: int, req: TranscribeRequest, background_tasks: BackgroundTasks):
    f = await get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    job_progress[file_id] = {"status": "queued", "progress": 0}
    background_tasks.add_task(run_transcription, file_id, f["path"], f["filename"], req.model_size)
    background_tasks.add_task(run_batch_analysis, [file_id], f["project_id"])
    return {"file_id": file_id, "status": "queued"}


@app.get("/api/files/{file_id}")
async def get_file_detail(file_id: int):
    f = await get_file(file_id)
    if not f:
        raise HTTPException(404)
    f["progress"] = job_progress.get(file_id)
    return f


@app.get("/api/files/{file_id}/transcript")
async def file_transcript(file_id: int):
    return await get_transcript(file_id)


@app.get("/api/files/{file_id}/analysis")
async def file_analysis(file_id: int):
    data = await get_analysis(file_id)
    if not data:
        raise HTTPException(404, "No analysis yet")
    return data


@app.get("/api/projects/{project_id}/analysis")
async def project_analysis_get(project_id: int):
    data = await get_project_analysis(project_id)
    if not data:
        raise HTTPException(404, "No analysis yet")
    return data


@app.post("/api/projects/{project_id}/analyse")
async def project_analysis_run(project_id: int, req: AnalyseRequest, background_tasks: BackgroundTasks):
    """Manually trigger project-level story bible generation."""
    current = job_progress.get("__project_analysis__", {})
    if current.get("status") == "analyzing":
        return {"ok": True, "status": "already_running"}
    job_progress["__project_analysis__"] = {"status": "queued"}
    background_tasks.add_task(run_project_bible, project_id, req.ai_model)
    return {"ok": True, "status": "queued"}


@app.get("/api/projects/{project_id}/analysis/status")
async def project_analysis_status(project_id: int):
    return job_progress.get("__project_analysis__", {"status": "idle"})


@app.get("/api/projects/{project_id}/stats")
async def project_stats(project_id: int):
    return await get_project_stats(project_id)


@app.get("/api/projects/{project_id}/analysis/stale")
async def project_analysis_stale(project_id: int):
    """Is the Story Bible out-of-date? Returns new_clips count since last generation."""
    return await check_analysis_stale(project_id)


@app.get("/api/projects/{project_id}/analysis/checkpoint")
async def project_analysis_checkpoint(project_id: int):
    """How many batches are saved so the UI can show resume info."""
    cp = await get_checkpoint_status(project_id)
    return cp or {"last_batch": 0, "total_batches": 0}


# In-memory cache: project_id → {cut_info, xml_str}
_narrative_cuts: dict[int, dict] = {}


@app.post("/api/projects/{project_id}/narrative-cut")
async def generate_narrative_cut(project_id: int, req: NarrativeCutRequest):
    """
    AI selects clips + timestamps for a narrative cut on the given theme,
    then generates an FCPXML ready for DaVinci Resolve.
    """
    from pathlib import Path
    from analyzer import build_transcripts_csv

    all_files = await get_project_files(project_id)
    clips     = []
    file_map  = {}   # filename → {path, duration_seconds}

    for pf in all_files:
        db_segs = await get_transcript(pf["id"])
        if db_segs:
            segs = [{"start": s["start_time"], "end": s["end_time"], "text": s["text"]}
                    for s in db_segs]
            clips.append({"filename": pf["filename"], "segments": segs})
        # Always add to file_map for FCPXML path lookup
        file_map[pf["filename"]] = {
            "path":             pf["path"],
            "duration_seconds": pf.get("duration_seconds") or 0,
        }

    if not clips:
        raise HTTPException(400, "No transcripts found — process clips first")

    project      = await get_project(project_id)
    project_name = project["name"] if project else str(project_id)

    # ── Auto-save full transcript CSV to disk (fresh before every cut) ──
    # The AI uses this CSV table for accurate float timestamps.
    # The user can also open it in Excel/Numbers to inspect all segments.
    csv_dir  = Path("transcript_exports")
    csv_dir.mkdir(exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in project_name)
    csv_path  = csv_dir / f"{project_id}_{safe_name}_transcripts.csv"
    loop      = asyncio.get_event_loop()
    full_csv  = await loop.run_in_executor(None, lambda: build_transcripts_csv(clips))
    await loop.run_in_executor(None, lambda: csv_path.write_text(full_csv, encoding="utf-8"))
    log.info(f"Transcript CSV saved: {csv_path} ({len(full_csv):,} chars, {len(clips)} clips)")

    # Semantic search: LLM expands the theme into related keywords first,
    # then we union the matches from EACH keyword. This pulls clips from
    # MANY files (fixing the "only one clip used in XML" issue).
    from telegram_bot import _expand_query_with_llm
    expanded_keywords = await _expand_query_with_llm(req.theme)
    log.info(f"Cut: theme {req.theme!r} → semantic keywords: {expanded_keywords}")

    seen_files = {}
    for kw in expanded_keywords:
        matches = await get_theme_relevant_clips(project_id, kw, max_clips=12)
        for m in matches:
            if m["filename"] not in seen_files:
                seen_files[m["filename"]] = m
            else:
                # Boost score by adding to the match count
                seen_files[m["filename"]]["match_count"] = (
                    seen_files[m["filename"]].get("match_count", 0) + m.get("match_count", 0)
                )
    # Also include direct literal theme match
    direct = await get_theme_relevant_clips(project_id, req.theme, max_clips=15)
    for m in direct:
        if m["filename"] not in seen_files:
            seen_files[m["filename"]] = m

    relevant_clips = sorted(seen_files.values(), key=lambda r: -r.get("match_count", 0))[:40]
    log.info(f"Cut: semantic+literal search found {len(relevant_clips)} unique files for theme")

    # Load batch summaries for project context in the cut prompt
    batch_sums = await get_all_batch_summaries(project_id)

    try:
        cut_info = await loop.run_in_executor(
            None,
            lambda c=clips, t=req.theme, d=req.duration, m=req.ai_model, rc=relevant_clips, bs=batch_sums:
                select_narrative_clips(c, t, d, m, relevant_clips=rc, batch_summaries=bs),
        )
    except Exception as e:
        log.error(f"Narrative cut selection failed: {e}")
        raise HTTPException(500, f"AI selection failed: {e}")

    # Verify all selected filenames exist in file_map
    missing = [c["filename"] for c in cut_info.get("clips", []) if c["filename"] not in file_map]
    if missing:
        log.warning(f"AI selected unknown filenames: {missing}")
        cut_info["clips"] = [c for c in cut_info["clips"] if c["filename"] in file_map]

    if not cut_info.get("clips"):
        raise HTTPException(500, "AI returned no valid clips — try again")

    # Enforce diversity: if all picks are from one file, re-roll with stricter prompt
    unique_files = {c["filename"] for c in cut_info["clips"]}
    if len(unique_files) < 2 and len(file_map) > 1:
        log.warning(f"Cut produced from only {len(unique_files)} file(s) — retrying with diversity boost")
        try:
            boosted_theme = (
                f"{req.theme}\n\n"
                f"CRITICAL: The previous attempt used only one source file. "
                f"You MUST pick from at least 3 DIFFERENT source files this time."
            )
            cut_info = await loop.run_in_executor(
                None,
                lambda: select_narrative_clips(
                    clips, boosted_theme, req.duration, req.ai_model,
                    relevant_clips=relevant_clips, batch_summaries=batch_sums,
                ),
            )
            cut_info["clips"] = [c for c in cut_info.get("clips", []) if c["filename"] in file_map]
        except Exception as e:
            log.warning(f"Diversity retry failed (using original): {e}")

    # Generate FCPXML
    try:
        xml_str = generate_fcpxml(cut_info, file_map, project_name)
    except Exception as e:
        raise HTTPException(500, f"FCPXML generation failed: {e}")

    _narrative_cuts[project_id] = {"cut_info": cut_info, "xml_str": xml_str}

    # ── Auto-save FCPXML to disk (so user can drag from Finder) ──
    cut_dir = Path("narrative_cuts")
    cut_dir.mkdir(exist_ok=True)
    safe_title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in cut_info.get("title", "cut"))[:60]
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    xml_path   = cut_dir / f"{safe_title}_{timestamp}.fcpxml"
    xml_path.write_text(xml_str, encoding="utf-8")
    log.info(f"FCPXML saved: {xml_path}")

    total_dur = sum(
        c.get("out_time", 0) - c.get("in_time", 0)
        for c in cut_info.get("clips", [])
    )

    return {
        "title":          cut_info.get("title", "Narrative Cut"),
        "narrative_note": cut_info.get("narrative_note", ""),
        "total_duration": round(total_dur, 1),
        "clips":          cut_info.get("clips", []),
        "xml_ready":      True,
        "csv_path":       str(csv_path.resolve()),
        "xml_path":       str(xml_path.resolve()),
    }


@app.post("/api/narrative-cut/reveal")
async def reveal_cut_file(req: dict):
    """Reveal the FCPXML file in Finder so the user can drag it into Resolve."""
    path = req.get("path")
    if not path or not Path(path).exists():
        raise HTTPException(404, f"FCPXML not found at: {path}")
    try:
        subprocess.Popen(["open", "-R", path])
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/projects/{project_id}/narrative-cut/latest.fcpxml")
async def download_narrative_cut(project_id: int):
    """Download the most recently generated narrative cut as FCPXML."""
    from fastapi.responses import Response
    cached = _narrative_cuts.get(project_id)
    if not cached:
        raise HTTPException(404, "No narrative cut generated yet — run Generate Cut first")

    project      = await get_project(project_id)
    project_name = (project["name"] if project else str(project_id)).replace(" ", "_")
    title        = cached["cut_info"].get("title", "Narrative_Cut").replace(" ", "_").replace("/", "-")
    filename     = f"{project_name}_{title}.fcpxml"

    return Response(
        content=cached["xml_str"],
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/projects/{project_id}/export/transcripts.csv")
async def export_transcripts_csv(project_id: int):
    """Download all transcripts as a CSV file."""
    import csv
    import io
    from fastapi.responses import StreamingResponse

    files = await get_project_files(project_id)
    project = await get_project(project_id)
    project_name = (project["name"] if project else str(project_id)).replace(" ", "_")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["filename", "duration_s", "start_time", "end_time", "text"])

    for f in files:
        segs = await get_transcript(f["id"])
        for s in segs:
            writer.writerow([
                f["filename"],
                round(f["duration_seconds"] or 0, 2),
                round(s["start_time"], 3),
                round(s["end_time"],   3),
                s["text"],
            ])

    output.seek(0)
    filename = f"{project_name}_transcripts.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/projects/{project_id}/chat")
async def get_chat_history(project_id: int):
    """Load all persisted chat messages for this project."""
    return await get_chat_messages(project_id)


CUT_INTENT_RE = re.compile(
    r"\b(?:make|generate|build|create|edit|cut me|give me)\b.{0,40}\b(?:cut|edit|video|sequence|fcpxml|reel|montage)\b"
    r"|^cut\b|\bnarrative cut\b|\b\d+\s*(?:s|sec|second)s?\s+cut\b",
    re.IGNORECASE,
)

DURATION_RE = re.compile(r"(\d{1,3})\s*(?:s|sec|second)s?\b", re.IGNORECASE)


def _detect_cut_intent(message: str) -> dict | None:
    """If the message looks like a cut request, return {theme, duration}."""
    if not CUT_INTENT_RE.search(message):
        return None
    # Extract duration (default 60s)
    dur_match = DURATION_RE.search(message)
    duration = int(dur_match.group(1)) if dur_match else 60
    duration = max(15, min(600, duration))
    # The theme = the whole user message (the AI selector knows what to do)
    theme = message.strip()
    return {"theme": theme, "duration": duration}


@app.post("/api/projects/{project_id}/chat")
async def project_chat(project_id: int, req: ChatRequest):
    """
    Unified chat endpoint.
    - Detects cut requests ("make a 60s cut about X") and runs the FCPXML generator.
    - Otherwise hits the fast transcript-only path (same as Telegram bot).
    Returns reply + cited_clips + optional cut info.
    """
    if not req.messages:
        raise HTTPException(400, "No messages provided")

    last = req.messages[-1]
    if last.get("role") != "user":
        raise HTTPException(400, "Last message must be from user")

    user_msg = last["content"]
    ai_model = req.ai_model or "haiku"

    await save_chat_message(project_id, "user", user_msg)

    # XML/cut generation removed v36 — straight to transcript chat.
    if False and _detect_cut_intent(user_msg):
        cut_intent = None
        try:
            log.info(f"Cut intent detected — theme={cut_intent['theme'][:60]!r}, duration={cut_intent['duration']}s")
            cut_req = NarrativeCutRequest(
                theme=cut_intent["theme"],
                duration=cut_intent["duration"],
                ai_model=ai_model,
            )
            cut_res = await generate_narrative_cut(project_id, cut_req)
            # Build a chat-shaped reply summarising the cut
            clip_lines = []
            cited = []
            for i, c in enumerate(cut_res["clips"], 1):
                ts_in  = f"{int(c['in_time']//60)}:{int(c['in_time']%60):02d}"
                ts_out = f"{int(c['out_time']//60)}:{int(c['out_time']%60):02d}"
                dur    = round(c['out_time'] - c['in_time'], 1)
                role   = (c.get('narrative_role') or '').split('—')[0].strip()
                clip_lines.append(
                    f"<b>{i}.</b> <code>[{c['filename']} @ {ts_in}]</code> → <code>{ts_out}</code> "
                    f"<i>({dur}s · {role})</i>\n<blockquote>{c.get('quote','')[:120]}</blockquote>"
                )
                # Resolve filename → file_id for the citation button
                from database import get_project_files
                if not cited:
                    files = await get_project_files(project_id)
                    files_by_name = {f["filename"]: f for f in files}
                fobj = files_by_name.get(c["filename"])
                if fobj and not any(x["file_id"] == fobj["id"] for x in cited):
                    cited.append({
                        "file_id":    fobj["id"],
                        "filename":   c["filename"],
                        "start_time": c["in_time"],
                    })

            reply = (
                f"🎬 <b>{cut_res['title']}</b>\n"
                f"<i>{cut_res['narrative_note']}</i>\n\n"
                f"<b>{len(cut_res['clips'])} clips · {cut_res['total_duration']}s total</b>\n\n"
                + "\n\n".join(clip_lines)
            )
            await save_chat_message(project_id, "assistant", reply)
            return {
                "reply":       reply,
                "cited_clips": cited,
                "cut": {
                    "title":          cut_res["title"],
                    "duration":       cut_res["total_duration"],
                    "clip_count":     len(cut_res["clips"]),
                    "xml_path":       cut_res.get("xml_path"),
                    "xml_url":        f"/api/projects/{project_id}/narrative-cut/latest.fcpxml",
                },
            }
        except HTTPException:
            raise
        except Exception as e:
            log.exception(f"Cut-via-chat failed: {e}")
            # Fall through to normal chat

    # Use the unified chat helper — same code path as Telegram
    from telegram_bot import chat_transcripts_only
    try:
        reply, cited = await chat_transcripts_only(project_id, user_msg, model=ai_model)
        return {"reply": reply, "cited_clips": cited}
    except Exception as e:
        log.exception(f"Chat ERROR: {e}")
        raise HTTPException(500, str(e))


@app.delete("/api/projects/{project_id}/chat")
async def clear_chat(project_id: int):
    await clear_chat_messages(project_id)
    return {"ok": True}


@app.post("/api/projects/{project_id}/chat/{message_id}/pin")
async def pin_message(project_id: int, message_id: int, pinned: bool = True):
    await set_chat_message_pinned(message_id, pinned)
    return {"ok": True}


@app.get("/api/projects/{project_id}/chat/notes")
async def chat_notes(project_id: int):
    """Pinned chat messages shown in the Analysis tab."""
    return await get_pinned_chat_messages(project_id)


@app.get("/api/files/{file_id}/progress")
async def file_progress(file_id: int):
    return job_progress.get(file_id, {"status": "unknown"})


@app.get("/api/search")
async def search(q: str, project_id: int | None = None):
    if len(q) < 2:
        raise HTTPException(400, "Query too short")
    return await search_transcripts(q, project_id)


@app.get("/api/jobs")
async def all_jobs():
    return job_progress


@app.get("/api/files/{file_id}/scenes")
async def file_scenes(file_id: int):
    """Return detected scenes for a clip (empty list if not yet detected)."""
    return await get_scenes(file_id)


class DetectScenesRequest(BaseModel):
    force: bool = False
    use_ai: bool = False    # if true → also runs Claude vision per scene


@app.post("/api/files/{file_id}/detect-scenes")
async def detect_file_scenes(
    file_id: int,
    background_tasks: BackgroundTasks,
    body: DetectScenesRequest | None = None,
):
    """Manually trigger scene detection. Returns already_detected if scenes exist (unless force=true)."""
    f = await get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")

    force  = bool(body and body.force)
    use_ai = bool(body and body.use_ai)

    existing = await get_scenes(file_id)
    if existing and not force:
        return {
            "status": "already_detected",
            "scene_count": len(existing),
            "file_id": file_id,
        }

    async def _run():
        loop = asyncio.get_event_loop()
        try:
            tx_segs = await get_transcript(file_id)
            scenes = await loop.run_in_executor(
                None,
                lambda: detect_scenes(file_id, f["path"], transcript_segments=tx_segs, use_ai=use_ai),
            )
            if scenes:
                await save_scenes(file_id, scenes)
                await update_file_scene_summary(file_id, scenes)
                first_thumb = next((s["thumbnail_path"] for s in scenes if s.get("thumbnail_path")), None)
                if first_thumb:
                    await save_poster_path(file_id, first_thumb)
            job_progress[f"scenes_{file_id}"] = {"status": "done", "count": len(scenes), "ai": use_ai}
        except Exception as e:
            job_progress[f"scenes_{file_id}"] = {"status": "error", "error": str(e)}

    job_progress[f"scenes_{file_id}"] = {"status": "running", "ai": use_ai}
    background_tasks.add_task(_run)
    return {"status": "queued", "file_id": file_id, "use_ai": use_ai}


@app.post("/api/files/{file_id}/classify-with-ai")
async def classify_scenes_with_ai(file_id: int, background_tasks: BackgroundTasks):
    """Run Claude vision on EXISTING scenes — refines shot_size/angle/location/description."""
    f = await get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")

    scenes = await get_scenes(file_id)
    if not scenes:
        raise HTTPException(400, "No scenes to classify. Run scene detection first.")

    # Settings check
    s = _load_settings()
    if s.get("offline_mode"):
        raise HTTPException(400, "Cannot use AI in offline mode")

    tx_segs = await get_transcript(file_id)

    async def _run():
        loop = asyncio.get_event_loop()
        try:
            done = 0
            for sc in scenes:
                if not sc.get("thumbnail_path"):
                    continue
                thumb_abs = Path("static") / sc["thumbnail_path"]
                if not thumb_abs.exists():
                    continue
                has_dialogue = any(
                    (seg.get("end_time") or 0) >= sc["start_time"] and
                    (seg.get("start_time") or 0) <= sc["end_time"]
                    for seg in tx_segs
                )
                ai = await loop.run_in_executor(
                    None,
                    lambda p=str(thumb_abs), d=has_dialogue: classify_shot_with_claude(p, d),
                )
                if ai:
                    await update_scene_classification(sc["id"], ai)
                    done += 1
                job_progress[f"ai_classify_{file_id}"] = {
                    "status": "running", "done": done, "total": len(scenes),
                }
            # Refresh file-level summary
            refreshed = await get_scenes(file_id)
            await update_file_scene_summary(file_id, refreshed)
            job_progress[f"ai_classify_{file_id}"] = {
                "status": "done", "done": done, "total": len(scenes),
            }
        except Exception as e:
            job_progress[f"ai_classify_{file_id}"] = {"status": "error", "error": str(e)}

    job_progress[f"ai_classify_{file_id}"] = {"status": "running", "done": 0, "total": len(scenes)}
    background_tasks.add_task(_run)
    return {"status": "queued", "file_id": file_id, "scene_count": len(scenes)}


@app.post("/api/files/{file_id}/reveal")
async def reveal_in_finder(file_id: int):
    """Reveal the source file in macOS Finder."""
    f = await get_file(file_id)
    if not f:
        raise HTTPException(404, "File not found")
    p = Path(f["path"])
    if not p.exists():
        raise HTTPException(404, f"File missing on disk: {f['path']}")
    try:
        # `open -R` reveals the file in Finder, selecting it
        subprocess.Popen(["open", "-R", str(p)])
        return {"ok": True, "path": str(p)}
    except Exception as e:
        raise HTTPException(500, f"Could not open: {e}")


@app.get("/api/projects/{project_id}/posters")
async def project_posters(project_id: int):
    """Return poster thumbnail paths for up to 8 clips in a project."""
    paths = await get_project_poster_paths(project_id, limit=8)
    return {"posters": paths}


@app.get("/api/projects/{project_id}/scenes/search")
async def search_scenes(project_id: int, q: str = ""):
    """Search scenes by shot type or tags (e.g. ?q=closeup or ?q=desert)."""
    if not q.strip():
        return []
    return await search_project_scenes(project_id, q)


@app.get("/api/projects/{project_id}/scenes")
async def project_all_scenes(project_id: int, shot_type: str = ""):
    """Return all scenes for a project, optionally filtered by shot_type."""
    import aiosqlite, json
    from database import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if shot_type:
            sql = """
                SELECT s.*, f.filename, f.id as file_id
                FROM scenes s JOIN files f ON s.file_id = f.id
                WHERE f.project_id = ? AND LOWER(s.shot_type) = ?
                ORDER BY f.filename, s.scene_num LIMIT 500
            """
            cur = await db.execute(sql, (project_id, shot_type.lower()))
        else:
            sql = """
                SELECT s.*, f.filename, f.id as file_id
                FROM scenes s JOIN files f ON s.file_id = f.id
                WHERE f.project_id = ?
                ORDER BY f.filename, s.scene_num LIMIT 1000
            """
            cur = await db.execute(sql, (project_id,))
        rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        try:
            r["tags"] = json.loads(r.get("tags") or "[]")
        except Exception:
            r["tags"] = []
    return rows


# ── Settings (offline mode etc.) ─────────────────────────────────────────────

import json as _json
_SETTINGS_PATH = Path("settings.json") if True else None

def _load_settings() -> dict:
    try:
        return _json.loads(_SETTINGS_PATH.read_text())
    except Exception:
        return {"offline_mode": False}

def _save_settings(data: dict):
    _SETTINGS_PATH.write_text(_json.dumps(data, indent=2))

def _mask(value: str | None) -> str | None:
    """Return a masked version of a secret (e.g. 'sk-ant-…XYZ4')."""
    if not value:
        return None
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:7]}…{value[-4:]}"


@app.get("/api/settings")
async def get_settings():
    s = _load_settings()
    from analyzer import _claude_available
    bot_username = s.get("telegram_bot_username")
    if not bot_username and s.get("telegram_token"):
        try:
            import urllib.request, json as _j
            r = urllib.request.urlopen(
                f"https://api.telegram.org/bot{s['telegram_token']}/getMe", timeout=4
            ).read()
            data = _j.loads(r)
            bot_username = data.get("result", {}).get("username")
            if bot_username:
                s["telegram_bot_username"] = bot_username
                _save_settings(s)
        except Exception as e:
            log.debug(f"Could not fetch bot username: {e}")

    # Read masked keys from env (never echo actual secrets)
    env_keys = {
        "anthropic":  os.environ.get("ANTHROPIC_API_KEY"),
        "groq":       os.environ.get("GROQ_API_KEY"),
        "groq_2":     os.environ.get("GROQ_API_KEY_2"),
        "gemini":     os.environ.get("GEMINI_API_KEY"),
    }

    return {
        "offline_mode": s.get("offline_mode", False),
        "claude_available": _claude_available,
        "telegram_enabled": bool(s.get("telegram_token")),
        "telegram_chat_ids": s.get("telegram_chat_ids", []),
        "telegram_default_project_id": s.get("telegram_default_project_id"),
        "telegram_token_set": bool(s.get("telegram_token")),
        "telegram_bot_username": bot_username,
        "telegram_url": f"https://t.me/{bot_username}" if bot_username else None,
        "telegram_token_masked": _mask(s.get("telegram_token")),
        "telegram_model": s.get("telegram_model") or "haiku",
        "api_keys": {k: {"set": bool(v), "masked": _mask(v)} for k, v in env_keys.items()},
    }


@app.patch("/api/settings/api-keys")
async def patch_api_keys(req: dict):
    """
    Update API keys in .env file. Pass {"anthropic": "sk-ant-...", "groq": "...", ...}.
    An empty string clears the key. Omitted keys are left unchanged.
    """
    from pathlib import Path
    env_path = Path(".env")

    # Load existing .env
    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                existing[k.strip()] = v

    # Map UI names → env var names
    KEY_MAP = {
        "anthropic": "ANTHROPIC_API_KEY",
        "groq":      "GROQ_API_KEY",
        "groq_2":    "GROQ_API_KEY_2",
        "gemini":    "GEMINI_API_KEY",
    }

    updated = []
    for ui_key, env_var in KEY_MAP.items():
        if ui_key in req:
            val = (req[ui_key] or "").strip()
            if val:
                existing[env_var] = val
                os.environ[env_var] = val
                updated.append(env_var)
            else:
                existing.pop(env_var, None)
                os.environ.pop(env_var, None)
                updated.append(f"{env_var} (cleared)")

    # Write back
    env_path.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n")

    # Refresh any module-level clients so the changes take effect immediately
    try:
        import importlib, analyzer
        importlib.reload(analyzer)
        log.info(f"API keys updated: {updated}; analyzer reloaded")
    except Exception as e:
        log.warning(f"Couldn't reload analyzer after key update: {e}")

    return {"updated": updated, "ok": True}

class SettingsPatch(BaseModel):
    offline_mode: bool | None = None
    telegram_token: str | None = None
    telegram_chat_ids: list[int] | None = None
    telegram_default_project_id: int | None = None

@app.patch("/api/settings")
async def patch_settings(body: SettingsPatch):
    s = _load_settings()
    old_token = s.get("telegram_token")
    if body.offline_mode is not None:
        s["offline_mode"] = body.offline_mode
    if body.telegram_token is not None:
        s["telegram_token"] = body.telegram_token.strip() or None
        if not s["telegram_token"]:
            s.pop("telegram_token", None)
    if body.telegram_chat_ids is not None:
        s["telegram_chat_ids"] = body.telegram_chat_ids
    if body.telegram_default_project_id is not None:
        s["telegram_default_project_id"] = body.telegram_default_project_id
    _save_settings(s)

    # ── Restart the Telegram bot if the token changed ─────────────────────
    # The bot is created at startup with a fixed token. If the user adds or
    # changes the token via Settings UI we need to tear down the old bot and
    # start a new one so they don't have to restart the whole app.
    new_token = s.get("telegram_token")
    if new_token != old_token:
        global _telegram_task
        if _telegram_task and not _telegram_task.done():
            _telegram_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(_telegram_task), timeout=3)
            except Exception:
                pass
        _telegram_task = None
        if new_token:
            try:
                from telegram_bot import run_telegram_bot
                _telegram_task = asyncio.create_task(
                    run_telegram_bot(new_token, _load_settings),
                    name="telegram-bot",
                )
                log.info("Telegram bot restarted with new token")
            except Exception as e:
                log.warning(f"Telegram bot failed to restart: {e}")

    # Don't return the token
    out = dict(s)
    out.pop("telegram_token", None)
    out["telegram_token_set"] = bool(s.get("telegram_token"))
    return out


# ── Project Export / Import (.nolanproj bundle) ──────────────────────────

@app.get("/api/projects/{project_id}/export")
async def export_project(project_id: int):
    """
    Bundle a project + all metadata (transcripts, scenes, thumbnails, chat,
    analysis) into a .nolanproj zip file. Recipients can import this and have
    every clip pre-processed — they only need access to the source MP4s.
    """
    import json as _j, zipfile, tempfile
    from database import (
        get_project, get_project_files, get_project_folders, get_transcript,
        get_scenes, get_chat_messages, get_project_analysis,
    )

    project = await get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    folders = await get_project_folders(project_id)
    files   = await get_project_files(project_id)

    bundle = {
        "schema_version":  1,
        "exported_at":     datetime.now(timezone.utc).isoformat(),
        "project":         dict(project),
        "folders":         [dict(f) for f in folders],
        "files":           [],
        "segments":        {},
        "scenes":          {},
        "chat_messages":   [],
        "analysis":        None,
    }

    file_thumb_dirs: list[tuple[int, Path]] = []
    for f in files:
        bundle["files"].append(dict(f))

        segs = await get_transcript(f["id"])
        if segs:
            bundle["segments"][str(f["id"])] = [dict(s) for s in segs]

        scenes = await get_scenes(f["id"])
        if scenes:
            bundle["scenes"][str(f["id"])] = [dict(s) for s in scenes]

        tdir = Path("static/thumbnails") / str(f["id"])
        if tdir.exists():
            file_thumb_dirs.append((f["id"], tdir))

    bundle["chat_messages"] = [dict(m) for m in (await get_chat_messages(project_id))]
    bundle["analysis"]      = await get_project_analysis(project_id)

    # Make values JSON-serialisable (datetimes etc.)
    def _safe(o):
        if isinstance(o, dict):  return {k: _safe(v) for k, v in o.items()}
        if isinstance(o, list):  return [_safe(v) for v in o]
        if hasattr(o, "isoformat"): return o.isoformat()
        return o

    payload = _j.dumps(_safe(bundle), indent=2, default=str)

    # Stream the zip
    tmp = tempfile.NamedTemporaryFile(suffix=".nolanproj", delete=False)
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("manifest.json", _j.dumps({
            "version": 1, "project_name": project["name"],
            "file_count": len(files), "thumb_count": sum(1 for _ in file_thumb_dirs),
        }))
        zf.writestr("data.json", payload)
        for fid, tdir in file_thumb_dirs:
            for thumb in tdir.iterdir():
                if thumb.is_file():
                    zf.write(thumb, f"thumbnails/{fid}/{thumb.name}")

    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in project["name"])
    log.info(f"Project {project_id} exported · {len(files)} files · {len(file_thumb_dirs)} thumb dirs")
    return FileResponse(
        tmp.name,
        filename=f"{safe_name}.nolanproj",
        media_type="application/zip",
    )


@app.post("/api/projects/import")
async def import_project(file: UploadFile = File(...), footage_root: str = Form("")):
    """
    Import a .nolanproj bundle. If `footage_root` is given, all file paths
    are relinked by walking that folder and matching basenames — so friends
    with the same MP4s in a different location can still use the data.
    """
    import json as _j, zipfile, tempfile, shutil
    from database import (
        create_project, add_project_folder, upsert_file, update_file_status,
        save_segments, save_scenes, save_chat_message, save_project_analysis,
        save_poster_path, update_file_scene_summary,
    )
    import aiosqlite
    from database import DB_PATH

    # Save the upload to a temp file
    tmp = tempfile.NamedTemporaryFile(suffix=".nolanproj", delete=False)
    tmp.write(await file.read())
    tmp.close()

    # Parse
    try:
        with zipfile.ZipFile(tmp.name, "r") as zf:
            manifest = _j.loads(zf.read("manifest.json"))
            data     = _j.loads(zf.read("data.json"))
            file_names_in_zip = zf.namelist()
    except Exception as e:
        raise HTTPException(400, f"Invalid bundle: {e}")

    # Build basename → on-disk path index from footage_root
    relink_index: dict[str, str] = {}
    if footage_root and Path(footage_root).is_dir():
        for root, _, fnames in os.walk(footage_root):
            for fn in fnames:
                if fn.startswith("."):
                    continue
                relink_index.setdefault(fn, str(Path(root) / fn))
        log.info(f"Import: indexed {len(relink_index)} files under {footage_root}")

    # Create the new project
    new_proj_id = await create_project(data["project"]["name"] + " (imported)")
    log.info(f"Import: created new project id={new_proj_id}")

    # Folders
    for fol in data.get("folders", []):
        try:
            await add_project_folder(new_proj_id, fol["path"])
        except Exception:
            pass

    # Files → new IDs
    old_to_new_id: dict[int, int] = {}
    for f in data.get("files", []):
        old_id = f["id"]
        # Relink path
        basename = Path(f["path"]).name
        new_path = relink_index.get(basename) or f["path"]

        result = await upsert_file(new_proj_id, new_path, basename, f.get("size_bytes") or 0)
        new_id = result[0] if isinstance(result, tuple) else result
        old_to_new_id[old_id] = new_id

        # Restore status + metadata
        kwargs = {}
        for k in ("duration_seconds", "transcribed_at", "analyzed_at",
                  "primary_shot_type", "primary_shot_size", "primary_roll_type",
                  "primary_setting", "primary_location", "scene_tags"):
            if f.get(k) is not None:
                kwargs[k] = f[k]
        await update_file_status(new_id, f.get("status", "done"), **kwargs)

    # Transcripts
    for old_fid_str, segs in data.get("segments", {}).items():
        new_fid = old_to_new_id.get(int(old_fid_str))
        if not new_fid:
            continue
        seg_data = [{"start": s["start_time"], "end": s["end_time"], "text": s["text"]}
                    for s in segs]
        if seg_data:
            await save_segments(new_fid, seg_data)

    # Scenes — IDs change so we'll regenerate. Also extract thumbnails.
    with zipfile.ZipFile(tmp.name, "r") as zf:
        for old_fid_str, scenes in data.get("scenes", {}).items():
            new_fid = old_to_new_id.get(int(old_fid_str))
            if not new_fid:
                continue
            # Remap thumbnail paths from old_fid → new_fid
            new_scenes = []
            old_thumb_dir = f"thumbnails/{old_fid_str}/"
            new_thumb_dir_disk = Path("static/thumbnails") / str(new_fid)
            new_thumb_dir_disk.mkdir(parents=True, exist_ok=True)
            # Copy thumbnail files
            for name in file_names_in_zip:
                if name.startswith(old_thumb_dir):
                    basename = name.split("/")[-1]
                    target = new_thumb_dir_disk / basename
                    with zf.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
            # Remap each scene
            for sc in scenes:
                old_thumb_rel = sc.get("thumbnail_path") or ""
                new_thumb_rel = old_thumb_rel.replace(
                    f"thumbnails/{old_fid_str}/",
                    f"thumbnails/{new_fid}/"
                ) if old_thumb_rel else None
                new_scenes.append({
                    "scene_num":     sc["scene_num"],
                    "start_time":    sc["start_time"],
                    "end_time":      sc["end_time"],
                    "thumbnail_path": new_thumb_rel,
                    "shot_type":     sc.get("shot_type"),
                    "shot_size":     sc.get("shot_size"),
                    "shot_angle":    sc.get("shot_angle"),
                    "roll_type":     sc.get("roll_type"),
                    "setting":       sc.get("setting"),
                    "location":      sc.get("location"),
                    "description":   sc.get("description"),
                    "tags":          sc.get("tags") or [],
                    "ai_classified": sc.get("ai_classified", 0),
                })
            if new_scenes:
                await save_scenes(new_fid, new_scenes)
                await update_file_scene_summary(new_fid, new_scenes)
                first_thumb = next((s["thumbnail_path"] for s in new_scenes if s.get("thumbnail_path")), None)
                if first_thumb:
                    await save_poster_path(new_fid, first_thumb)

    # Chat history
    for m in data.get("chat_messages", []):
        try:
            await save_chat_message(new_proj_id, m["role"], m["content"])
        except Exception:
            pass

    # Analysis
    if data.get("analysis"):
        try:
            await save_project_analysis(new_proj_id, data["analysis"])
        except Exception:
            pass

    relinked = sum(1 for f in data.get("files", [])
                   if relink_index.get(Path(f["path"]).name))
    log.info(f"Import: project {new_proj_id} ready · {len(old_to_new_id)} files · "
             f"{relinked} relinked to local disk")

    return {
        "project_id":    new_proj_id,
        "project_name":  data["project"]["name"] + " (imported)",
        "file_count":    len(old_to_new_id),
        "relinked":      relinked,
        "missing":       len(old_to_new_id) - relinked,
    }


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import threading
    import webbrowser
    import uvicorn

    def _open_browser():
        import time
        time.sleep(1.2)
        webbrowser.open("http://localhost:8765")

    threading.Thread(target=_open_browser, daemon=True).start()
    # NOLAN_DEV=1 enables hot reload for development; off by default so
    # background tasks (Telegram bot, scene detection) survive.
    dev = bool(os.environ.get("NOLAN_DEV"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8765,
        reload=dev,
    )
