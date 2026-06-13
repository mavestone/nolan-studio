import aiosqlite
import json
from contextlib import asynccontextmanager

DB_PATH = "footage.db"


@asynccontextmanager
async def _db():
    """
    Open a database connection with WAL mode + a 10-second busy timeout.
    WAL lets readers and one writer coexist without immediately locking.
    busy_timeout makes SQLite retry for up to 10 s before raising 'locked'.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=10000")  # 10 s
        await db.execute("PRAGMA foreign_keys=ON")      # make ON DELETE CASCADE actually fire
        yield db


async def init_db():
    async with _db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS project_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                last_scanned TIMESTAMP,
                files_found INTEGER DEFAULT 0,
                UNIQUE(project_id, path),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                path TEXT NOT NULL,
                filename TEXT NOT NULL,
                duration_seconds REAL,
                size_bytes INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                transcribed_at TIMESTAMP,
                analyzed_at TIMESTAMP,
                UNIQUE(project_id, path),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                text TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER UNIQUE NOT NULL,
                story_value INTEGER,
                summary TEXT,
                highlights TEXT,
                themes TEXT,
                characters TEXT,
                raw_json TEXT,
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS project_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER UNIQUE NOT NULL,
                raw_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bible_batch_checkpoints (
                project_id    INTEGER NOT NULL,
                batch_num     INTEGER NOT NULL,
                total_batches INTEGER NOT NULL,
                summary       TEXT    NOT NULL,
                PRIMARY KEY (project_id, batch_num),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id  INTEGER NOT NULL,
                role        TEXT    NOT NULL,
                content     TEXT    NOT NULL,
                pinned      INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_chat_project ON chat_messages(project_id)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scenes (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id        INTEGER NOT NULL,
                scene_num      INTEGER NOT NULL,
                start_time     REAL    NOT NULL,
                end_time       REAL    NOT NULL,
                thumbnail_path TEXT,
                shot_type      TEXT,
                tags           TEXT,
                UNIQUE(file_id, scene_num),
                FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_scenes_file ON scenes(file_id)")
        # Migrate: add shot_type and tags to scenes if missing
        async with db.execute("PRAGMA table_info(scenes)") as _cur:
            _scols = {row[1] for row in await _cur.fetchall()}
        if "shot_type" not in _scols:
            await db.execute("ALTER TABLE scenes ADD COLUMN shot_type TEXT")
        if "tags" not in _scols:
            await db.execute("ALTER TABLE scenes ADD COLUMN tags TEXT")
        # New: granular pro-grade attributes
        if "shot_size" not in _scols:
            await db.execute("ALTER TABLE scenes ADD COLUMN shot_size TEXT")     # extreme_close_up | close_up | medium | full | wide | extreme_wide
        if "shot_angle" not in _scols:
            await db.execute("ALTER TABLE scenes ADD COLUMN shot_angle TEXT")    # eye_level | low | high | dutch | over_shoulder | pov
        if "roll_type" not in _scols:
            await db.execute("ALTER TABLE scenes ADD COLUMN roll_type TEXT")     # a_roll | b_roll
        if "setting" not in _scols:
            await db.execute("ALTER TABLE scenes ADD COLUMN setting TEXT")       # indoor | outdoor
        if "location" not in _scols:
            await db.execute("ALTER TABLE scenes ADD COLUMN location TEXT")      # free-text: "desert", "kitchen", "car interior"
        if "description" not in _scols:
            await db.execute("ALTER TABLE scenes ADD COLUMN description TEXT")   # one-sentence AI description
        if "ai_classified" not in _scols:
            await db.execute("ALTER TABLE scenes ADD COLUMN ai_classified INTEGER DEFAULT 0")  # 1 if Claude vision was used
        if "visual_content" not in _scols:
            await db.execute("ALTER TABLE scenes ADD COLUMN visual_content TEXT")  # comma-sep objects: "shoe, sand, hand, water, grass"
        # Migrate: add poster_path, primary_shot_type, scene_tags to files table
        async with db.execute("PRAGMA table_info(files)") as _cur:
            _cols = {row[1] for row in await _cur.fetchall()}
        if "poster_path" not in _cols:
            await db.execute("ALTER TABLE files ADD COLUMN poster_path TEXT")
        if "primary_shot_type" not in _cols:
            await db.execute("ALTER TABLE files ADD COLUMN primary_shot_type TEXT")
        if "scene_tags" not in _cols:
            await db.execute("ALTER TABLE files ADD COLUMN scene_tags TEXT")
        if "primary_shot_size" not in _cols:
            await db.execute("ALTER TABLE files ADD COLUMN primary_shot_size TEXT")
        if "primary_roll_type" not in _cols:
            await db.execute("ALTER TABLE files ADD COLUMN primary_roll_type TEXT")
        if "primary_setting" not in _cols:
            await db.execute("ALTER TABLE files ADD COLUMN primary_setting TEXT")
        if "primary_location" not in _cols:
            await db.execute("ALTER TABLE files ADD COLUMN primary_location TEXT")
        if "frame_rate" not in _cols:
            await db.execute("ALTER TABLE files ADD COLUMN frame_rate REAL")   # e.g. 29.97, 59.94, 119.88
        # Migrate: add project_id column to files if it doesn't exist yet
        async with db.execute("PRAGMA table_info(files)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        if "project_id" not in columns:
            await db.execute("ALTER TABLE files ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE")
            await db.commit()

        await db.execute("CREATE INDEX IF NOT EXISTS idx_segments_file ON segments(file_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_files_project ON files(project_id)")
        await db.commit()

        # Migrate legacy files (no project_id) — attach them to a "Default" project
        async with db.execute("SELECT COUNT(*) FROM files WHERE project_id IS NULL") as cur:
            (count,) = await cur.fetchone()
        if count > 0:
            await db.execute("INSERT OR IGNORE INTO projects (name) VALUES ('Default')")
            await db.commit()
            async with db.execute("SELECT id FROM projects WHERE name = 'Default'") as cur:
                row = await cur.fetchone()
                default_id = row[0]
            await db.execute("UPDATE files SET project_id = ? WHERE project_id IS NULL", (default_id,))
            await db.commit()


# ── Projects ──

async def create_project(name: str) -> int:
    async with _db() as db:
        await db.execute("INSERT INTO projects (name) VALUES (?)", (name,))
        await db.commit()
        async with db.execute("SELECT id FROM projects WHERE name = ?", (name,)) as cur:
            row = await cur.fetchone()
            return row[0]


async def get_projects() -> list:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT p.*, COUNT(f.id) as file_count,
                   SUM(CASE WHEN f.status = 'done' THEN 1 ELSE 0 END) as done_count
            FROM projects p
            LEFT JOIN files f ON f.project_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at DESC
        """) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_project(project_id: int) -> dict | None:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def delete_project(project_id: int):
    async with _db() as db:
        await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await db.commit()


# ── Project folders ──

async def add_project_folder(project_id: int, path: str) -> int:
    async with _db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO project_folders (project_id, path) VALUES (?, ?)",
            (project_id, path)
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM project_folders WHERE project_id = ? AND path = ?",
            (project_id, path)
        ) as cur:
            row = await cur.fetchone()
            return row[0]


async def get_project_folders(project_id: int) -> list:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM project_folders WHERE project_id = ? ORDER BY path",
            (project_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def update_folder_scanned(folder_id: int, files_found: int):
    async with _db() as db:
        await db.execute(
            "UPDATE project_folders SET last_scanned = CURRENT_TIMESTAMP, files_found = ? WHERE id = ?",
            (files_found, folder_id)
        )
        await db.commit()


async def remove_project_folder(folder_id: int):
    async with _db() as db:
        await db.execute("DELETE FROM project_folders WHERE id = ?", (folder_id,))
        await db.commit()


# ── Files ──

async def upsert_file(project_id: int, path: str, filename: str, size_bytes: int) -> tuple[int, bool]:
    """Returns (file_id, is_new)."""
    async with _db() as db:
        async with db.execute(
            "SELECT id FROM files WHERE project_id = ? AND path = ?", (project_id, path)
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            return existing[0], False
        # INSERT OR IGNORE handles any unique constraint (old single-col or new composite)
        await db.execute(
            "INSERT OR IGNORE INTO files (project_id, path, filename, size_bytes) VALUES (?, ?, ?, ?)",
            (project_id, path, filename, size_bytes)
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM files WHERE project_id = ? AND path = ?", (project_id, path)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row[0], True
        # Fallback: path exists under a different project (stale row from old schema)
        await db.execute(
            "UPDATE files SET project_id = ? WHERE path = ?", (project_id, path)
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM files WHERE project_id = ? AND path = ?", (project_id, path)
        ) as cur:
            row = await cur.fetchone()
            return row[0], False


async def update_file_status(file_id: int, status: str, **kwargs):
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    async with _db() as db:
        if fields:
            await db.execute(
                f"UPDATE files SET status = ?, {fields} WHERE id = ?",
                [status] + list(kwargs.values()) + [file_id]
            )
        else:
            await db.execute("UPDATE files SET status = ? WHERE id = ?", (status, file_id))
        await db.commit()


async def save_segments(file_id: int, segments: list):
    async with _db() as db:
        await db.execute("DELETE FROM segments WHERE file_id = ?", (file_id,))
        await db.executemany(
            "INSERT INTO segments (file_id, start_time, end_time, text) VALUES (?, ?, ?, ?)",
            [(file_id, s["start"], s["end"], s["text"]) for s in segments]
        )
        await db.commit()


async def save_analysis(file_id: int, data: dict):
    async with _db() as db:
        await db.execute("""
            INSERT OR REPLACE INTO analysis
              (file_id, story_value, summary, highlights, themes, characters, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            file_id,
            data.get("story_value"),
            data.get("summary"),
            json.dumps(data.get("highlights", [])),
            json.dumps(data.get("themes", [])),
            json.dumps(data.get("characters", [])),
            json.dumps(data),
        ))
        await db.commit()


async def get_project_files(project_id: int) -> list:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT f.*, a.story_value, a.summary,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM scenes s
                       WHERE s.file_id = f.id AND s.thumbnail_path IS NOT NULL
                   ) THEN 1 ELSE 0 END AS has_thumbnail,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM scenes s
                       WHERE s.file_id = f.id
                         AND s.visual_content IS NOT NULL AND s.visual_content != ''
                   ) THEN 1 ELSE 0 END AS has_ai_detect
            FROM files f
            LEFT JOIN analysis a ON a.file_id = f.id
            WHERE f.project_id = ?
            ORDER BY a.story_value DESC NULLS LAST, f.created_at DESC
        """, (project_id,)) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_file(file_id: int) -> dict | None:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM files WHERE id = ?", (file_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_transcript(file_id: int) -> list:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM segments WHERE file_id = ? ORDER BY start_time", (file_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_analysis(file_id: int) -> dict | None:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM analysis WHERE file_id = ?", (file_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
            for field in ("highlights", "themes", "characters"):
                if d.get(field):
                    d[field] = json.loads(d[field])
            # Expose fields stored only in raw_json (e.g. story_notes)
            if d.get("raw_json"):
                for k, v in json.loads(d["raw_json"]).items():
                    if k not in d or d[k] is None:
                        d[k] = v
            return d


async def save_batch_checkpoint(project_id: int, batch_num: int, total_batches: int, summary: str):
    async with _db() as db:
        await db.execute("""
            INSERT OR REPLACE INTO bible_batch_checkpoints
              (project_id, batch_num, total_batches, summary)
            VALUES (?, ?, ?, ?)
        """, (project_id, batch_num, total_batches, summary))
        await db.commit()


async def get_batch_checkpoints(project_id: int, total_batches: int) -> dict:
    """Returns {batch_num: summary} for all saved batches matching total_batches."""
    async with _db() as db:
        async with db.execute("""
            SELECT batch_num, summary FROM bible_batch_checkpoints
            WHERE project_id = ? AND total_batches = ?
            ORDER BY batch_num
        """, (project_id, total_batches)) as cur:
            return {row[0]: row[1] for row in await cur.fetchall()}


async def clear_batch_checkpoints(project_id: int):
    async with _db() as db:
        await db.execute(
            "DELETE FROM bible_batch_checkpoints WHERE project_id = ?", (project_id,)
        )
        await db.commit()


async def get_checkpoint_status(project_id: int) -> dict | None:
    """Returns info about any in-progress bible generation for this project."""
    async with _db() as db:
        async with db.execute("""
            SELECT batch_num, total_batches FROM bible_batch_checkpoints
            WHERE project_id = ?
            ORDER BY batch_num DESC LIMIT 1
        """, (project_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {"last_batch": row[0], "total_batches": row[1]}


async def save_project_analysis(project_id: int, data: dict):
    async with _db() as db:
        await db.execute("""
            INSERT OR REPLACE INTO project_analysis (project_id, raw_json)
            VALUES (?, ?)
        """, (project_id, json.dumps(data)))
        await db.commit()


async def get_project_analysis(project_id: int) -> dict | None:
    async with _db() as db:
        async with db.execute(
            "SELECT raw_json FROM project_analysis WHERE project_id = ?", (project_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return json.loads(row[0])


async def get_project_stats(project_id: int) -> dict:
    """Returns clip counts: total, done, with_transcript, silent, errors."""
    async with _db() as db:
        async with db.execute("""
            SELECT
                COUNT(*)                                                  AS total,
                SUM(CASE WHEN status = 'done'  THEN 1 ELSE 0 END)        AS done,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END)        AS errors,
                (SELECT COUNT(DISTINCT s.file_id)
                 FROM segments s
                 JOIN files f2 ON f2.id = s.file_id
                 WHERE f2.project_id = ? AND f2.status = 'done')         AS with_transcript
            FROM files
            WHERE project_id = ?
        """, (project_id, project_id)) as cur:
            row = await cur.fetchone()
            total, done, errors, with_tx = (row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0)
            return {
                "total": total,
                "done": done,
                "errors": errors,
                "with_transcript": with_tx,
                "silent": max(0, done - with_tx),
            }


# ── Chat messages ──

async def save_chat_message(project_id: int, role: str, content: str) -> int:
    async with _db() as db:
        cur = await db.execute(
            "INSERT INTO chat_messages (project_id, role, content) VALUES (?, ?, ?)",
            (project_id, role, content),
        )
        await db.commit()
        return cur.lastrowid


async def get_chat_messages(project_id: int) -> list[dict]:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM chat_messages WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_pinned_chat_messages(project_id: int) -> list[dict]:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM chat_messages WHERE project_id = ? AND pinned = 1 ORDER BY created_at",
            (project_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def set_chat_message_pinned(message_id: int, pinned: bool):
    async with _db() as db:
        await db.execute(
            "UPDATE chat_messages SET pinned = ? WHERE id = ?",
            (1 if pinned else 0, message_id),
        )
        await db.commit()


async def clear_chat_messages(project_id: int):
    async with _db() as db:
        await db.execute("DELETE FROM chat_messages WHERE project_id = ?", (project_id,))
        await db.commit()


async def check_analysis_stale(project_id: int) -> dict:
    """
    Returns whether the Story Bible is out-of-date.
    Compares _meta.total_clips (set when the bible was generated) against the
    current count of 'done' clips that have at least one transcript segment.
    """
    analysis = await get_project_analysis(project_id)
    if not analysis:
        return {"has_bible": False, "stale": False, "new_clips": 0, "bible_clip_count": 0}

    meta = analysis.get("_meta", {})
    bible_clip_count = meta.get("total_clips")

    if bible_clip_count is None:
        # Old bible without _meta — can't reliably detect staleness
        return {"has_bible": True, "stale": False, "new_clips": 0, "bible_clip_count": 0}

    # Count clips currently in the project that have transcripts
    async with _db() as db:
        async with db.execute("""
            SELECT COUNT(DISTINCT s.file_id)
            FROM segments s
            JOIN files f ON f.id = s.file_id
            WHERE f.project_id = ? AND f.status = 'done'
        """, (project_id,)) as cur:
            (current_with_tx,) = await cur.fetchone()

    new_clips = max(0, current_with_tx - bible_clip_count)
    return {
        "has_bible":       True,
        "stale":           new_clips > 0,
        "new_clips":       new_clips,
        "current_with_tx": current_with_tx,
        "bible_clip_count": bible_clip_count,
    }


async def get_all_batch_summaries(project_id: int) -> list[str]:
    """Return all saved batch summaries for this project, ordered by batch_num."""
    async with _db() as db:
        async with db.execute(
            "SELECT summary FROM bible_batch_checkpoints WHERE project_id = ? ORDER BY batch_num",
            (project_id,),
        ) as cur:
            return [row[0] for row in await cur.fetchall()]


async def get_theme_relevant_clips(project_id: int, query: str, max_clips: int = 25) -> list[dict]:
    """
    Keyword-search transcript segments for the query/theme words.
    Returns [{filename, segments: [{start, end, text}], match_count}] sorted by relevance.
    Full transcripts (all segments) are loaded for each matching clip so the AI
    has timestamps for the entire clip, not just the matched lines.
    """
    import re as _re

    STOP = {
        'this','that','with','from','they','have','what','when','then','been',
        'your','will','just','also','some','more','into','than','about','would',
        'there','their','which','could','should','find','look','tell','give',
        'need','want','make','like','does','very','really','most','best','each',
        'both','only','even','such','much','many','over','after','before','first',
        'other','people','where','while','still','maybe','actually','something',
        'clips','clip','show','scene','footage','video','camera','time','think',
        'know','said','says','talking','says','like','just','yeah','okay',
    }
    words = [w.lower() for w in _re.split(r'[^\w]+', query)
             if len(w) >= 4 and w.lower() not in STOP][:10]

    if not words:
        return []

    async with _db() as db:
        db.row_factory = aiosqlite.Row
        # Count matching segments per file (relevance score)
        conditions = " OR ".join("LOWER(s.text) LIKE ?" for _ in words)
        params = [f"%{w}%" for w in words] + [project_id, max_clips]
        async with db.execute(f"""
            SELECT f.id AS file_id, f.filename, COUNT(*) AS match_count
            FROM segments s
            JOIN files f ON f.id = s.file_id
            WHERE ({conditions}) AND f.project_id = ? AND f.status = 'done'
            GROUP BY f.id, f.filename
            ORDER BY match_count DESC
            LIMIT ?
        """, params) as cur:
            file_rows = [dict(r) for r in await cur.fetchall()]

        if not file_rows:
            return []

        # Load FULL transcripts for the matched files (need timestamps for cut selection)
        result = []
        for fr in file_rows:
            async with db.execute(
                "SELECT start_time, end_time, text FROM segments WHERE file_id = ? ORDER BY start_time",
                (fr['file_id'],)
            ) as cur:
                segs = [{'start': r['start_time'], 'end': r['end_time'], 'text': r['text']}
                        for r in await cur.fetchall()]
            if segs:
                result.append({
                    'file_id':     fr['file_id'],
                    'filename':    fr['filename'],
                    'segments':    segs,
                    'match_count': fr['match_count'],
                })

    return result


async def search_transcripts(query: str, project_id: int | None = None) -> list:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        if project_id:
            sql = """
                SELECT s.*, f.filename, f.id as file_id
                FROM segments s
                JOIN files f ON f.id = s.file_id
                WHERE s.text LIKE ? AND f.project_id = ?
                ORDER BY f.id, s.start_time LIMIT 200
            """
            params = (f"%{query}%", project_id)
        else:
            sql = """
                SELECT s.*, f.filename, f.id as file_id
                FROM segments s
                JOIN files f ON f.id = s.file_id
                WHERE s.text LIKE ?
                ORDER BY f.id, s.start_time LIMIT 200
            """
            params = (f"%{query}%",)
        async with db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── Scenes ──

async def save_scenes(file_id: int, scenes: list[dict]) -> None:
    """Upsert detected scenes for a file."""
    import json
    async with _db() as db:
        await db.execute("DELETE FROM scenes WHERE file_id = ?", (file_id,))
        for s in scenes:
            tags_json = json.dumps(s.get("tags") or [])
            await db.execute(
                """INSERT INTO scenes (
                    file_id, scene_num, start_time, end_time, thumbnail_path,
                    shot_type, tags,
                    shot_size, shot_angle, roll_type, setting, location, description, ai_classified,
                    visual_content
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    file_id, s["scene_num"], s["start_time"], s["end_time"],
                    s.get("thumbnail_path"), s.get("shot_type"), tags_json,
                    s.get("shot_size"), s.get("shot_angle"), s.get("roll_type"),
                    s.get("setting"), s.get("location"), s.get("description"),
                    1 if s.get("ai_classified") else 0,
                    s.get("visual_content"),
                ),
            )
        await db.commit()


async def update_scene_classification(scene_id: int, data: dict) -> None:
    """Update a single scene's classification (used by AI vision pass)."""
    import json
    fields = []
    values = []
    for k in ("shot_size", "shot_angle", "roll_type", "setting", "location", "description", "visual_content"):
        if k in data and data[k] is not None:
            fields.append(f"{k} = ?")
            values.append(data[k])
    if "tags" in data and data["tags"] is not None:
        fields.append("tags = ?")
        values.append(json.dumps(data["tags"]))
    if "shot_type" in data and data["shot_type"] is not None:
        fields.append("shot_type = ?")
        values.append(data["shot_type"])
    if not fields:
        return
    fields.append("ai_classified = 1")
    values.append(scene_id)
    async with _db() as db:
        await db.execute(f"UPDATE scenes SET {', '.join(fields)} WHERE id = ?", values)
        await db.commit()


async def get_scenes(file_id: int) -> list[dict]:
    """Return all detected scenes for a file, ordered by scene_num."""
    import json
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM scenes WHERE file_id = ? ORDER BY scene_num",
            (file_id,)
        ) as cur:
            rows = await cur.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["tags"] = json.loads(d.get("tags") or "[]")
        except Exception:
            d["tags"] = []
        result.append(d)
    return result


async def search_project_scenes(project_id: int, q: str) -> list[dict]:
    """Search scenes across all attributes: shot_type, shot_size, shot_angle, roll_type, setting, location, description, tags, visual_content."""
    import json
    q_lower = q.lower().strip()
    like = f"%{q_lower}%"
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT s.*, f.filename, f.id as file_id
            FROM scenes s
            JOIN files f ON s.file_id = f.id
            WHERE f.project_id = ?
              AND (
                  LOWER(s.shot_type)   LIKE ?
                  OR LOWER(s.shot_size)  LIKE ?
                  OR LOWER(s.shot_angle) LIKE ?
                  OR LOWER(s.roll_type)  LIKE ?
                  OR LOWER(s.setting)    LIKE ?
                  OR LOWER(s.location)   LIKE ?
                  OR LOWER(s.description) LIKE ?
                  OR LOWER(s.tags)       LIKE ?
                  OR LOWER(s.visual_content) LIKE ?
              )
            ORDER BY f.filename, s.scene_num
            LIMIT 200
        """, (project_id, like, like, like, like, like, like, like, like, like)) as cur:
            rows = await cur.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["tags"] = json.loads(d.get("tags") or "[]")
        except Exception:
            d["tags"] = []
        result.append(d)
    return result


async def update_file_frame_rate(file_id: int, fps: float | None) -> None:
    """Store a clip's frame rate (e.g. 29.97)."""
    if not fps or fps <= 0:
        return
    async with _db() as db:
        await db.execute("UPDATE files SET frame_rate = ? WHERE id = ?", (round(fps, 3), file_id))
        await db.commit()


async def has_scenes(file_id: int) -> bool:
    """True if scene detection has been run for this file."""
    async with _db() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM scenes WHERE file_id = ?", (file_id,)
        ) as cur:
            (count,) = await cur.fetchone()
        return count > 0


async def has_scene_thumbnails(file_id: int) -> bool:
    """
    True if this clip has at least one scene WITH a thumbnail. A clip can have
    scene rows whose thumbnail_path is NULL (extraction failed, e.g. drive was
    unplugged) — those need re-detection, so has_scenes() isn't enough.
    """
    async with _db() as db:
        async with db.execute(
            "SELECT 1 FROM scenes WHERE file_id = ? AND thumbnail_path IS NOT NULL LIMIT 1",
            (file_id,),
        ) as cur:
            return await cur.fetchone() is not None


async def has_scene_ai(file_id: int) -> bool:
    """True if this clip has at least one scene with AI visual_content."""
    async with _db() as db:
        async with db.execute(
            "SELECT 1 FROM scenes WHERE file_id = ? AND visual_content IS NOT NULL "
            "AND visual_content != '' LIMIT 1",
            (file_id,),
        ) as cur:
            return await cur.fetchone() is not None


async def save_poster_path(file_id: int, poster_path: str) -> None:
    """Save the poster thumbnail path for a clip."""
    async with _db() as db:
        await db.execute(
            "UPDATE files SET poster_path = ? WHERE id = ?",
            (poster_path, file_id)
        )
        await db.commit()


async def update_file_scene_summary(file_id: int, scenes: list[dict]) -> None:
    """Denormalize aggregated scene attributes onto the files table for sort/filter."""
    import json
    from collections import Counter
    if not scenes:
        return

    # Dominant shot_type (back-compat)
    shot_counter = Counter(s.get("shot_type", "unknown") for s in scenes)
    primary_type = shot_counter.most_common(1)[0][0]
    if primary_type in ("broll", "unknown") and len(shot_counter) > 1:
        for st, _ in shot_counter.most_common():
            if st not in ("broll", "unknown"):
                primary_type = st
                break

    # Most prominent shot_size (prefer closer shots — they're more selective)
    SIZE_RANK = {"extreme_close_up": 0, "close_up": 1, "medium": 2, "full": 3, "wide": 4, "extreme_wide": 5}
    sizes = [s.get("shot_size") for s in scenes if s.get("shot_size")]
    primary_size = min(sizes, key=lambda s: SIZE_RANK.get(s, 9)) if sizes else None

    # Dialogue tier — surface the HIGHEST level present across the clip's scenes
    # (heavy > dialogue > none). Legacy a_roll/b_roll are normalized.
    _norm = {"a_roll": "dialogue", "b_roll": "no_dialogue"}
    rolls = [_norm.get(s.get("roll_type"), s.get("roll_type")) for s in scenes if s.get("roll_type")]
    if "heavy_dialogue" in rolls:
        primary_roll = "heavy_dialogue"
    elif "dialogue" in rolls:
        primary_roll = "dialogue"
    elif "no_dialogue" in rolls:
        primary_roll = "no_dialogue"
    else:
        primary_roll = None

    # Setting — majority vote, ties favour outdoor
    settings = Counter(s.get("setting") for s in scenes if s.get("setting"))
    primary_setting = settings.most_common(1)[0][0] if settings else None

    # Location — first non-empty
    primary_location = next((s.get("location") for s in scenes if s.get("location")), None)

    all_tags = sorted({t for s in scenes for t in (s.get("tags") or [])})

    async with _db() as db:
        await db.execute(
            """UPDATE files SET
                primary_shot_type = ?,
                primary_shot_size = ?,
                primary_roll_type = ?,
                primary_setting   = ?,
                primary_location  = ?,
                scene_tags        = ?
               WHERE id = ?""",
            (primary_type, primary_size, primary_roll, primary_setting,
             primary_location, json.dumps(all_tags), file_id),
        )
        await db.commit()


async def get_project_poster_paths(project_id: int, limit: int = 8) -> list[str]:
    """Get poster paths from the first N clips with posters in a project."""
    async with _db() as db:
        async with db.execute(
            "SELECT poster_path FROM files WHERE project_id = ? AND poster_path IS NOT NULL ORDER BY id LIMIT ?",
            (project_id, limit)
        ) as cur:
            rows = await cur.fetchall()
        return [r[0] for r in rows]
