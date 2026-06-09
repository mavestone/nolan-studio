"""
fcpxml.py — Generate Final Cut Pro XML for DaVinci Resolve

DaVinci Resolve 18/19 imports FCPXML 1.9/1.10 natively.

Key structure (matching Resolve's expected format):
  - <asset> defines the media with id, name, uid, hasVideo, hasAudio, format
  - <media-rep> inside <asset> holds the file path (src attribute)
  - <asset-clip> on the timeline references the asset via ref=id
  - src on <asset> is NOT used — only <media-rep> carries the path

Time format: millisecond-precision rationals, e.g. 45200/1000s for 45.2s
"""

import subprocess
import json
import uuid
from pathlib import Path


def _t(seconds: float) -> str:
    """Convert seconds to FCPXML rational time (ms precision)."""
    if seconds == 0:
        return "0s"
    ms = round(seconds * 1000)
    return f"{ms}/1000s"


def _frame_aligned(seconds: float, frame_dur: str) -> str:
    """
    Snap seconds to a frame boundary using the given frameDuration.
    frame_dur is e.g. "1001/30000s" (29.97 fps) or "1/25s" (25 fps).
    Returns a rational time in the same denominator as the frame duration,
    which is what DaVinci Resolve expects for clean import.
    """
    if seconds == 0:
        return "0s"
    # Parse "num/dens" → (num, den)
    fd = frame_dur.rstrip("s")
    if "/" in fd:
        num, den = fd.split("/")
        num, den = int(num), int(den)
    else:
        num, den = int(fd), 1
    # frame_period_seconds = num / den
    # frames = seconds * den / num
    frames = round(seconds * den / num)
    total_num = frames * num
    return f"{total_num}/{den}s"


def _url(path: str) -> str:
    """Filesystem path → standard `file:///` URL per FCPXML/RFC 3986."""
    return Path(path).as_uri()


def _resolve_actual_path(stored_path: str) -> str:
    """
    If the stored path is missing on disk (folder was renamed since scan),
    walk the drive root and find a file with the same basename. Returns the
    found path if any, otherwise the original.
    """
    import os
    if os.path.exists(stored_path):
        return stored_path

    # Try to find the same filename anywhere under the drive root
    basename = os.path.basename(stored_path)
    if not basename:
        return stored_path

    # Walk up the path to find the volume root (e.g. /Volumes/DRIVE)
    parts = Path(stored_path).parts
    if len(parts) < 3 or parts[1] != "Volumes":
        return stored_path
    # parts[0] is "/" on Unix — use os.path.join so we don't get a double-slash
    drive_root = os.path.join(parts[0], parts[1], parts[2])
    if not os.path.isdir(drive_root):
        return stored_path

    # Walk the drive looking for the file (cached by basename for speed)
    cache_key = f"_nolan_pathcache_{drive_root}"
    if not hasattr(_resolve_actual_path, "_cache"):
        _resolve_actual_path._cache = {}
    cache = _resolve_actual_path._cache.setdefault(drive_root, None)
    if cache is None:
        cache = {}
        for root, _, files in os.walk(drive_root):
            for f in files:
                cache.setdefault(f, os.path.join(root, f))
        _resolve_actual_path._cache[drive_root] = cache
        import logging
        logging.getLogger("nolan").info(
            f"FCPXML path cache: indexed {len(cache)} files under {drive_root}"
        )

    found = cache.get(basename)
    if found and os.path.exists(found):
        return found
    return stored_path


def _esc(s: str) -> str:
    return (
        str(s or "")
        .replace("&",  "&amp;")
        .replace("<",  "&lt;")
        .replace(">",  "&gt;")
        .replace('"',  "&quot;")
        .replace("'",  "&apos;")
    )


def _get_tc_info(path: str):
    """Read embedded SMPTE timecode, framerate, audio/video specs."""
    import shutil, os as _os
    _ffprobe = shutil.which("ffprobe") or next(
        (c for c in ("/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe") if _os.path.exists(c)),
        "ffprobe")
    cmd = [
        _ffprobe, "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        path
    ]
    meta = {
        "offset": 0.0,
        "frame_dur": "1/25s",
        "fps": 25.0,
        "tcFormat": "NDF",
        "width": 1920,
        "height": 1080,
        "has_audio": False,
        "audio_channels": 2,
        "audio_rate": 48000
    }
    
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        
        video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
        
        if video:
            meta["width"] = video.get("width", 1920)
            meta["height"] = video.get("height", 1080)
            if "r_frame_rate" in video:
                num, den = video["r_frame_rate"].split('/')
                meta["fps"] = float(num) / float(den)
                meta["frame_dur"] = f"{den}/{num}s"
                if abs(meta["fps"] - 29.97) < 0.1 or abs(meta["fps"] - 59.94) < 0.1:
                    meta["tcFormat"] = "DF"
                    
        if audio:
            meta["has_audio"] = True
            meta["audio_channels"] = int(audio.get("channels", 2))
            meta["audio_rate"] = int(audio.get("sample_rate", 48000))
            
        tc = None
        for s in data.get("streams", []):
            tc = s.get("tags", {}).get("timecode")
            if tc: break

        if tc:
            if ';' in tc:
                meta["tcFormat"] = "DF"
            parts = tc.replace(';', ':').split(':')
            if len(parts) == 4:
                h, m, s, f = map(int, parts)
                meta["offset"] = h * 3600 + m * 60 + s + (f / meta["fps"])
    except Exception:
        pass
    return meta


def unique_formats(assets: list[dict]) -> dict[tuple, str]:
    formats = {}
    idx = 1
    for a in assets:
        meta = a["meta"]
        key = (meta["width"], meta["height"], meta["frame_dur"])
        if key not in formats:
            formats[key] = f"r{idx}"
            idx += 1
    return formats

def generate_fcpxml(
    cut_info:       dict,   # {title, narrative_note, clips: [{filename, in_time, out_time, ...}]}
    files_by_name:  dict,   # {filename: {path: str, duration_seconds: float}}
    project_name:   str,
    fps:            float = 25.0,  # Legacy parameter
) -> str:
    import os
    clips = cut_info.get("clips", [])
    if not clips:
        raise ValueError("No clips selected for the cut.")

    # Validate every referenced file exists on disk (DaVinci import dies silently otherwise)
    missing_paths = []
    for c in clips:
        info = files_by_name.get(c["filename"])
        if info and not os.path.exists(info["path"]):
            missing_paths.append(info["path"])
    if missing_paths:
        import logging
        logging.getLogger("nolan").warning(
            f"FCPXML: {len(missing_paths)} clip path(s) missing on disk — DaVinci won't find them. "
            f"Example: {missing_paths[0]}"
        )

    # ── Build asset registry (deduplicated by filename) ────────────────────
    seen: dict[str, str] = {}   # filename → asset_id
    assets: list[dict]  = []
    
    seq_meta = None

    for clip in clips:
        fname = clip["filename"]
        if fname in seen:
            continue
        info = files_by_name.get(fname)
        if not info:
            continue
            
        # Self-heal stale paths (folders renamed since scan)
        path = _resolve_actual_path(info["path"])
        if path != info["path"]:
            import logging
            logging.getLogger("nolan").info(
                f"FCPXML: resolved renamed path  {info['path']}  →  {path}"
            )
        meta = _get_tc_info(path)
        
        if not seq_meta:
            seq_meta = meta

        aid = f"a{len(assets) + 1}"
        seen[fname] = aid

        uid = uuid.uuid5(uuid.NAMESPACE_URL, fname).hex.upper()
        fd  = meta["frame_dur"]

        # Per FCPXML 1.10 spec: asset start = 0s. DaVinci reads embedded source
        # TC from the file itself. Spine asset-clip `start` is media-relative
        # (seconds from the beginning of the file, NOT including source TC).
        assets.append({
            "id":       aid,
            "name":     fname.rsplit(".", 1)[0],
            "uid":      uid,
            "src":      _url(path),
            "start":    "0s",
            "duration": _frame_aligned(info.get("duration_seconds") or 3600, fd),
            "meta":     meta
        })

    if not seq_meta:
        raise ValueError("Could not extract metadata from any files.")

    # Native formats per asset (used by both <asset> and <asset-clip>)
    formats = unique_formats(assets)

    # Always add a 4K UHD format for the SEQUENCE only (timeline output).
    # Each asset keeps its native format so DaVinci can decode it correctly.
    SEQ_FORMAT_KEY = (3840, 2160, "1001/30000s")
    if SEQ_FORMAT_KEY in formats:
        seq_format_id = formats[SEQ_FORMAT_KEY]
    else:
        seq_format_id = f"r{len(formats) + 1}"
        formats[SEQ_FORMAT_KEY] = seq_format_id

    # ── Build timeline spine ───────────────────────────────────────────────
    # Use cumulative FRAME counts (not seconds) to avoid any rounding gaps/overlaps
    # between consecutive clips on the timeline.
    spine_lines: list[str] = []

    # Master frame_dur = sequence frame duration (use first asset's)
    # Forced sequence framerate: 29.97 fps (1001/30000s) — matches the 4K timeline above
    seq_fd = "1001/30000s"
    seq_num, seq_den = 1001, 30000

    cumulative_frames = 0     # running offset on the timeline, in seq frames

    for clip in clips:
        aid = seen.get(clip["filename"])
        if not aid:
            continue

        asset = next((a for a in assets if a["id"] == aid), None)
        meta = asset["meta"] if asset else seq_meta
        fd   = meta["frame_dur"]
        clip_num, clip_den = (int(x) for x in fd.rstrip("s").split("/"))
        tc_off = meta["offset"]  # source TC offset for this asset

        # in_time / out_time = seconds from media start (set by analyzer)
        in_raw  = max(0.0, float(clip.get("in_time",  0)))
        out_raw = float(clip.get("out_time", in_raw + 10))
        dur_raw = max(0.5, out_raw - in_raw)

        # Media-relative offset (NOT absolute source TC) — DaVinci handles TC mapping itself
        in_frames  = round(in_raw * clip_den / clip_num)
        dur_frames = max(1, round(dur_raw * clip_den / clip_num))

        # Timeline offset in SEQUENCE frame space (use cumulative_frames * seq_num/seq_den)
        offset_str = "0s" if cumulative_frames == 0 else f"{cumulative_frames * seq_num}/{seq_den}s"
        start_str  = f"{in_frames * clip_num}/{clip_den}s"
        dur_str    = f"{dur_frames * clip_num}/{clip_den}s"

        name = _esc(clip["filename"].rsplit(".", 1)[0])
        fid = formats.get((meta["width"], meta["height"], meta["frame_dur"]), "r1")
        audio_role = ' audioRole="dialogue"' if meta["has_audio"] else ""

        spine_lines.append(
            f'            <asset-clip ref="{aid}" offset="{offset_str}" '
            f'name="{name}" start="{start_str}" duration="{dur_str}" '
            f'format="{fid}"{audio_role}/>'
        )

        # Advance timeline cursor by the same number of SEQUENCE frames
        # (in mixed framerate edits we approximate by seconds → seq frames)
        seq_frames_for_clip = round(dur_raw * seq_den / seq_num)
        cumulative_frames += seq_frames_for_clip

    # Sequence total duration in sequence-frame space
    total_dur = f"{cumulative_frames * seq_num}/{seq_den}s" if cumulative_frames else "0s"
    cut_title = _esc(cut_info.get("title") or f"{project_name} — Narrative Cut")

    # ── Assemble FCPXML ────────────────────────────────────────────────────
    
    format_xml = "\n".join(
        f'        <format id="{fid}" name="FFVideoFormat{h}p" '
        f'frameDuration="{fd}" fieldOrder="progressive" width="{w}" '
        f'height="{h}" colorSpace="1-1-1 (Rec. 709)"/>'
        for (w, h, fd), fid in formats.items()
    )

    asset_xml_lines = []
    for a in assets:
        m = a["meta"]
        fid = formats.get((m["width"], m["height"], m["frame_dur"]), "r1")

        has_audio = "1" if m["has_audio"] else "0"
        audio_attrs = ""
        if m["has_audio"]:
            audio_attrs = (
                f' audioChannels="{m["audio_channels"]}"'
                f' audioRate="{m["audio_rate"]}"'   # Hz, e.g. 48000 (NOT "48k")
            )

        # NOTE: no `uid` attribute — DaVinci computes its own from the file's
        # actual metadata. A wrong/fabricated UID makes Resolve treat the
        # clip as a different file and fail to link to the media on disk.
        asset_xml_lines.append(
            f'        <asset id="{a["id"]}" name="{_esc(a["name"])}" '
            f'start="{a["start"]}" duration="{a["duration"]}" hasVideo="1" '
            f'format="{fid}" hasAudio="{has_audio}"{audio_attrs}>\n'
            f'            <media-rep kind="original-media" src="{a["src"]}"/>\n'
            f'        </asset>'
        )
    asset_xml = "\n".join(asset_xml_lines)

    spine_xml = "\n".join(spine_lines)

    note_attr = ""
    note_text = cut_info.get("narrative_note", "")
    if note_text:
        note_attr = f'\n                    note="{_esc(note_text[:300])}"'
        
    main_fid = seq_format_id   # forced 4K UHD 29.97

    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.10">
    <resources>
{format_xml}
{asset_xml}
    </resources>
    <library>
        <event name="Nolan — {_esc(project_name)}">
            <project name="{cut_title}"{note_attr}>
                <sequence duration="{total_dur}" format="{main_fid}"
                          tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48000">
                    <spine>
{spine_xml}
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>
"""
