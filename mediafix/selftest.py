import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mediafix import downmix as downmix_mod
from mediafix import srt as srt_mod

_VENDOR_ROOT = Path(__file__).resolve().parent.parent
if str(_VENDOR_ROOT) not in sys.path:
    sys.path.insert(0, str(_VENDOR_ROOT))

import audio_downmix as vendor  # noqa: E402


class CheckFailure(Exception):
    pass


def _run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _uid() -> str:
    """Current uid, or 'n/a' on platforms without POSIX uids (Windows)."""
    return str(os.getuid()) if hasattr(os, "getuid") else "n/a"


def check_tools(config) -> list[str]:
    lines = []
    for tool in (config.ffmpeg, config.ffprobe):
        found = shutil.which(tool) or (tool if os.path.isabs(tool) and os.path.exists(tool) else None)
        if not found:
            raise CheckFailure(f"{tool} not found on PATH")
        lines.append(f"  {tool}: {found}")

    version = _run([config.ffmpeg, "-version"])
    first = version.stdout.splitlines()[0] if version.stdout else "unknown"
    lines.append(f"    {first}")

    filters = _run([config.ffmpeg, "-hide_banner", "-filters"])
    if config.enhance > 0 and "dialoguenhance" not in filters.stdout:
        raise CheckFailure(
            "ffmpeg has no dialoguenhance filter (needs ffmpeg >= 6.0); "
            "rebuild the image or set enhance = 0"
        )
    lines.append("  dialoguenhance filter: present" if config.enhance > 0 else "  dialoguenhance: skipped")

    if config.remux:
        if not shutil.which(config.mkvmerge):
            raise CheckFailure("mkvmerge not found; install mkvtoolnix or set remux = false")
        lines.append(f"  mkvmerge: {shutil.which(config.mkvmerge)}")
    return lines


def check_ctranslate2() -> list[str]:
    try:
        import ctranslate2
    except ImportError as exc:
        raise CheckFailure(f"ctranslate2 unavailable: {exc}") from exc

    lines = [f"  ctranslate2 {ctranslate2.__version__} (cpu backend)"]
    supported = ctranslate2.get_supported_compute_types("cpu")
    if supported:
        lines.append(f"  cpu compute types: {', '.join(sorted(supported))}")
    return lines


def check_cache_writable() -> list[str]:
    """Report the HF cache location and prove we can write into it.

    faster-whisper downloads through huggingface_hub, which writes a token
    file next to the model cache. If HF_HOME points somewhere unwritable the
    only symptom is a bare PermissionError deep inside the library.
    """
    lines = []
    home = os.environ.get("HF_HOME") or "(unset)"
    lines.append(f"  HF_HOME={home}")
    lines.append(f"  HOME={os.environ.get('HOME') or '(unset)'}")
    lines.append(f"  uid={_uid()} cwd={os.getcwd()}")

    try:
        from huggingface_hub import constants
    except ImportError:
        lines.append("  huggingface_hub not installed yet; skipping")
        return lines

    cache = constants.HF_HOME
    token = constants.HF_TOKEN_PATH
    lines.append(f"  resolved cache={cache}")
    lines.append(f"  resolved token={token}")

    if not os.path.isabs(cache):
        raise CheckFailure(
            f"HF_HOME is relative ({cache!r}). It must be an absolute path such as "
            f"'/home/mediafix/.cache/huggingface'. A relative path resolves against "
            f"the working directory {os.getcwd()!r} and normally cannot be written."
        )

    try:
        os.makedirs(cache, exist_ok=True)
    except OSError as exc:
        raise CheckFailure(f"cannot create {cache}: {exc}") from exc

    probe = os.path.join(cache, ".write-test")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.unlink(probe)
    except OSError as exc:
        raise CheckFailure(
            f"{cache} is not writable by uid {_uid()}: {exc}\n"
            f"  fix on the host: chown -R $(id -u):$(id -g) <MEDIAFIX_HF_DIR>"
        ) from exc

    lines.append(f"  cache is writable ({cache})")
    return lines


def check_model(config) -> list[str]:
    check_cache_writable()
    from mediafix.subtitles import SubtitleEngine

    engine = SubtitleEngine(config)
    engine.load()
    return [f"  model '{config.model}' loaded on {config.device}/{config.compute_type}"]


def check_srt_roundtrip() -> list[str]:
    from mediafix.subtitles import Segment

    with tempfile.TemporaryDirectory() as tmp:
        destination = os.path.join(tmp, "clip.eng.srt")
        segments = [
            Segment(0.0, 1.5, "First line"),
            Segment(1.5, 3.0, "Second line\nwith a wrap"),
        ]
        text = srt_mod.render_srt(segments)
        expected = (
            "1\n00:00:00,000 --> 00:00:01,500\nFirst line\n\n"
            "2\n00:00:01,500 --> 00:00:03,000\nSecond line\nwith a wrap\n"
        )
        if text != expected:
            raise CheckFailure(f"srt rendering mismatch:\n{text!r}")
        srt_mod.write_atomic(destination, text)
        if not os.path.exists(destination):
            raise CheckFailure("srt sidecar was not written")
        leftovers = [n for n in os.listdir(tmp) if n != "clip.eng.srt"]
        if leftovers:
            raise CheckFailure(f"atomic write left temp files behind: {leftovers}")
    return ["  srt render + atomic write: ok"]


def check_downmix_roundtrip(config) -> list[str]:
    if config.dry_run:
        return ["  downmix roundtrip: skipped (dry-run)"]
    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "sample.mkv")
        result = _run([
            config.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=5.1",
            "-t", "2", "-c:a", "ac3", source,
        ])
        if result.returncode != 0:
            raise CheckFailure("could not generate test clip: " + result.stderr.strip()[:300])

        args = downmix_mod.build_args(config)
        _src, _base, status, message = vendor.process_one(args, source, None)
        if status != "processed":
            raise CheckFailure(f"downmix roundtrip returned '{status}': {message}")

        from mediafix.probe import probe

        info = probe(source, config.ffprobe)
        if not info.has_english_stereo:
            raise CheckFailure("downmix completed but no English stereo track was found")
        if not info.has_surround:
            raise CheckFailure("downmix dropped the original surround track")
        return ["  downmix in-place roundtrip: ok (surround + english stereo both present)"]


def run(config, model: bool = True, downmix: bool = True) -> int:
    sections = [
        ("tools", lambda: check_tools(config)),
        ("ctranslate2", lambda: check_ctranslate2()),
        ("cache", lambda: check_cache_writable()),
        ("subtitles", lambda: check_srt_roundtrip()),
    ]
    if model:
        sections.append(("model", lambda: check_model(config)))
    if downmix:
        sections.append(("downmix", lambda: check_downmix_roundtrip(config)))

    failures = 0
    for name, fn in sections:
        print(f"[{name}]")
        try:
            for line in fn():
                print(line)
        except CheckFailure as exc:
            failures += 1
            print(f"  FAIL: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  ERROR: {type(exc).__name__}: {exc}")

    if failures:
        print(f"\n{failures} check(s) failed")
        return 1
    print("\nall checks passed")
    return 0