"""Profanity censorship ("bleeparr") for mediafix.

This module is a library port of David Peele's Bleeparr CLI
(davidpeele/bleeparr_CLI, MIT). Bleeparr locates profanity in a subtitle
track, refines each occurrence down to per-word timestamps with Whisper and
then appends a "Censored (Bleeparr)" audio track to the original file,
preserving every original stream.

The record of known swear words ships as swears.txt, derived from the
cleanvid project (mmguero/cleanvid, BSD-3-Clause).

Unlike the CLI it was ported from, nothing here touches argparse, exits the
process or writes a `clips/` folder next to the video: all scratch files live
in a per-job temporary directory and every failure is reported as an
exception or result instead of a hard exit.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from mediafix import downmix as downmix_mod
from mediafix import srt as srt_mod
from mediafix import subtitles as sub_mod
from mediafix.probe import CENSOR_TRACK_TITLE

_MODEL_BY_TIER = {"S": "small.en", "M": "medium.en"}
_CENSOR_TMP_SUFFIX = ".bleeparr-tmp"


class CensorError(RuntimeError):
    """A fatal failure inside a censorship job."""


@dataclass(frozen=True)
class SwearMatcher:
    """Case-insensitive swear list built from the swears.txt vocabulary."""

    pattern: re.Pattern | None
    single_words: frozenset[str]

    def find(self, text: str) -> list[str]:
        """Return the distinct bad words found in *text* (lowercased)."""
        if not text or self.pattern is None:
            return []
        seen: set[str] = set()
        result: list[str] = []
        for match in self.pattern.finditer(text):
            word = match.group(0).lower()
            if word not in seen:
                seen.add(word)
                result.append(word)
        return result

    def any_in(self, text: str) -> bool:
        if not text or self.pattern is None:
            return False
        return self.pattern.search(text) is not None

    def word_is_bad(self, word: str) -> bool:
        return word in self.single_words

    def search_file(self, path):
        """Cheap whole-file scan used by the scanner - never loads it fully.

        Returns True when a swear is found, False when the file reads clean,
        and None when the file cannot be read (treat as "unknown/candidate").
        """
        path = Path(path)
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if self.any_in(line):
                        return True
        except (OSError, UnicodeDecodeError):
            return None
        return False


def default_swears_path() -> str:
    return str(Path(__file__).resolve().parent / "swears.txt")


def load_swears(source=None) -> SwearMatcher:
    """Build a matcher from a word list (or the packaged swears.txt).

    Each line is trimmed; the part before a `|` is the actual swear (the right
    side of the cleanvid format is only the textual substitution used by other
    tools and is ignored here). Lines containing spaces are treated as
    multi-word phrases so "blow job" cannot match "blow". Matching is
    case-insensitive with word boundaries.
    """
    path = Path(source) if source else Path(default_swears_path())
    words: set[str] = set()
    phrases: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CensorError(f"cannot read swear list {path}: {exc}") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entry = line.split("|", 1)[0].strip().lower()
        if not entry:
            continue
        if " " in entry:
            phrases.append(re.escape(entry))
        else:
            words.add(entry)
    vocabulary = sorted(words | set(phrases))
    pattern = (
        re.compile(r"\b(?:" + "|".join(vocabulary) + r")\b", re.IGNORECASE)
        if vocabulary
        else None
    )
    return SwearMatcher(pattern, single_words=frozenset(words))


def mask_word(word: str) -> str:
    word = str(word)
    if len(word) <= 1:
        return "*"
    return word[0] + "*" * (len(word) - 1)


# --------------------------------------------------------------------------
# Timed subtitle segments (SRT / ASS / VTT / MicroDVD)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TimedSegment:
    start: float
    end: float
    text: str
    index: int


def _parse_srt_loose(text: str) -> list[TimedSegment]:
    out: list[TimedSegment] = []
    index = 0
    for block in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        timing = next((ln for ln in lines if "-->" in ln), None)
        if not timing:
            continue
        matched = re.search(
            r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
            r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})",
            timing,
        )
        if not matched:
            continue
        parts = [int(x) for x in matched.groups()]
        start = parts[0] * 3600 + parts[1] * 60 + parts[2] + parts[3] / 1000.0
        end = parts[4] * 3600 + parts[5] * 60 + parts[6] + parts[7] / 1000.0
        index += 1
        text_lines = [ln for ln in lines if "-->" not in ln]
        # An SRT cue normally starts with its index number; drop it so the
        # text does not pick up "1 Hello world".
        while text_lines and text_lines[0].isdigit():
            text_lines = text_lines[1:]
        text = " ".join(text_lines).strip()
        if text:
            out.append(TimedSegment(start, end, text, index))
    return out


def _parse_srt(text: str) -> list[TimedSegment]:
    try:
        import srt

        parsed = list(srt.parse(text))
    except Exception:  # noqa: BLE001 - tolerate malformed files
        return _parse_srt_loose(text)
    out: list[TimedSegment] = []
    for entry in parsed:
        text = (entry.content or "").strip()
        if text:
            out.append(TimedSegment(
                start=entry.start.total_seconds(),
                end=entry.end.total_seconds(),
                text=text,
                index=int(entry.index),
            ))
    return out


def _vtt_times(line: str) -> tuple[float, float] | None:
    matched = re.search(
        r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
        r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})",
        line,
    )
    if matched:
        parts = [int(x) for x in matched.groups()]
        return (
            parts[0] * 3600 + parts[1] * 60 + parts[2] + parts[3] / 1000.0,
            parts[4] * 3600 + parts[5] * 60 + parts[6] + parts[7] / 1000.0,
        )
    matched = re.search(
        r"(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2})[.,](\d{1,3})",
        line,
    )
    if matched:
        parts = [int(x) for x in matched.groups()]
        return (
            parts[0] * 60 + parts[1] + parts[2] / 1000.0,
            parts[3] * 60 + parts[4] + parts[5] / 1000.0,
        )
    matched = re.search(
        r"(\d{1,2}):(\d{2}):(\d{2})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})",
        line,
    )
    if matched:
        parts = [int(x) for x in matched.groups()]
        return (
            parts[0] * 3600 + parts[1] * 60 + parts[2],
            parts[3] * 3600 + parts[4] * 60 + parts[5],
        )
    matched = re.search(
        r"(\d{1,2}):(\d{2})\s*-->\s*(\d{1,2}):(\d{2})",
        line,
    )
    if matched:
        parts = [int(x) for x in matched.groups()]
        return (parts[0] * 60 + parts[1], parts[2] * 60 + parts[3])
    return None


def _parse_vtt(text: str) -> list[TimedSegment]:
    out: list[TimedSegment] = []
    index = 0
    cue: list[str] = []
    timings: tuple[float, float] | None = None

    def flush() -> None:
        nonlocal cue, timings
        content = " ".join(cue).strip()
        if content and timings is not None:
            out.append(TimedSegment(timings[0], timings[1], content, len(out) + 1))
        cue, timings = [], None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("WEBVTT") or line.startswith(("NOTE ", "STYLE ", "REGION ", "Kind:", "Language:")):
            continue
        if "-->" in line:
            timings = _vtt_times(line)
            continue
        if timings is not None:
            cue.append(line)
    flush()
    return out


def _ass_time(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    tail = parts[-1].split(".")
    seconds = int(tail[0])
    sub = float("0." + tail[1]) if len(tail) > 1 else 0.0
    minutes = int(parts[-2]) if len(parts) > 1 else 0
    hours = int(parts[-3]) if len(parts) > 2 else 0
    return hours * 3600 + minutes * 60 + seconds + sub


def _parse_ass(text: str) -> list[TimedSegment]:
    out: list[TimedSegment] = []
    for line in text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        fields = line[len("Dialogue:"):].lstrip().split(",", 9)
        if len(fields) < 10:
            continue
        content = fields[9].strip()
        if not content:
            continue
        try:
            start = _ass_time(fields[1])
            end = _ass_time(fields[2])
        except (IndexError, ValueError):
            continue
        out.append(TimedSegment(start, end, content, len(out) + 1))
    return out


def _parse_sub(text: str) -> list[TimedSegment]:
    out: list[TimedSegment] = []
    fps = 25.0
    for line in text.splitlines():
        matched = re.match(r"\s*\{(\d+)\}\{(\d+)\}(.*)", line, re.S)
        if not matched:
            continue
        content = (matched.group(3) or "").strip().replace("|", " ").strip()
        if not content:
            continue
        start, end = int(matched.group(1)) / fps, int(matched.group(2)) / fps
        out.append(TimedSegment(start, end, content, len(out) + 1))
    return out


def parse_timed_segments(path) -> list[TimedSegment] | None:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        try:
            text = path.read_text(encoding="latin1")
        except OSError:
            return None
    suffix = path.suffix.lower()
    if suffix in {".ass", ".ssa"}:
        return _parse_ass(text)
    if suffix == ".vtt":
        return _parse_vtt(text)
    if suffix == ".sub":
        return _parse_sub(text)
    return _parse_srt(text)


def find_bad_sections(segments, matcher: SwearMatcher) -> list[dict]:
    """Return subtitle sections containing at least one swear word."""
    sections: list[dict] = []
    for segment in segments:
        words = matcher.find(segment.text)
        if words:
            sections.append({
                "sub_index": segment.index,
                "start": segment.start,
                "end": segment.end,
                "words": words,
                "content": segment.text,
            })
    return sections


def bad_sections_from_file(path, matcher: SwearMatcher) -> list[dict]:
    segments = parse_timed_segments(path)
    if not segments:
        return []
    return find_bad_sections(segments, matcher)


# --------------------------------------------------------------------------
# Whisper engine + clip refinement
# --------------------------------------------------------------------------

def _normalize_whisper_word(word: str) -> str:
    return re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", (word or "").strip().lower())


class CensorEngine:
    """Lazily caches the Whisper models used to refine clip timestamps."""

    def __init__(self, config):
        self.config = config
        self._models: dict[str, object] = {}
        self._lock = threading.Lock()

    def model(self, name: str):
        with self._lock:
            model = self._models.get(name)
            if model is None:
                sub_mod.SubtitleEngine._check_cache()
                from faster_whisper import WhisperModel

                model = WhisperModel(
                    name,
                    device=self.config.device,
                    compute_type=self.config.compute_type,
                    cpu_threads=self.config.cpu_threads,
                )
                self._models[name] = model
            return model


def _bleeptool_tiers(bleeptool: str) -> list[tuple[str, str]]:
    tiers: list[tuple[str, str]] = []
    for part in re.split(r"[^A-Za-z]+", bleeptool or ""):
        part = part.strip().upper()
        if part in {"S", "M"}:
            tiers.append((part, part))
        elif part == "FSM":
            tiers.append(("FSM", "subtitle"))
    return tiers


def _tier_model(tier: str, censor_models) -> str:
    if censor_models:
        index = {"S": 0, "M": 1}.get(tier, 0)
        if index < len(censor_models):
            return censor_models[index]
    return _MODEL_BY_TIER.get(tier, "small.en")


def _whisper_refine_clips(engine, clips_dir, indices, matcher, model_name, tag,
                          progress=None, cancel: threading.Event | None = None,
                          ) -> tuple[list[dict], list[int]]:
    """Transcribe the given clips and return (word hits, still-missed indices)."""
    model = engine.model(model_name)
    clips = sorted(f for f in os.listdir(clips_dir) if f.endswith(".wav"))
    hits: list[dict] = []
    missed: list[int] = []
    for idx in indices:
        if cancel is not None and cancel.is_set():
            break
        filename = clips[idx]
        segments, _info = model.transcribe(
            os.path.join(clips_dir, filename),
            beam_size=engine.config.beam_size,
            word_timestamps=True,
        )
        found_in_clip = False
        for segment in segments:
            for word_item in getattr(segment, "words", None) or []:
                word = _normalize_whisper_word(getattr(word_item, "word", ""))
                if word and matcher.word_is_bad(word):
                    start = float(getattr(word_item, "start", 0) or 0)
                    end = float(getattr(word_item, "end", start) or start)
                    hits.append({
                        "clip_number": idx,
                        "start": start,
                        "end": end,
                        "word": word,
                        "model": tag,
                    })
                    found_in_clip = True
        if not found_in_clip:
            missed.append(idx)
        if progress:
            progress(min(0.7, 0.2 + 0.5 * (len(clips) and (idx + 1) / len(clips))),
                     f"{tag} model: scanned clip {idx + 1}/{len(clips)}")
    return hits, missed


def fuzzy_match(a: str, b: str, threshold: float = 0.8) -> bool:
    return SequenceMatcher(None, a, b).ratio() >= threshold


def merge_whisper_and_subtitles(whisper_hits, sections, fallback_enabled: bool = True
                                ) -> tuple[list[dict], list[int]]:
    """Merge whisper word hits with a full-subtitle mute fallback.

    A section is fully covered when Whisper heard every expected word (exact or
    fuzzy match); otherwise the whole subtitle section is muted instead.
    Returns (mute_segments, fallback_clip_indices) sorted by start time.
    """
    detected: dict[int, list[dict]] = defaultdict(list)
    for hit in whisper_hits:
        detected[hit["clip_number"]].append(hit)

    mute_segments: list[dict] = []
    fallback_clips: list[int] = []
    for idx, section in enumerate(sections):
        expected = set(section["words"])
        heard = [hit["word"] for hit in detected.get(idx, [])]

        matched: set[str] = set()
        for exp in expected:
            for word in heard:
                if word == exp or fuzzy_match(exp, word):
                    matched.add(exp)
                    break

        if expected and matched == expected:
            for hit in detected[idx]:
                mute_segments.append({
                    "start": hit["start"],
                    "end": hit["end"],
                    "word": hit["word"],
                    "clip_number": hit["clip_number"],
                    "model": hit["model"],
                })
        elif fallback_enabled:
            mute_segments.append({
                "start": section["start"],
                "end": section["end"],
                "word": list(section["words"]),
                "clip_number": idx,
                "fallback": True,
            })
            fallback_clips.append(idx)

    mute_segments.sort(key=lambda m: m["start"])
    return mute_segments, fallback_clips


def _fix_absolute_times(mute_segments, sections) -> None:
    for mute in mute_segments:
        if not mute.get("fallback"):
            base = sections[mute["clip_number"]]["start"]
            mute["start"] = base + mute["start"]
            mute["end"] = base + mute["end"]


def select_mutes(merged, beep_mode: str | None) -> list[dict]:
    """Pick which mutes to apply, mirroring bleeparr's beep-mode filter.

    ``words`` keeps only Whisper-refined word mutes, ``segments`` keeps only
    full-subtitle fallbacks; anything else (including the no-beep default)
    keeps every mute.
    """
    if beep_mode == "words":
        return [m for m in merged if not m.get("fallback")]
    if beep_mode == "segments":
        return [m for m in merged if m.get("fallback")]
    return list(merged)


# --------------------------------------------------------------------------
# Audio clip extraction
# --------------------------------------------------------------------------

def _run_cmd(cmd_list):
    return subprocess.run(
        cmd_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False
    )


def _tail(stderr: str | None) -> str:
    return "\n".join((stderr or "").strip().splitlines()[-20:])


def _extract_clips(video_path, sections, config, clips_dir: Path,
                   cancel: threading.Event | None = None) -> list[str]:
    """Split each bad section into a mono, 16 kHz clip under *clips_dir*."""
    times = sorted({sec["start"] for sec in sections} | {sec["end"] for sec in sections})
    segment_times = ",".join(str(t) for t in times)
    result = _run_cmd([
        config.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video_path),
        "-vn", "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000",
        "-f", "segment", "-segment_times", segment_times,
        "-segment_start_number", "1",
        str(clips_dir / "clip_%02d.wav"),
    ])
    if result.returncode != 0:
        raise CensorError(f"clip extraction failed: {_tail(result.stderr)}")
    if cancel is not None and cancel.is_set():
        return []

    wanted: list[str] = []
    for i in range(len(sections)):
        source = clips_dir / f"clip_{2 * (i + 1):02d}.wav"
        target = clips_dir / f"clip_{i + 1:02d}.wav"
        if source.exists():
            os.replace(source, target)
            wanted.append(target.name)
    for leftover in clips_dir.glob("clip_*.wav"):
        if leftover.name not in wanted:
            try:
                leftover.unlink()
            except OSError:
                pass
    if not wanted:
        raise CensorError("no subtitle segments could be extracted as audio clips")

    if config.boost_db > 0:
        for name in wanted:
            if cancel is not None and cancel.is_set():
                break
            source = clips_dir / name
            boosted = clips_dir / (name[:-4] + "_boosted.wav")
            result = _run_cmd([
                config.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source), "-filter:a", f"volume={config.boost_db}dB",
                str(boosted),
            ])
            if result.returncode == 0 and boosted.exists():
                os.replace(boosted, source)
            elif boosted.exists():
                try:
                    boosted.unlink()
                except OSError:
                    pass
    return wanted


def _run_ffmpeg(cmd_list, cancel: threading.Event | None = None):
    """Run ffmpeg, supporting graceful cancellation mid-encode."""
    process = subprocess.Popen(
        cmd_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    while process.poll() is None:
        if cancel is not None and cancel.is_set():
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return process, True
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            continue
    return process, False


# --------------------------------------------------------------------------
# In-place censored audio track
# --------------------------------------------------------------------------

_CONTAINER_FMT = {
    "matroska": "matroska",
    "webm": "matroska",
    "mov": "mp4",
    "mp4": "mp4",
    "m4a": "mp4",
    "3gp": "mp4",
}

_KNOWN_STREAM_TYPES = {"video", "audio", "subtitle", "data", "attachment"}


def _is_generated_track(stream) -> bool:
    return stream["type"] == "audio" and stream["title"].startswith(CENSOR_TRACK_TITLE)


def _probe_media(video_file, ffprobe: str) -> dict:
    result = _run_cmd([
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(video_file),
    ])
    if result.returncode != 0:
        return {"format": "matroska", "streams": []}
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        data = {}

    fmt = (data.get("format", {}).get("format_name") or "matroska").split(",")[0].strip().lower()
    streams: list[dict] = []
    per_type: dict[str, int] = {}
    for stream in data.get("streams", []):
        index = stream.get("index")
        kind = stream.get("codec_type") or "unknown"
        if index is None or kind not in _KNOWN_STREAM_TYPES:
            continue
        tags = stream.get("tags") or {}
        channels = stream.get("channels") or 0
        type_index = per_type.get(kind, 0)
        per_type[kind] = type_index + 1
        streams.append({
            "index": index,
            "type": kind,
            "type_index": type_index,
            "lang": (tags.get("language") or "und").lower(),
            "title": tags.get("title") or "",
            "channels": channels,
            "layout": stream.get("channel_layout") or f"{channels}ch",
        })
    return {"format": fmt, "streams": streams}


def _select_audio_targets(audio_streams, langs: str) -> list[dict]:
    wanted = {tag.strip().lower() for tag in langs.split(",") if tag.strip()}
    matched = [s for s in audio_streams if s["lang"] in wanted]
    if not matched:
        matched = audio_streams[:1]
    return matched


def _build_censored_filtergraph(mute_points, pre_buffer_ms, post_buffer_ms, beep,
                                src_label, out_label) -> tuple[list[str], str]:
    """Build a filtergraph that mutes (and optionally beeps over) *mute_points*."""
    base = f"{out_label}_base"
    parts = [f"[{src_label}]anull[{base}]"]
    current = base

    for i, mute in enumerate(mute_points):
        start = max(0, mute["start"] - (pre_buffer_ms / 1000.0))
        end = mute["end"] + (post_buffer_ms / 1000.0)
        nxt = f"{out_label}_m{i}"
        parts.append(
            f"[{current}]volume=enable='between(t,{start},{end})':volume=0[{nxt}]"
        )
        current = nxt

    if not beep:
        return parts, f"[{current}]"

    beep_labels = []
    for i, mute in enumerate(mute_points):
        start = max(0, mute["start"] - (pre_buffer_ms / 1000.0))
        end = mute["end"] + (post_buffer_ms / 1000.0)
        duration = round(max(0.001, end - start), 3)
        delay_ms = int(start * 1000)
        parts.append(
            f"aevalsrc=sin(2*PI*1000*t):d={duration}:s=44100[{out_label}_beep{i}]"
        )
        parts.append(
            f"[{out_label}_beep{i}]adelay={delay_ms}|{delay_ms}[{out_label}_dbeep{i}]"
        )
        beep_labels.append(f"[{out_label}_dbeep{i}]")

    all_inputs = f"[{current}]" + "".join(beep_labels)
    parts.append(
        f"{all_inputs}amix=inputs={len(beep_labels) + 1}:duration=longest[{out_label}]"
    )
    return parts, f"[{out_label}]"


def add_censored_audio_track(input_file, mute_points, config,
                             cancel: threading.Event | None = None) -> tuple[bool, str]:
    """Append one censored track per matching audio stream, in place.

    Original streams are stream-copied; only the new tracks are encoded. Any
    "Censored (Bleeparr)" track from an earlier run is dropped first, so a
    re-run rescans the pristine originals and leaves exactly one censored set.
    The result is written to a sibling temp file, then swapped in.
    """
    if not mute_points:
        return False, "no segments to censor; file untouched"

    media = _probe_media(input_file, config.ffprobe)
    streams = media["streams"]
    generated = [s for s in streams if _is_generated_track(s)]
    keep = [s for s in streams if not _is_generated_track(s)]
    audio_streams = [s for s in keep if s["type"] == "audio"]
    if not audio_streams:
        raise CensorError("no audio streams found; cannot build a censored track")

    container = _CONTAINER_FMT.get(media["format"], "matroska")
    targets = _select_audio_targets(audio_streams, config.censor_audio_langs)
    track_title = config.censor_track_title
    multi = len(targets) > 1

    parts: list[str] = []
    labels: list[str] = []
    for j, stream in enumerate(targets):
        sub_parts, label = _build_censored_filtergraph(
            mute_points,
            config.pre_buffer_ms,
            config.post_buffer_ms,
            config.beep,
            f"0:a:{stream['type_index']}",
            f"cens{j}",
        )
        parts.extend(sub_parts)
        labels.append(label)
    filter_complex = ";".join(parts)

    tmp_out = os.path.join(
        os.path.dirname(os.path.abspath(input_file)),
        f".{os.path.basename(input_file)}{_CENSOR_TMP_SUFFIX}",
    )

    cmd = [
        config.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(input_file), "-filter_complex", filter_complex,
    ]
    for stream in keep:
        cmd += ["-map", f"0:{stream['index']}"]
    cmd += ["-c:v", "copy", "-c:s", "copy", "-c:d", "copy", "-c:t", "copy"]
    n_audio = len(audio_streams)
    for i in range(n_audio):
        cmd += [f"-c:a:{i}", "copy", f"-disposition:a:{i}", "0"]
    for j, stream in enumerate(targets):
        idx = n_audio + j
        title = track_title if not multi else f"{track_title} ({stream['layout']})"
        bitrate = 384 if stream["channels"] >= 6 else 192
        cmd += [
            "-map", labels[j],
            f"-c:a:{idx}", "aac", f"-b:a:{idx}", f"{bitrate}k",
            f"-metadata:s:a:{idx}", f"title={title}",
            f"-metadata:s:a:{idx}", f"language={stream['lang']}",
            f"-disposition:a:{idx}", "default" if j == 0 else "0",
        ]
    cmd += ["-f", container, tmp_out]

    process, cancelled = _run_ffmpeg(cmd, cancel=cancel)
    if cancelled:
        try:
            os.remove(tmp_out)
        except OSError:
            pass
        raise CensorError("cancelled")
    if process.returncode != 0:
        try:
            os.remove(tmp_out)
        except OSError:
            pass
        err = (process.stderr or "").lower()
        if "no space left on device" in err:
            raise CensorError(
                "ffmpeg failed: no space left on device while writing output "
                "(the copy needs ~1x the movie size on that drive)"
            )
        raise CensorError(f"ffmpeg failed: {_tail(process.stderr)}")

    os.replace(tmp_out, str(input_file))
    message = (
        f"added {len(targets)} censored track(s) over {len(mute_points)} segment(s)"
    )
    return True, message


# --------------------------------------------------------------------------
# Job runner
# --------------------------------------------------------------------------

@dataclass
class CensorResult:
    path: str
    segments: int = 0
    model_hits: int = 0
    fallback_hits: int = 0
    skipped: bool = False
    cancelled: bool = False
    error: str | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None and not self.cancelled


def run_censor_job(engine, item, config, progress=None,
                   cancel: threading.Event | None = None) -> CensorResult:
    """Full censorship pipeline for one item. Never raises for user input."""
    shortage = downmix_mod.check_space(item.path, item.size, config.free_space_margin_gb)
    if shortage:
        return CensorResult(item.path, error=shortage)

    if progress:
        progress(0.0, "loading swear list")
    matcher = load_swears(config.swears_path)

    existing = sub_mod.existing_sidecar(item, config)
    if existing is not None and matcher.search_file(existing) is False:
        return CensorResult(item.path, skipped=True, message="no profanity found")

    if progress:
        progress(0.05, "resolving a subtitle")
    temp_dir = Path(tempfile.mkdtemp(prefix="mediafix_censor_"))
    try:
        subtitle_path = sub_mod.resolve_temp_subtitle(item, config, temp_dir)
        if subtitle_path is None:
            return CensorResult(item.path, error="no subtitles available for censorship")
        sections = bad_sections_from_file(subtitle_path, matcher)
        if not sections:
            return CensorResult(item.path, skipped=True, message="no profanity found")

        if progress:
            progress(0.1, f"extracting {len(sections)} audio clip(s)")
        _extract_clips(item.path, sections, config, temp_dir, cancel=cancel)
        if cancel is not None and cancel.is_set():
            return CensorResult(item.path, cancelled=True)

        tiers = _bleeptool_tiers(config.bleeptool)
        all_hits: list[dict] = []
        missed = list(range(len(sections)))
        has_fsm = "FSM" in dict(tiers) or any(tier == "FSM" for tier, _ in tiers)
        ran_whisper = False
        whisper_error: str | None = None
        for tier, tag in tiers:
            if tag == "subtitle":
                continue
            if not missed:
                break
            try:
                tier_hits, missed = _whisper_refine_clips(
                    engine,
                    str(temp_dir),
                    missed,
                    matcher,
                    _tier_model(tier, config.censor_models),
                    tag,
                    progress=progress,
                    cancel=cancel,
                )
                all_hits.extend(tier_hits)
                ran_whisper = True
            except Exception as exc:  # noqa: BLE001 - degrade to next tier
                whisper_error = whisper_error or f"{tag} pass failed: {type(exc).__name__}: {exc}"
                continue
        if cancel is not None and cancel.is_set():
            return CensorResult(item.path, cancelled=True)
        if not ran_whisper and not has_fsm:
            return CensorResult(
                item.path,
                error=f"whisper refinement unavailable ({whisper_error or 'no models configured'})",
            )

        if progress:
            progress(0.8, "merging whisper timestamps")
        merged, fallback = merge_whisper_and_subtitles(
            all_hits, sections, fallback_enabled=has_fsm
        )
        _fix_absolute_times(merged, sections)
        if not merged:
            return CensorResult(item.path, skipped=True, message="nothing to mute after refinement")
        selected = select_mutes(merged, config.beep_mode if config.beep else None)

        if progress:
            progress(0.9, "writing censored audio track")
        try:
            ok, message = add_censored_audio_track(item.path, selected, config, cancel=cancel)
        except CensorError as exc:
            if cancel is not None and cancel.is_set():
                return CensorResult(item.path, cancelled=True)
            return CensorResult(item.path, error=str(exc))
        model_hits = sum(1 for m in merged if not m.get("fallback"))
        return CensorResult(
            item.path,
            segments=len(selected),
            model_hits=model_hits,
            fallback_hits=len(fallback),
            message=message,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)