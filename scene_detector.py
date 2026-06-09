"""
scene_detector.py — Scene detection + thumbnail extraction + pro-grade shot classification

Pipeline per detected scene:
  1. Extract a 320px JPEG thumbnail at midpoint via ffmpeg
  2. OpenCV heuristic classifier — fills in shot_size, shot_angle, setting, tags
     (free, fast, no API). Always runs.
  3. A-roll / B-roll determined from caller-supplied transcript overlap
  4. (Optional) Claude vision classifier refines + adds specific location/description

Schema returned per scene:
  {
    scene_num, start_time, end_time, thumbnail_path,
    shot_type,    # back-compat: closeup | medium | wide | broll
    shot_size,    # extreme_close_up | close_up | medium | full | wide | extreme_wide
    shot_angle,   # eye_level | low | high | dutch | over_shoulder | pov
    setting,      # indoor | outdoor
    location,     # free text — set by AI only
    description,  # one-line — set by AI only
    roll_type,    # a_roll | b_roll  (set from transcript overlap)
    tags,         # list[str]
    ai_classified # bool
  }
"""

import json
import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger("nolan")

THUMBNAILS_DIR = Path("static/thumbnails")
THUMB_WIDTH    = 320
POSTER_WIDTH   = 400


# ── OpenCV heuristic classifier ───────────────────────────────────────────────

def _classify_shot_opencv(thumbnail_path: str) -> dict:
    """
    OpenCV-only classifier. Returns the legacy fields plus granular shot_size/angle/setting/tags.
    Always returns *something* — uses placeholders for fields it can't determine.
    """
    try:
        import cv2
        import numpy as np

        img = cv2.imread(str(thumbnail_path))
        if img is None:
            return _empty_classification()

        h, w = img.shape[:2]
        frame_area = float(w * h)
        tags: list[str] = []

        # ── Face detection ────────────────────────────────────────────────
        face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=4, minSize=(20, 20)
        )

        shot_size  = "extreme_wide"
        shot_type  = "broll"
        shot_angle = "eye_level"

        if len(faces) > 0:
            largest = max(faces, key=lambda f: f[2] * f[3])
            fx, fy, fw, fh = largest
            face_ratio = (fw * fh) / frame_area

            # Granular shot size
            if face_ratio > 0.30:
                shot_size = "extreme_close_up"
                shot_type = "closeup"
            elif face_ratio > 0.14:
                shot_size = "close_up"
                shot_type = "closeup"
            elif face_ratio > 0.05:
                shot_size = "medium"
                shot_type = "medium"
            elif face_ratio > 0.015:
                shot_size = "full"
                shot_type = "wide"
            else:
                shot_size = "wide"
                shot_type = "wide"

            # Angle estimation from face position in frame
            face_cy = fy + fh / 2
            rel_y   = face_cy / h
            if rel_y < 0.30:
                shot_angle = "low"           # face high in frame → camera looking up
            elif rel_y > 0.65:
                shot_angle = "high"          # face low in frame → camera looking down
            else:
                shot_angle = "eye_level"

            # People tags
            if len(faces) >= 3:
                tags.append("group")
            elif len(faces) == 2:
                tags.append("two-shot")
            else:
                tags.append("person")
                if shot_size in ("close_up", "extreme_close_up", "medium"):
                    tags.append("interview")

            if shot_size in ("close_up", "extreme_close_up"):
                tags.append("face")
        else:
            shot_type = "broll"
            shot_size = "wide"   # default for non-face shots — AI will refine

        # ── Indoor / outdoor classification (scored heuristic) ─────────────
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

        # Sky — only count blue pixels in the UPPER HALF (sky doesn't live below)
        upper_hsv = hsv[: h // 2, :, :]
        upper_area = float(upper_hsv.shape[0] * upper_hsv.shape[1]) or 1.0
        sky_mask = cv2.inRange(upper_hsv, np.array([95, 40, 80]), np.array([135, 255, 255]))
        sky_pct  = sky_mask.sum() / (255.0 * upper_area)

        # Sand / warm earth tones
        sand_mask = cv2.inRange(hsv, np.array([10, 15, 80]), np.array([40, 200, 255]))
        sand_pct  = sand_mask.sum() / (255.0 * frame_area)

        # Greenery
        green_mask = cv2.inRange(hsv, np.array([35, 30, 30]), np.array([85, 255, 255]))
        green_pct  = green_mask.sum() / (255.0 * frame_area)

        # Saturation + brightness
        sat_mean = float(hsv[:, :, 1].mean())
        v_mean   = float(hsv[:, :, 2].mean())

        # Hue diversity — outdoor scenes tend to have a wider palette
        hue_hist = cv2.calcHist([hsv], [0], None, [18], [0, 180])
        hue_hist /= max(hue_hist.sum(), 1.0)
        hue_diversity = int((hue_hist > 0.05).sum())

        # Edge density
        gray_for_edges = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray_for_edges, 100, 200)
        edge_pct = float((edges > 0).sum()) / frame_area

        # Score
        outdoor_score = 0
        if sky_pct   > 0.08: outdoor_score += 3
        if sky_pct   > 0.20: outdoor_score += 2
        if sand_pct  > 0.20: outdoor_score += 2
        if green_pct > 0.20: outdoor_score += 2
        if hue_diversity >= 6: outdoor_score += 1
        if sat_mean > 60 and edge_pct > 0.06: outdoor_score += 1

        indoor_score = 0
        if sky_pct < 0.02 and sand_pct < 0.05 and green_pct < 0.05:
            indoor_score += 2
        if hue_diversity <= 3: indoor_score += 1
        if sat_mean < 45: indoor_score += 1
        if edge_pct > 0.12 and sky_pct < 0.02: indoor_score += 1

        if v_mean < 50:
            tags.append("night")
            setting = "outdoor" if sky_pct > 0.02 else "indoor"
        elif outdoor_score > indoor_score:
            setting = "outdoor"
        else:
            setting = "indoor"

        tags.append(setting)
        if setting == "outdoor" and v_mean > 110 and sky_pct > 0.05:
            tags.append("day")
        if sand_pct > 0.18:
            tags.append("desert")
        if green_pct > 0.20:
            tags.append("nature")

        return {
            "shot_type":    shot_type,
            "shot_size":    shot_size,
            "shot_angle":   shot_angle,
            "setting":      setting,
            "tags":         tags,
            "location":     None,
            "description":  None,
            "ai_classified": False,
        }

    except Exception as e:
        log.debug(f"[classify_shot_opencv] {e}")
        return _empty_classification()


def _empty_classification() -> dict:
    """Sensible defaults so every scene always has shot_size, shot_angle, setting set."""
    return {
        "shot_type":  "broll",
        "shot_size":  "wide",        # never null
        "shot_angle": "eye_level",   # never null
        "setting":    "outdoor",     # never null — most common default
        "tags": [], "location": None,
        "description": None, "ai_classified": False,
    }


# ── Claude vision classifier (optional, runs when online) ─────────────────────

def classify_shot_with_claude(thumbnail_path: str, has_dialogue: bool = False) -> dict | None:
    """
    Use Claude Haiku via the local Claude CLI to classify a scene thumbnail.
    Returns the parsed JSON dict on success, None on any failure (caller falls back).
    """
    try:
        # Import here to avoid hard dependency at module load
        from analyzer import _try_claude_cli, _claude_available
        if not _claude_available:
            return None

        abs_path = Path(thumbnail_path).resolve()
        if not abs_path.exists():
            return None

        prompt = f"""Analyze the single film frame at @{abs_path}.

This scene {'CONTAINS spoken dialogue' if has_dialogue else 'has NO spoken dialogue'}.

Respond with ONLY a JSON object — no prose, no markdown fence — using these exact keys and value vocabularies:

{{
  "shot_size":      "extreme_close_up | close_up | medium | full | wide | extreme_wide | aerial",
  "shot_angle":     "eye_level | low | high | dutch | over_shoulder | pov",
  "setting":        "indoor | outdoor",
  "location":       "<2-4 word specific location: desert dune, kitchen interior, car cabin, beach, hotel lobby, etc.>",
  "description":    "<one sentence describing the shot composition and action>",
  "visual_content": "<comma-separated list of 8-20 specific nouns/adjectives that are VISIBLE in this frame — objects, materials, body parts, animals, vehicles, nature elements, textures, clothing items, etc. Be very specific. Examples: shoe, grass, hand, watch, sand, camel, water, leather jacket, phone, coffee cup, road, palm tree, ring, beard, tears, sunglasses>"
}}

Definitions:
- shot_size: extreme_close_up = eyes/lips fill frame; close_up = head/shoulders; medium = waist-up; full = whole body; wide = body in environment; extreme_wide = environment dominates; aerial = top-down/bird's eye view from above (typically drone)
- shot_angle: low = camera below subject looking up; high = camera above looking down; dutch = noticeable tilt; over_shoulder = OTS framing; pov = first person view; eye_level otherwise
- setting + location should agree (indoor = "kitchen", "office"; outdoor = "desert", "beach")
- visual_content: list EVERYTHING specific you can see — this powers search, so the more specific the better"""

        system = (
            "You are a professional film analyst classifying single frames. "
            "Output strict JSON only. No markdown, no commentary."
        )

        raw = _try_claude_cli(prompt, system, max_tokens=400, cli_model="claude-haiku-4-5")
        if not raw:
            return None

        # Extract JSON from response (model may add stray text)
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            log.debug(f"Claude returned non-JSON: {raw[:200]}")
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            log.debug(f"Claude JSON parse failed: {e} -- {raw[:200]}")
            return None

        # Normalise — accept slight variations
        normalised = {
            "shot_size":      _norm_enum(data.get("shot_size"),  SHOT_SIZES),
            "shot_angle":     _norm_enum(data.get("shot_angle"), SHOT_ANGLES),
            "setting":        _norm_enum(data.get("setting"),    {"indoor", "outdoor"}),
            "location":       (data.get("location") or "").strip()[:80] or None,
            "description":    (data.get("description") or "").strip()[:240] or None,
            "visual_content": (data.get("visual_content") or "").strip()[:500] or None,
            "ai_classified":  True,
        }
        # Derive legacy shot_type from shot_size
        normalised["shot_type"] = _shot_type_from_size(normalised["shot_size"], has_dialogue)
        return normalised

    except Exception as e:
        log.debug(f"[classify_shot_with_claude] {e}")
        return None


SHOT_SIZES  = {"extreme_close_up", "close_up", "medium", "full", "wide", "extreme_wide", "aerial"}
SHOT_ANGLES = {"eye_level", "low", "high", "dutch", "over_shoulder", "pov"}


def _norm_enum(val, allowed: set) -> str | None:
    if not val:
        return None
    v = str(val).strip().lower().replace("-", "_").replace(" ", "_")
    return v if v in allowed else None


def _shot_type_from_size(shot_size: str | None, has_dialogue: bool) -> str:
    """Back-compat shot_type for older UI: closeup | medium | wide | broll."""
    if shot_size in ("close_up", "extreme_close_up"):
        return "closeup"
    if shot_size == "medium":
        return "medium"
    if shot_size in ("wide", "full"):
        return "wide" if has_dialogue else "broll"
    if shot_size == "extreme_wide":
        return "broll"
    return "broll" if not has_dialogue else "medium"


# ── Scene detection driver ────────────────────────────────────────────────────

def detect_scenes(
    file_id: int,
    path: str,
    threshold: float = 2.0,
    transcript_segments: list[dict] | None = None,
    use_ai: bool = False,
) -> list[dict]:
    """
    Detect scene cuts + classify each one.

    transcript_segments: list of {start, end, text} — used to set roll_type
                         (a_roll if speech overlaps the scene, else b_roll)
    use_ai: if True, also run Claude vision per scene (only if claude_available)
    """
    try:
        from scenedetect import open_video, SceneManager
        from scenedetect.detectors import AdaptiveDetector

        thumb_dir = THUMBNAILS_DIR / str(file_id)
        thumb_dir.mkdir(parents=True, exist_ok=True)

        video   = open_video(path)
        manager = SceneManager()
        # auto_downscale (on by default) shrinks frames for the cut-detection
        # math. The expensive part is decoding the source frames, which we
        # can't avoid without losing accuracy on multi-cut clips.
        manager.add_detector(AdaptiveDetector(adaptive_threshold=threshold))
        manager.detect_scenes(video, show_progress=False)
        scene_list = manager.get_scene_list()

        if not scene_list:
            dur = video.duration.get_seconds() if video.duration else 0.0
            raw_scenes = [(0.0, dur)]
        else:
            raw_scenes = [(s.get_seconds(), e.get_seconds()) for s, e in scene_list]

        transcript_segments = transcript_segments or []

        scenes = []
        for i, (start_s, end_s) in enumerate(raw_scenes):
            mid_s     = (start_s + end_s) / 2
            thumb_rel = f"thumbnails/{file_id}/{i:04d}.jpg"
            thumb_abs = Path("static") / thumb_rel

            thumb_ok = False
            try:
                _extract_thumbnail(path, mid_s, str(thumb_abs))
                thumb_ok = True
            except Exception as te:
                log.debug(f"[scene_detector] thumb {i} failed: {te}")
                thumb_rel = None

            # A-roll vs B-roll from transcript overlap
            has_dialogue = _scene_has_dialogue(start_s, end_s, transcript_segments)
            roll_type    = "a_roll" if has_dialogue else "b_roll"

            # Always start with OpenCV
            cls = _classify_shot_opencv(str(thumb_abs)) if thumb_ok else _empty_classification()

            # Optionally refine with AI
            if use_ai and thumb_ok:
                ai = classify_shot_with_claude(str(thumb_abs), has_dialogue)
                if ai:
                    # AI fields take priority; OpenCV tags still useful
                    for k in ("shot_size", "shot_angle", "setting", "location", "description", "visual_content"):
                        if ai.get(k):
                            cls[k] = ai[k]
                    cls["shot_type"]     = ai.get("shot_type") or cls.get("shot_type")
                    cls["ai_classified"] = True

            scenes.append({
                "scene_num":      i,
                "start_time":     round(start_s, 3),
                "end_time":       round(end_s,   3),
                "thumbnail_path": thumb_rel,
                "roll_type":      roll_type,
                **cls,
            })

        log.info(f"[file {file_id}] {len(scenes)} scenes detected (ai={use_ai})")
        return scenes

    except Exception as e:
        log.warning(f"[file {file_id}] Scene detection error: {e}")
        return []


def _scene_has_dialogue(start_s: float, end_s: float, segments: list[dict]) -> bool:
    """True if any transcript segment overlaps this scene's time range."""
    for seg in segments:
        seg_start = seg.get("start_time") or seg.get("start") or 0
        seg_end   = seg.get("end_time")   or seg.get("end")   or seg_start
        if seg_end >= start_s and seg_start <= end_s:
            text = (seg.get("text") or "").strip()
            if len(text) > 2:  # ignore single-character noise
                return True
    return False


def classify_shot(thumbnail_path: str) -> dict:
    """Back-compat wrapper. Returns legacy shot_type + tags."""
    cls = _classify_shot_opencv(thumbnail_path)
    return {"shot_type": cls.get("shot_type", "unknown"), "tags": cls.get("tags", [])}


def extract_clip_poster(file_id: int, path: str, at_seconds: float = 5.0) -> str | None:
    try:
        thumb_dir = THUMBNAILS_DIR / str(file_id)
        thumb_dir.mkdir(parents=True, exist_ok=True)
        thumb_abs = thumb_dir / "poster.jpg"
        _extract_thumbnail(path, at_seconds, str(thumb_abs), width=POSTER_WIDTH)
        return f"thumbnails/{file_id}/poster.jpg"
    except Exception as e:
        log.debug(f"[file {file_id}] Poster extraction failed: {e}")
        return None


def _extract_thumbnail(video_path: str, timestamp: float,
                       output_path: str, width: int = THUMB_WIDTH) -> None:
    ts = max(0.0, timestamp)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{ts:.3f}",
        "-i", video_path,
        "-vframes", "1",
        "-vf", f"scale={width}:-2",
        "-q:v", "4",
        output_path,
    ]
    subprocess.run(cmd, capture_output=True, check=True, timeout=30)
