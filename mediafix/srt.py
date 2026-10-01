import os
import tempfile
from pathlib import Path


def format_timestamp(seconds: float) -> str:
    total = max(0.0, float(seconds))
    hours, remainder = divmod(int(total), 3600)
    minutes, secs = divmod(remainder, 60)
    millis = int(round((total - int(total)) * 1000))
    if millis == 1000:
        millis = 0
        secs += 1
    if secs == 60:
        secs = 0
        minutes += 1
    if minutes == 60:
        minutes = 0
        hours += 1
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def render_srt(segments) -> str:
    blocks = []
    for index, segment in enumerate(segments, start=1):
        text = (segment.text or "").strip()
        if not text:
            continue
        start = format_timestamp(segment.start)
        end = format_timestamp(max(segment.end, segment.start + 0.05))
        blocks.append(f"{index}\n{start} --> {end}\n{text}\n")
    return "\n".join(blocks)


def write_atomic(path, text: str) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n",
        dir=destination.parent, prefix=f".{destination.stem}.", suffix=destination.suffix,
        delete=False,
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, destination)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    return destination


def sidecar_path(video_path, language: str, extension: str = ".srt") -> Path:
    video = Path(video_path)
    tag = language.lower()
    tag = {"en": "eng", "english": "eng"}.get(tag, tag)
    return video.with_name(f"{video.stem}.{tag}{extension}")


def parse_sidecar_language(stem: str, base: str):
    parts = base.split(".")
    tags = [p for p in parts[1:] if p]
    if not tags:
        return None
    return ".".join(tags)