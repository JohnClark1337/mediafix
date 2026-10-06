import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

_VENDOR_ROOT = Path(__file__).resolve().parent.parent
if str(_VENDOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_VENDOR_ROOT))

import audio_downmix as vendor  # noqa: E402

is_english = vendor.is_english
has_english_stereo = vendor.has_english_stereo

CENSOR_TRACK_TITLE = "Censored (Bleeparr)"


@dataclass(frozen=True)
class AudioStreamInfo:
    index: int
    codec: str = ""
    channels: int = 0
    layout: str = ""
    language: str = ""
    title: str = ""
    default: bool = False
    voice: bool = False

    @property
    def is_english(self) -> bool:
        return is_english(self.language) or is_english(self.title)

    @property
    def is_surround(self) -> bool:
        return self.channels >= 6


@dataclass(frozen=True)
class SubtitleStreamInfo:
    index: int
    codec: str = ""
    language: str = ""
    title: str = ""
    default: bool = False

    @property
    def is_english(self) -> bool:
        return is_english(self.language) or is_english(self.title)


@dataclass
class MediaInfo:
    path: str
    size: int = 0
    duration: float | None = None
    audio: list[AudioStreamInfo] = field(default_factory=list)
    subtitles: list[SubtitleStreamInfo] = field(default_factory=list)
    error: str | None = None

    @property
    def has_english_subtitle(self) -> bool:
        return any(s.is_english for s in self.subtitles)

    @property
    def has_english_stereo(self) -> bool:
        return any(
            s.channels == 2 and s.is_english and not (s.title or "").startswith(CENSOR_TRACK_TITLE)
            for s in self.audio
        )

    def has_censored_track(self, prefix: str = CENSOR_TRACK_TITLE) -> bool:
        return any((s.title or "").startswith(prefix) for s in self.audio)

    @property
    def has_surround(self) -> bool:
        return any(s.is_surround for s in self.audio)

    @property
    def has_audio(self) -> bool:
        return bool(self.audio)

    @property
    def needs_audio(self) -> bool:
        return self.has_surround and not self.has_english_stereo

    @property
    def primary_audio(self) -> AudioStreamInfo | None:
        return self.audio[0] if self.audio else None


def _to_media_info(path, size, data) -> MediaInfo:
    streams = data.get("streams") or []
    audio = []
    subtitles = []
    duration = None
    audio_relative = 0
    for stream in streams:
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        language = tags.get("language", "") or ""
        title = tags.get("title", "") or ""
        default = bool(disposition.get("default"))
        kind = stream.get("codec_type")
        if kind == "audio":
            channels = int(stream.get("channels") or 0)
            audio.append(AudioStreamInfo(
                index=audio_relative,
                codec=stream.get("codec_name", ""),
                channels=channels,
                layout=stream.get("channel_layout", "") or "",
                language=language,
                title=title,
                default=default,
                voice=bool((stream.get("tags") or {}).get("comment", "").lower().startswith("dialog")),
            ))
            audio_relative += 1
        elif kind == "subtitle":
            subtitles.append(SubtitleStreamInfo(
                index=int(stream.get("index") or 0),
                codec=stream.get("codec_name", ""),
                language=language,
                title=title,
                default=default,
            ))
    fmt = data.get("format") or {}
    raw_duration = fmt.get("duration")
    if raw_duration:
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            duration = None
    if duration is None and data.get("streams"):
        for stream in streams:
            if stream.get("duration"):
                try:
                    duration = float(stream["duration"])
                except (TypeError, ValueError):
                    continue
                break
    return MediaInfo(
        path=str(path),
        size=int(size or 0),
        duration=duration,
        audio=audio,
        subtitles=subtitles,
    )


def probe(path, ffprobe: str = "ffprobe") -> MediaInfo:
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError as exc:
        return MediaInfo(path=str(target), error=f"stat failed: {exc}")

    try:
        data = vendor.probe_file(ffprobe, str(target))
    except (RuntimeError, OSError, ValueError) as exc:
        return MediaInfo(path=str(target), size=size, error=str(exc))

    if not isinstance(data, dict):
        return MediaInfo(path=str(target), size=size, error="ffprobe returned no stream data")

    info = _to_media_info(target, size, data)
    if info.duration is None or not info.audio:
        try:
            fallback = subprocess.run(
                [ffprobe, "-v", "error", "-print_format", "json",
                 "-show_streams", "-show_format", str(target)],
                capture_output=True, text=True, timeout=120,
            )
            if fallback.returncode == 0:
                retry = _to_media_info(target, size, json.loads(fallback.stdout))
                if retry.duration is not None:
                    info.duration = retry.duration
                if not info.audio and retry.audio:
                    info.audio = retry.audio
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
    return info