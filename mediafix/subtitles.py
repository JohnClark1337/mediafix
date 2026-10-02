import threading
from dataclasses import dataclass

from mediafix import audio as audio_mod
from mediafix import srt as srt_mod

CHUNK_SECONDS = 600
CHUNK_OVERLAP_SECONDS = 2


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


def run_subtitle_job(engine: SubtitleEngine, item, config,
                     progress=None, cancel: threading.Event | None = None) -> SubtitleResult:
    destination = srt_mod.sidecar_path(item.path, config.sidecar_language, config.sidecar_ext)
    language = config.sub_language
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
    )