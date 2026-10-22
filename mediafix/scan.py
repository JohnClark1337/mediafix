import fnmatch
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from mediafix import probe as probe_mod
from mediafix.probe import MediaInfo

SKIP_DIR_NAMES = {
    "@eadir", ".trash", ".trashes", "$recycle.bin",
    "system volume information", "nightmix_output", ".mediafix",
}

FILTER_ALL = "all"
FILTER_SUB = "sub"
FILTER_AUDIO = "audio"
FILTER_BOTH = "both"
FILTER_CENSOR = "censor"
FILTER_CLEAN = "clean"
FILTERS = (FILTER_ALL, FILTER_SUB, FILTER_AUDIO, FILTER_BOTH, FILTER_CENSOR, FILTER_CLEAN)


@dataclass(frozen=True)
class SidecarInfo:
    path: str
    language: str | None
    is_english: bool
    is_forced: bool


@dataclass
class MediaItem:
    path: str
    size: int = 0
    duration: float | None = None
    audio: list = field(default_factory=list)
    subtitles: list = field(default_factory=list)
    sidecars: list = field(default_factory=list)
    needs_subtitle: bool = False
    needs_audio: bool = False
    needs_censor: bool = False
    selectable: bool = True
    selected: bool = False
    want_subtitle: bool = False
    want_audio: bool = False
    want_censor: bool = False
    error: str | None = None

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def directory(self) -> str:
        return os.path.dirname(self.path)

    @property
    def needs_any(self) -> bool:
        return self.needs_subtitle or self.needs_audio or self.needs_censor

    @property
    def actionable(self) -> bool:
        return self.selectable and (self.want_subtitle or self.want_audio or self.want_censor)


@dataclass
class ScanResult:
    roots: list[str]
    items: list[MediaItem] = field(default_factory=list)
    cached: int = 0
    probed: int = 0
    duration_scanned: float = 0.0
    total_bytes: int = 0

    @property
    def needs_subtitle_count(self) -> int:
        return sum(1 for i in self.items if i.needs_subtitle)

    @property
    def needs_audio_count(self) -> int:
        return sum(1 for i in self.items if i.needs_audio)

    @property
    def needs_censor_count(self) -> int:
        return sum(1 for i in self.items if i.needs_censor)

    @property
    def both_count(self) -> int:
        return sum(1 for i in self.items if i.needs_subtitle and i.needs_audio)

    @property
    def clean_count(self) -> int:
        return sum(1 for i in self.items if not i.needs_any)

    @property
    def failed_count(self) -> int:
        return sum(1 for i in self.items if i.error)


def _is_skipped_dir(name: str) -> bool:
    lowered = name.lower()
    return name.startswith(".") or lowered in SKIP_DIR_NAMES or lowered.startswith(".trash")


def walk(roots, extensions) -> list[str]:
    ext_set = {e.lower() if e.startswith(".") else "." + e.lower() for e in extensions}
    found: list[str] = []
    seen: set[str] = set()

    def handle_file(path: str) -> None:
        if os.path.splitext(path)[1].lower() not in ext_set:
            return
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            found.append(os.path.abspath(path))

    for raw in roots:
        root = os.path.abspath(os.path.expanduser(raw))
        if os.path.isfile(root):
            handle_file(root)
            continue
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not _is_skipped_dir(d))
            for name in sorted(files):
                handle_file(os.path.join(current, name))
    found.sort(key=lambda p: p.lower())
    return found


def list_directory(directory: str) -> list[str]:
    """Names of the files in *directory* (empty list when it cannot be read)."""
    try:
        with os.scandir(directory) as entries:
            return sorted(entry.name for entry in entries if entry.is_file())
    except OSError:
        return []


def sidecars_for(video_path, names, extensions) -> list[SidecarInfo]:
    """Sidecars of *video_path* given the file names of its directory.

    The names are passed in so a directory is listed once per scan instead of
    once per video in it.
    """
    video = Path(video_path)
    parent = video.parent
    stem = video.stem
    results: list[SidecarInfo] = []
    for name in names:
        entry = parent / name
        if entry.suffix.lower() not in extensions:
            continue
        if entry.stem == stem:
            results.append(SidecarInfo(str(entry), None, True, False))
            continue
        prefix = stem + "."
        if not entry.stem.startswith(prefix):
            continue
        tag = entry.stem[len(prefix):]
        if not tag:
            continue
        pieces = [p for p in tag.replace("_", "-").split(".") if p]
        if not pieces:
            continue
        language = pieces[0]
        results.append(SidecarInfo(
            str(entry), language,
            probe_mod.is_english(language),
            any(p.lower() in {"forced", "sdh"} for p in pieces),
        ))
    return results


def find_sidecars(video_path, extensions) -> list[SidecarInfo]:
    parent = Path(video_path).parent
    return sidecars_for(video_path, list_directory(str(parent)), extensions)


class DirectoryIndex:
    """One directory listing per directory, shared by every file in it."""

    def __init__(self):
        self._names: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def names(self, directory: str) -> list[str]:
        with self._lock:
            cached = self._names.get(directory)
        if cached is not None:
            return cached
        listing = list_directory(directory)
        with self._lock:
            return self._names.setdefault(directory, listing)


def classify(path, info: MediaInfo, sidecars, sub_language: str,
             swears=None, censor_track_title: str = probe_mod.CENSOR_TRACK_TITLE) -> MediaItem:
    size = info.size
    if not size:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
    has_english_sidecar = any(s.is_english and not s.is_forced for s in sidecars)
    # The generated-track prefix is a config value, so the probe result has to
    # carry it before needs_audio is derived from it.
    info = replace(info, censor_prefix=censor_track_title)
    item = MediaItem(
        path=str(path),
        size=size,
        duration=info.duration,
        audio=info.audio,
        subtitles=info.subtitles,
        sidecars=list(sidecars),
    )
    if info.error:
        item.error = info.error
        item.selectable = False
        return item
    if not info.audio:
        item.selectable = False
        item.error = "no audio track"
        return item
    item.needs_subtitle = not info.has_english_subtitle and not has_english_sidecar
    item.needs_audio = info.needs_audio
    if swears is None:
        item.needs_censor = False
    elif info.has_censored_track(censor_track_title):
        item.needs_censor = False
    elif has_english_sidecar:
        sidecar = next((s.path for s in sidecars if s.is_english and not s.is_forced), None)
        sniff = swears.search_file(sidecar) if sidecar else None
        item.needs_censor = sniff is not False
    else:
        item.needs_censor = True
    item.selectable = item.needs_any
    item.want_subtitle = item.needs_subtitle
    item.want_audio = item.needs_audio
    item.want_censor = item.needs_censor
    return item


def _encode(info: MediaInfo) -> dict:
    """Cache payload for a probed file.

    Only the ffprobe result is cached. The sidecar list is not: it lives next
    to the video, so writing "Movie.eng.srt" does not change the video's size
    or mtime and a cached entry would keep reporting the file as missing
    subtitles.
    """
    return {
        "size": info.size,
        "duration": info.duration,
        "error": info.error,
        "audio": [asdict(a) for a in info.audio],
        "subtitles": [asdict(s) for s in info.subtitles],
    }


def _decode(data: dict) -> MediaInfo:
    audio = [probe_mod.AudioStreamInfo(**a) for a in data.get("audio", [])]
    subs = [probe_mod.SubtitleStreamInfo(**s) for s in data.get("subtitles", [])]
    return MediaInfo(
        path=data.get("path", ""),
        size=int(data.get("size") or 0),
        duration=data.get("duration"),
        audio=audio,
        subtitles=subs,
        error=data.get("error"),
    )


class ScanCache:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.entries: dict[str, dict] = {}
        self.hits = 0
        if self.path and self.path.is_file():
            try:
                self.entries = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.entries = {}

    def get(self, path, size, mtime):
        entry = self.entries.get(path)
        if not entry:
            return None
        if int(entry.get("size") or -1) != int(size) or float(entry.get("mtime") or -1) != float(mtime):
            return None
        self.hits += 1
        return entry

    def put(self, path, size, mtime, payload: dict) -> None:
        self.entries[path] = {"size": int(size), "mtime": float(mtime), **payload}

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.entries), encoding="utf-8")
        os.replace(tmp, self.path)


def scan(roots, config, jobs: int = 8, progress=None, use_cache: bool = True) -> ScanResult:
    from mediafix import censor as censor_mod

    started = time.monotonic()
    paths = walk(roots, config.video_ext)
    result = ScanResult(roots=[str(r) for r in roots])
    cache = ScanCache(config.cache_path) if use_cache else None
    index = DirectoryIndex()
    swear_matcher = censor_mod.load_swears(config.swears_path)

    def work(path: str) -> MediaItem:
        try:
            stat = os.stat(path)
            size, mtime = stat.st_size, stat.st_mtime
        except OSError as exc:
            return MediaItem(path=path, error=f"stat failed: {exc}", selectable=False)

        entry = cache.get(path, size, mtime) if cache else None
        if entry:
            info = _decode({**entry, "path": path})
        else:
            info = probe_mod.probe(path, config.ffprobe)
            if cache:
                cache.put(path, size, mtime, _encode(info))
        # Sidecars are always re-read: they are separate files and can appear
        # or change without the video being touched.
        sidecars = sidecars_for(
            path,
            index.names(os.path.dirname(path) or "."),
            config.subtitle_ext,
        )
        return classify(
            path, info, sidecars, config.sub_language,
            swears=swear_matcher,
            censor_track_title=config.censor_track_title,
        )

    if paths:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            done = 0
            for item in pool.map(work, paths):
                result.items.append(item)
                done += 1
                if progress:
                    progress(done, len(paths))

    if cache:
        cache.save()
        result.cached = cache.hits

    result.duration_scanned = time.monotonic() - started
    result.total_bytes = sum(i.size for i in result.items)
    return result


def filter_items(items, mode: str) -> list[MediaItem]:
    if mode == FILTER_SUB:
        return [i for i in items if i.needs_subtitle]
    if mode == FILTER_AUDIO:
        return [i for i in items if i.needs_audio]
    if mode == FILTER_BOTH:
        return [i for i in items if i.needs_subtitle and i.needs_audio]
    if mode == FILTER_CENSOR:
        return [i for i in items if i.needs_censor]
    if mode == FILTER_CLEAN:
        return [i for i in items if not i.needs_any]
    return list(items)


def match_pattern(item: MediaItem, pattern: str) -> bool:
    """Whether *item* matches a user-supplied wildcard pattern.

    The pattern uses fnmatch syntax (*, ?, [seq]) and is matched against both
    the file name and the full path, case-insensitively on every platform. A
    pattern with no wildcard characters is a substring search, so typing
    ``star`` finds ``Star.Wars.mkv`` without the user having to add stars.
    """
    pattern = (pattern or "").strip()
    if not pattern:
        return True
    pattern = pattern.lower().replace("\\", "/")
    if not any(char in pattern for char in "*?["):
        pattern = f"*{pattern}*"
    path = item.path.replace("\\", "/").lower()
    return fnmatch.fnmatchcase(item.name.lower(), pattern) or fnmatch.fnmatchcase(path, pattern)


def filter_by_pattern(items, pattern: str) -> list[MediaItem]:
    """Filter *items* by :func:`match_pattern`; a blank pattern keeps everything."""
    return [item for item in items if match_pattern(item, pattern)]


def human_size(num: int) -> str:
    step = 1024.0
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < step or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TB"


def human_duration(seconds) -> str:
    if not seconds:
        return "--:--"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"