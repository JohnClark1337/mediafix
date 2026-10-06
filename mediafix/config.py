import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    cpu_threads: int = 4
    beam_size: int = 5
    vad_filter: bool = True
    sub_language: str = "en"
    translate: bool = False
    audio_stream: int | None = None
    sidecar_ext: str = ".srt"
    downmix_jobs: int = 4
    video_ext: tuple[str, ...] = (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm", ".ts", ".m2ts")
    subtitle_ext: tuple[str, ...] = (".srt", ".ass", ".ssa", ".vtt", ".sub")
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    mkvmerge: str = "mkvmerge"
    enhance: float = 1.5
    voice: float = 2.0
    bitrate: str = "192k"
    loudness: float = -16.0
    loudnorm: bool = True
    replace: bool = False
    remux: bool = True
    free_space_margin_gb: float = 2.0
    cache_path: str | None = None
    subtitle_search: bool = True
    subliminal_providers: tuple[str, ...] = ()
    subliminal_provider_configs: dict = field(default_factory=dict)
    swears_path: str | None = None
    censor_track_title: str = "Censored (Bleeparr)"
    censor_models: tuple[str, ...] = ("small.en", "medium.en")
    bleeptool: str = "S-M-FSM"
    censor_audio_langs: str = "eng,en,english,und"
    pre_buffer_ms: int = 100
    post_buffer_ms: int = 100
    boost_db: int = 6
    beep: bool = False
    beep_mode: str = ""

    @property
    def sidecar_language(self) -> str:
        return "eng" if self.sub_language.lower() in {"en", "eng"} else self.sub_language


def _coerce(name: str, raw):
    current = getattr(Config(), name)
    if isinstance(current, bool):
        return _as_bool(raw)
    if isinstance(current, tuple):
        if isinstance(raw, str):
            parts = [p.strip() for p in raw.replace(",", " ").split()]
            return tuple(p.lower() if p.startswith(".") else p for p in parts if p)
        return tuple(raw)
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if current is None:
        if raw in {"", "none", "null"}:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def candidate_paths(explicit=None) -> list[Path]:
    found: list[Path] = []
    if explicit:
        found.append(Path(explicit))
    env = os.environ.get("MEDIAFIX_CONFIG")
    if env:
        found.append(Path(env))
    found.append(Path("config.toml"))
    home = Path.home() / ".config" / "mediafix" / "config.toml"
    found.append(home)
    return found


def load(explicit=None, overrides=None) -> Config:
    values = {}
    for path in candidate_paths(explicit):
        if not path.is_file():
            continue
        if tomllib is None:
            raise RuntimeError("tomllib requires Python 3.11+")
        with path.open("rb") as handle:
            values = tomllib.load(handle)
        break

    known = {f.name for f in fields(Config)}
    kwargs = {}
    for name in known:
        if name in values:
            kwargs[name] = _coerce(name, values[name])
    for name in known:
        env_name = "MEDIAFIX_" + name.upper()
        if env_name in os.environ:
            kwargs[name] = _coerce(name, os.environ[env_name])
    for name, value in (overrides or {}).items():
        if value is None or name not in known:
            continue
        kwargs[name] = value if name in {"video_ext", "subtitle_ext"} else _coerce(name, value)

    config = replace(Config(), **kwargs)
    if config.free_space_margin_gb < 0:
        config = replace(config, free_space_margin_gb=0.0)
    if config.downmix_jobs < 1:
        config = replace(config, downmix_jobs=1)
    if config.beam_size < 1:
        config = replace(config, beam_size=1)
    if config.cpu_threads < 1:
        config = replace(config, cpu_threads=1)
    return config