from pathlib import Path
from faster_whisper import WhisperModel
import av

_model = None

VIDEO_EXTENSIONS = {".mp4", ".mov"}


def get_model(size: str = "base") -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel(size, device="auto", compute_type="auto")
    return _model


def get_video_duration(video_path: str) -> float:
    try:
        with av.open(video_path) as container:
            if container.duration:
                return float(container.duration) / 1_000_000  # microseconds → seconds
    except Exception:
        pass
    return 0.0


class NoAudioError(Exception):
    """Raised when a file has no audio stream — should map to 'silent' status, not 'error'."""
    pass


def has_audio_stream(video_path: str) -> bool:
    """Quick check: does this file contain at least one audio stream?"""
    try:
        with av.open(video_path) as container:
            return any(s.type == "audio" for s in container.streams)
    except Exception:
        return False


def transcribe_file(video_path: str, model_size: str = "base", progress_callback=None) -> list[dict]:
    # Pre-flight: no audio stream → silent, not error
    if not has_audio_stream(video_path):
        raise NoAudioError("File has no audio stream")

    model = get_model(model_size)

    if progress_callback:
        progress_callback("transcribing")

    # faster-whisper 1.x accepts video paths directly via PyAV
    segments, info = model.transcribe(
        video_path,
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )

    result = []
    for seg in segments:
        result.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip(),
        })

    return result


def scan_folder(folder_path: str) -> list[dict]:
    folder = Path(folder_path)
    files = []
    seen = set()

    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        # Skip macOS metadata sidecar files (._FILENAME) and other dotfiles
        if p.name.startswith(".") or p.name.startswith("._"):
            continue
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        files.append({
            "path": str(p),
            "filename": p.name,
            "size_bytes": p.stat().st_size,
        })

    files.sort(key=lambda f: f["path"])
    return files
