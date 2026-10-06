import os
import sys
import threading
from pathlib import Path

_VENDOR_ROOT = Path(__file__).resolve().parent.parent
if str(_VENDOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_VENDOR_ROOT))

import audio_downmix as vendor  # noqa: E402

ToolError = vendor.ToolError


def needs_downmix(info) -> bool:
    return info.needs_audio


def build_args(config, force: bool = False, dry_run: bool = False):
    argv = ["--in-place", "--no-progress"]
    argv += ["--ffmpeg", config.ffmpeg, "--ffprobe", config.ffprobe, "--mkvmerge", config.mkvmerge]
    argv += ["--enhance", str(config.enhance), "--voice", str(config.voice), "--bitrate", config.bitrate]
    argv += ["--loudness", str(config.loudness)]
    if not config.loudnorm:
        argv.append("--no-loudnorm")
    if config.replace:
        argv.append("--replace")
    if not config.remux:
        argv.append("--no-remux")
    if force:
        argv.append("--force")
    if dry_run:
        argv.append("--dry-run")
    argv.append("__placeholder__")
    args = vendor.make_parser().parse_args(argv)
    # The vendor only derives these inside main(); process_one() reads them off args.
    args.remux = not args.no_remux
    args.censor_prefix = config.censor_track_title
    return args


def preflight(config, force: bool = False) -> list[str]:
    warnings: list[str] = []
    args = build_args(config, force=force)
    vendor.check_tools(config.ffmpeg, config.ffprobe, require_mkvmerge=config.remux)
    vendor.check_filter_support(args)
    if config.remux:
        warnings.append("matroska output will be re-muxed with mkvmerge")
    if config.replace:
        warnings.append("--replace drops the original surround track(s)")
    if config.enhance <= 0:
        warnings.append("dialogue enhancement is disabled; plain downmix only")
    return warnings


def free_space_bytes(path) -> int:
    try:
        usage = os.statvfs(path)
        return usage.f_bavail * usage.f_frsize
    except (OSError, AttributeError):
        return 0


def check_space(path, size: int, margin_gb: float) -> str | None:
    available = free_space_bytes(path)
    if not available:
        return None
    needed = size + int(margin_gb * 1024 ** 3)
    if available < needed:
        return (
            f"insufficient free space: need {needed / 1024 ** 3:.1f} GB "
            f"(file {size / 1024 ** 3:.1f} GB + margin), have {available / 1024 ** 3:.1f} GB"
        )
    return None


class TempSizeMonitor:
    """Watches the vendor's temp file growth to report downmix progress."""

    def __init__(self, path, interval: float = 1.5):
        self.path = str(path)
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._size = 0
        self.stem = os.path.splitext(os.path.basename(self.path))[0]
        self.ext = os.path.splitext(self.path)[1]
        self.directory = os.path.dirname(self.path) or "."

    def _largest(self) -> int:
        try:
            entries = os.listdir(self.directory)
        except OSError:
            return 0
        total = 0
        for name in entries:
            if not name.startswith(f".{self.stem}."):
                continue
            if not (name.endswith(self.ext) or ".remux" in name):
                continue
            try:
                total = max(total, os.path.getsize(os.path.join(self.directory, name)))
            except OSError:
                continue
        return total

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._size = self._largest()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def bytes_written(self) -> int:
        return max(self._size, self._largest())


def run_one(args, path, cancel: threading.Event | None = None,
            progress=None, total_bytes: int | None = None) -> tuple[str, str]:
    target = str(path)
    monitor = TempSizeMonitor(target)
    monitor.start()

    stop = threading.Event()
    sampler: threading.Thread | None = None
    if progress and total_bytes:
        def sample() -> None:
            while not stop.wait(1.5):
                written = monitor.bytes_written
                if written and total_bytes > 0:
                    progress(min(0.95, written / total_bytes), f"{written / 1024 ** 2:.0f} MB")
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()

    try:
        _, _, status, message = vendor.process_one(args, target, None)
    except Exception as exc:  # noqa: BLE001 - vendor raises varied types
        status, message = vendor.STATUS_FAILED, f"{type(exc).__name__}: {exc}"
    finally:
        stop.set()
        monitor.stop()
        if sampler is not None:
            sampler.join(timeout=2)
    return status, message