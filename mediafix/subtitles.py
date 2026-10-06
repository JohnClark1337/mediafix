import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from mediafix import audio as audio_mod
from mediafix import probe as probe_mod
from mediafix import srt as srt_mod

CHUNK_SECONDS = 600
CHUNK_OVERLAP_SECONDS = 2

_TEXT_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt"}

_region_configured = False
_region_lock = threading.Lock()


class ModelCacheError(RuntimeError):
    """The Hugging Face model cache is unusable (bad HF_HOME or permissions)."""


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class SubtitleResult:
    path: str
    segments: int
    language: str
    duration: float
    cancelled: bool = False
    error: str | None = None
    fatal: bool = False
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None and not self.cancelled


class SubtitleEngine:
    def __init__(self, config):
        self.config = config
        self._model = None
        self._lock = threading.Lock()

    def load(self, progress=None):
        with self._lock:
            if self._model is None:
                # Check the cache before importing faster_whisper, so an
                # unusable HF_HOME is reported as such instead of surfacing
                # later as a bare PermissionError from inside the library.
                self._check_cache()

                from faster_whisper import WhisperModel

                if progress:
                    progress(0.0, f"loading model {self.config.model}")
                self._model = WhisperModel(
                    self.config.model,
                    device=self.config.device,
                    compute_type=self.config.compute_type,
                    cpu_threads=self.config.cpu_threads,
                )
            return self._model

    @staticmethod
    def _check_cache() -> None:
        """Fail early with a usable message if the model cache is unusable.

        huggingface_hub derives HF_TOKEN_PATH from HF_HOME at import time and
        writes a token file there. A relative or unwritable HF_HOME otherwise
        surfaces as 'Permission denied' with no indication of which path failed.
        """
        import os

        try:
            from huggingface_hub import constants
        except ImportError:
            return  # older hub, or not installed yet; let the caller find out

        cache = constants.HF_HOME
        if not os.path.isabs(cache):
            raise ModelCacheError(
                f"HF_HOME is not an absolute path: {cache!r}. "
                f"It resolves against the working directory {os.getcwd()!r}. "
                f"Set an absolute HF_HOME, for example "
                f"/home/mediafix/.cache/huggingface."
            )
        try:
            os.makedirs(cache, exist_ok=True)
        except OSError as exc:
            raise ModelCacheError(
                f"model cache {cache} cannot be created: {exc}. "
                f"On the host, make the cache directory writable by your user: "
                f"chown -R $(id -u):$(id -g) \"$MEDIAFIX_HF_DIR\""
            ) from exc

    def transcribe_file(self, path, info, progress=None, cancel: threading.Event | None = None) -> list[Segment]:
        config = self.config
        model = self.load(progress)

        stream = audio_mod.pick_audio_stream(info, config.audio_stream)
        if progress:
            progress(0.01, f"decoding audio stream {stream}")

        def decode_progress(fraction: float) -> None:
            if progress:
                progress(fraction * 0.35, "decoding audio")

        samples = audio_mod.extract_audio(
            path, audio_stream=stream, progress=decode_progress, cancel=cancel
        )
        total_seconds = samples.shape[0] / audio_mod.SAMPLING_RATE
        if total_seconds <= 0:
            raise audio_mod.AudioError("decoded audio has zero length")

        language = None if config.sub_language.lower() in {"auto", "detect", ""} else config.sub_language
        task = "translate" if config.translate else "transcribe"

        collected: list[Segment] = []
        chunks = list(audio_mod.iter_chunks(samples, CHUNK_SECONDS, CHUNK_OVERLAP_SECONDS))
        for index, (start_sample, _end_sample, chunk) in enumerate(chunks):
            if cancel is not None and cancel.is_set():
                break
            offset = start_sample / audio_mod.SAMPLING_RATE
            base = 0.35 + 0.64 * (index / len(chunks))
            span = 0.64 / len(chunks)

            def chunk_progress(fraction: float, _offset=offset, _base=base, _span=span) -> None:
                if progress:
                    progress(_base + fraction * _span, "transcribing")

            segments, _info = model.transcribe(
                chunk,
                language=language,
                task=task,
                beam_size=config.beam_size,
                vad_filter=config.vad_filter,
            )
            for segment in segments:
                if cancel is not None and cancel.is_set():
                    break
                absolute_start = segment.start + offset
                absolute_end = segment.end + offset
                if absolute_end <= offset + 0.01 and index > 0:
                    continue
                text = (segment.text or "").strip()
                if not text:
                    continue
                collected.append(Segment(start=absolute_start, end=absolute_end, text=text))

        if cancel is not None and cancel.is_set():
            return []
        return collected


# --------------------------------------------------------------------------
# Subtitle search: existing sidecar -> embedded -> online
# --------------------------------------------------------------------------

def existing_sidecar(item, config) -> Path | None:
    """Return a non-empty English sidecar for this item, if one exists."""
    destination = srt_mod.sidecar_path(item.path, config.sidecar_language, config.sidecar_ext)
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    for sidecar in item.sidecars:
        if sidecar.is_english and not sidecar.is_forced:
            candidate = Path(sidecar.path)
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
    return None


def extract_embedded_subtitle(video_path, config, out_path) -> tuple[bool, str]:
    """Extract the best text subtitle stream as SRT. Returns (ok, language)."""
    result = subprocess.run(
        [config.ffprobe, "-v", "error", "-print_format", "json", "-show_streams",
         "-select_streams", "s", str(video_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    if result.returncode != 0:
        return False, ""
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return False, ""

    candidates = []
    for stream in data.get("streams", []):
        codec = (stream.get("codec_name") or "").lower()
        if codec not in _TEXT_CODECS:
            continue
        tags = stream.get("tags") or {}
        lang = (tags.get("language") or "").lower()
        score = 2 if lang == config.sidecar_language.lower() else 1
        candidates.append({"index": stream.get("index"), "lang": lang or "und", "score": score})
    if not candidates:
        return False, ""
    candidates.sort(key=lambda c: (-c["score"], c["index"]))
    pick = candidates[0]

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    extract = subprocess.run(
        [config.ffmpeg, "-y", "-i", str(video_path), "-map", f"0:{pick['index']}",
         "-c:s", "srt", str(out_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    if extract.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        try:
            if out_path.exists():
                out_path.unlink()
        except OSError:
            pass
        return False, ""
    return True, pick["lang"]


def download_subtitle_text(video_file, config) -> str | None:
    """Download a subtitle with subliminal, returning its text or None.

    ``config.subtitle_search`` gates the online lookup entirely. Provider
    keys come from ``subliminal_provider_configs`` (e.g. OpenSubtitles API
    config keys) so keyless providers still work out of the box.
    """
    if not config.subtitle_search:
        return None
    try:
        from subliminal import Video, download_best_subtitles, region
        from babelfish import Language
    except ImportError:
        return None

    global _region_configured
    with _region_lock:
        if not _region_configured:
            try:
                region.configure("dogpile.cache.memory")
            except Exception:  # noqa: BLE001 - cache is optional
                pass
            _region_configured = True

    language = config.sidecar_language or "eng"
    providers = config.subliminal_providers or None
    provider_configs = dict(config.subliminal_provider_configs or {})
    video = Video.fromname(str(video_file))

    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            found = download_best_subtitles(
                [video],
                {Language(language)},
                providers=providers,
                provider_configs=provider_configs,
            )
        except Exception as exc:  # noqa: BLE001 - provider outages are common
            last_error = exc
            continue
        entries = found.get(video) if found else None
        if not entries:
            continue
        content = entries[0].content
        if isinstance(content, bytes):
            for encoding in ("utf-8-sig", "utf-8", "latin1"):
                try:
                    content = content.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
        if content:
            return str(content).lstrip("\ufeff")
    return None


def _cue_count(path) -> int:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    return len(re.findall(r"-->\s*\d", text))


def search_and_save_sidecar(item, config, destination) -> Path | None:
    """Search for subtitles and persist them as the sidecar.

    Resolution: existing sidecar, then an embedded English (or untagged) text
    stream, then an online download. Returns the destination path, or None
    when nothing could be found.
    """
    existing = existing_sidecar(item, config)
    if existing is not None:
        return existing

    temp_dir = Path(tempfile.mkdtemp(prefix="mediafix_search_"))
    try:
        embedded = temp_dir / f"{Path(item.path).stem}.embedded.srt"
        ok, lang = extract_embedded_subtitle(item.path, config, embedded)
        if ok and (not lang or lang in {"und"} or probe_mod.is_english(lang)):
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            os.replace(embedded, destination)
            return Path(destination)
        if config.subtitle_search:
            text = download_subtitle_text(item.path, config)
            if text is not None:
                srt_mod.write_atomic(destination, text)
                return Path(destination)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return None


def resolve_temp_subtitle(item, config, temp_dir) -> Path | None:
    """Resolve a subtitle for one-off use (censorship), never persisted.

    Reuses an existing sidecar when present; otherwise extracts an embedded
    text stream or downloads one into *temp_dir*.
    """
    existing = existing_sidecar(item, config)
    if existing is not None:
        return existing

    out = Path(temp_dir) / f"{Path(item.path).stem}.resolved.srt"
    ok, _lang = extract_embedded_subtitle(item.path, config, out)
    if ok:
        return out
    if not config.subtitle_search:
        return None
    text = download_subtitle_text(item.path, config)
    if text is None:
        return None
    out.write_text(text, encoding="utf-8")
    return out


def run_subtitle_job(engine: SubtitleEngine, item, config,
                     progress=None, cancel: threading.Event | None = None) -> SubtitleResult:
    destination = srt_mod.sidecar_path(item.path, config.sidecar_language, config.sidecar_ext)
    language = config.sub_language

    existing = existing_sidecar(item, config)
    if existing is not None:
        if progress:
            progress(1.0, f"using existing subtitles: {existing.name}")
        return SubtitleResult(
            str(destination), _cue_count(existing), language, 0.0,
            message=f"found existing subtitles: {existing.name}",
        )

    if config.subtitle_search:
        if progress:
            progress(0.0, "searching for subtitles")
        found = search_and_save_sidecar(item, config, destination)
        if found is not None:
            if progress:
                progress(1.0, f"subtitles found: {found.name}")
            return SubtitleResult(
                str(destination), _cue_count(found), language, 0.0,
                message=f"subtitles saved: {found.name}",
            )

    try:
        engine.load()
    except Exception as exc:  # noqa: BLE001
        return SubtitleResult(
            str(destination), 0, language, 0.0,
            error=f"model load failed: {type(exc).__name__}: {exc}", fatal=True,
        )

    segments: list[Segment] = []
    cancelled = False
    error = None
    try:
        segments = engine.transcribe_file(item.path, item, progress=progress, cancel=cancel)
        cancelled = cancel is not None and cancel.is_set()
    except Exception as exc:  # noqa: BLE001 - report, never crash the batch
        error = f"{type(exc).__name__}: {exc}"

    if error:
        return SubtitleResult(str(destination), 0, language, 0.0, error=error)
    if cancelled:
        return SubtitleResult(str(destination), 0, language, 0.0, cancelled=True)
    if not segments:
        return SubtitleResult(
            str(destination), 0, language, 0.0,
            error="no speech detected; sidecar not written",
        )

    text = srt_mod.render_srt(segments)
    try:
        srt_mod.write_atomic(destination, text)
    except OSError as exc:
        return SubtitleResult(str(destination), 0, language, 0.0, error=f"write failed: {exc}")

    if progress:
        progress(1.0, f"wrote {destination}")
    return SubtitleResult(
        str(destination), len(segments), language,
        segments[-1].end if segments else 0.0,
        message=f"{len(segments)} cues transcribed",
    )