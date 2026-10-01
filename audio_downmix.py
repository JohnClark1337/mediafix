#!/usr/bin/env python3
"""audio_downmix.py - Add nightmode stereo audio tracks to surround-sound videos.

For every video input file whose audio uses a 5.1 / 7.1 (or other multichannel)
surround layout, this tool:

  1. Applies the ffmpeg `dialoguenhance` filter ("nightmode") to lift dialogue
     relative to the rest of the mix.
  2. Downmixes the surround channels to stereo using the `pan` filter (centre
     and surround channels folded in with 0.707 coefficients).
  3. Applies `loudnorm` loudness normalization (default target -16 LUFS) so the
     new stereo track sits at a consistent loudness; override with --loudness,
     disable with --no-loudnorm.
  4. Encodes the result as a new stereo audio track (AAC / Opus) and muxes it
     alongside the original tracks.

Video, subtitles and the original audio are stream-copied, so the job is fast
and lossless for everything except the new stereo track.

The tool accepts individual files or directories (searched recursively unless
--no-recursive is given), so a whole media library can be queued in a single
run. While processing, a live progress bar shows overall completion and the
file currently being worked on (disabled with --no-progress). In sequential
recursive runs, --pause asks before continuing past each completed directory
(interactive TTY only).

Examples
--------
  # Keep originals, add stereo tracks, mirror the tree into ./nightmix_output
  python3 audio_downmix.py /media/movies

  # Same, but overwrite the originals in place
  python3 audio_downmix.py /media/movies --in-place

  # Drop the surround track and replace it with the stereo one
  python3 audio_downmix.py /media/movies --replace

  # Preview everything the tool would do without touching files
  python3 audio_downmix.py /media/movies --dry-run

   # Process 4 files at once and log to a file
   python3 audio_downmix.py /media/movies --jobs 4 --log downmix.log

After ffmpeg muxes the new track, matroska output is passed through
`mkvmerge` (if available) so clusters/cues are written the way VLC's reader
expects. Without this, long seeks on DVD-era MPEG-2 files can freeze in VLC.
Disable with --no-remux.

The new stereo track is placed first and marked as the default audio track,
and (unless --no-loudnorm) it goes through loudnorm normalization targeted at
--loudness LUFS. In serial recursive runs, --pause makes the tool ask before
moving on to each new directory so you can bail out of a long queue.
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.version_info < (3, 5):
    sys.stderr.write(
        "audio_downmix requires Python 3.5 or newer "
        "(found %d.%d). Try running with 'python3'.\n"
        % (sys.version_info[0], sys.version_info[1])
    )
    sys.exit(2)

log = logging.getLogger("audio_downmix")

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm"}

DEFAULT_OUTPUT_DIR = "./nightmix_output"

STATUS_PROCESSED = "processed"
STATUS_SKIP_STEREO = "skip-has-stereo"
STATUS_NO_AUDIO = "skip-no-audio"
STATUS_UNCHANGED = "unchanged"
STATUS_FAILED = "failed"

PAN_COEFFICIENTS = {
    "FL": (1.0, 0.0),
    "FR": (0.0, 1.0),
    "FC": (0.7071, 0.7071),
    "BC": (0.7071, 0.7071),
    "BL": (0.7071, 0.0),
    "BR": (0.0, 0.7071),
    "SL": (0.7071, 0.0),
    "SR": (0.0, 0.7071),
    "FLc": (0.7071, 0.0),
    "FRc": (0.0, 0.7071),
    "LFE": (0.0, 0.0),
}

LAYOUT_CHANNELS = {
    "5.1": ["FL", "FR", "FC", "LFE", "BL", "BR"],
    "5.0": ["FL", "FR", "FC", "BL", "BR"],
    "5.1(side)": ["FL", "FR", "FC", "LFE", "SL", "SR"],
    "5.0(side)": ["FL", "FR", "FC", "SL", "SR"],
    "6.1": ["FL", "FR", "FC", "LFE", "BC", "SL", "SR"],
    "6.0": ["FL", "FR", "FC", "BC", "SL", "SR"],
    "7.1": ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"],
    "7.0": ["FL", "FR", "FC", "BL", "BR", "SL", "SR"],
    "7.1(wide)": ["FL", "FR", "FC", "LFE", "BL", "BR", "FLc", "FRc"],
    "7.0(wide)": ["FL", "FR", "FC", "BL", "BR", "FLc", "FRc"],
}

FALLBACK_CHANNELS = {
    1: ["FC"],
    2: ["FL", "FR"],
    3: ["FL", "FR", "FC"],
    6: ["FL", "FR", "FC", "LFE", "BL", "BR"],
    7: ["FL", "FR", "FC", "LFE", "BL", "BR", "SL"],
    8: ["FL", "FR", "FC", "LFE", "BL", "BR", "SL", "SR"],
}


class ToolError(Exception):
    pass


class AudioStream(object):
    def __init__(self, pos, codec, channels, layout, language=""):
        self.pos = pos
        self.codec = codec
        self.channels = channels
        self.layout = layout
        self.language = language


class VideoFile(object):
    def __init__(self, path, base, audio, subtitle_codecs):
        self.path = path
        self.base = base
        self.audio = audio
        self.subtitle_codecs = subtitle_codecs


def shutil_which(name):
    import shutil
    return shutil.which(name)


def check_tools(ffmpeg, ffprobe, require_mkvmerge=False):
    for tool in (ffmpeg, ffprobe):
        if not shutil_which(tool):
            raise ToolError("Required tool not found on PATH: %s" % tool)
    if require_mkvmerge and not shutil_which("mkvmerge"):
        raise ToolError(
            "mkvmerge not found on PATH. Install MKVToolNix so matroska output can be "
            "re-muxed into VLC-friendly clusters, or pass --no-remux to skip this step."
        )


def has_filter(ffmpeg, name):
    proc = run_capture([ffmpeg, "-hide_banner", "-filters"])
    if proc.returncode != 0:
        return False
    line = " %s " % name
    for row in proc.stdout.splitlines():
        if line in " %s " % row:
            return True
    return False


def check_filter_support(args):
    if args.enhance <= 0:
        return
    if not has_filter(args.ffmpeg, "dialoguenhance"):
        raise ToolError(
            "This ffmpeg (%s) has no 'dialoguenhance' filter. That filter requires "
            "ffmpeg 6.0 or newer; Ubuntu/Debian apt often ships an older build. "
            "Install a modern ffmpeg (e.g. the static build from "
            "johnvansickle.com), or run with --enhance 0 for a plain downmix."
            % args.ffmpeg
        )


def scan_for_files(paths, extensions, recursive=True):
    ext_set = {e.lower() if e.startswith(".") else "." + e.lower() for e in extensions}
    found = []
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isfile(p):
            if os.path.splitext(p)[1].lower() in ext_set:
                found.append((p, None))
            else:
                log.warning("Ignoring non-video file: %s", p)
        elif os.path.isdir(p):
            if recursive:
                walker = os.walk(p)
            else:
                walker = iter([next(os.walk(p), (p, [], []))])
            for root, _dirs, files in walker:
                for name in files:
                    if os.path.splitext(name)[1].lower() in ext_set:
                        found.append((os.path.join(root, name), p))
        else:
            log.error("Path not found: %s", p)
    found.sort(key=lambda x: x[0].lower())
    return found


def run_capture(cmd):
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    return proc


def probe_file(probe_path, file_path):
    cmd = [probe_path, "-v", "error", "-print_format", "json",
           "-show_streams", file_path]
    proc = run_capture(cmd)
    if proc.returncode != 0:
        raise RuntimeError(
            "ffprobe failed: %s" % (proc.stderr.strip() or proc.stdout.strip())
        )
    return json.loads(proc.stdout)


def analyze_file(file_path, probe_path):
    data = probe_file(probe_path, file_path)
    streams = data.get("streams", [])
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    audio = []
    for i, s in enumerate(audio_streams):
        try:
            channels = int(s.get("channels") or 0)
        except (TypeError, ValueError):
            channels = 0
        audio.append(AudioStream(
            pos=i,
            codec=str(s.get("codec_name") or ""),
            channels=channels,
            layout=str(s.get("channel_layout") or "").strip(),
            language=str((s.get("tags") or {}).get("language") or "").strip(),
        ))
    subtitle_codecs = [str(s.get("codec_name") or "") for s in subtitle_streams]
    return VideoFile(file_path, None, audio, subtitle_codecs)


def channel_names(stream):
    layout = stream.layout.lower()
    if layout and "+" in layout:
        return [c.strip() for c in stream.layout.split("+") if c.strip()]
    if layout in LAYOUT_CHANNELS:
        return LAYOUT_CHANNELS[layout]
    if stream.channels in FALLBACK_CHANNELS:
        return FALLBACK_CHANNELS[stream.channels]
    return ["c%d" % i for i in range(stream.channels)]


def needs_downmix(stream):
    return stream.channels >= 6


def is_english(language):
    if not language:
        return False
    lang = language.strip().lower().replace("_", "-")
    return lang in ("eng", "en", "english") or lang.split("-")[0] == "en"


def has_english_stereo(vf):
    return any(s.channels == 2 and is_english(s.language) for s in vf.audio)


def format_coef(value):
    return "{:.4f}".format(value).rstrip("0").rstrip(".")


def build_pan(stream):
    channels = channel_names(stream)
    left = []
    right = []
    for ch in channels:
        lc, rc = PAN_COEFFICIENTS.get(ch, (0.5, 0.5))
        if lc:
            left.append(ch if lc == 1.0 else "{}*{}".format(format_coef(lc), ch))
        if rc:
            right.append(ch if rc == 1.0 else "{}*{}".format(format_coef(rc), ch))
    c0 = "+".join(left) if left else "0*FL"
    c1 = "+".join(right) if right else "0*FL"
    return "stereo|c0={}|c1={}".format(c0, c1)


def build_command(ffmpeg, src, dst, vf, enhance, voice, bitrate, replace, loudness=None):
    ext = os.path.splitext(dst)[1].lower()
    targets = [s for s in vf.audio if needs_downmix(s)]

    fc_parts = []
    labels = []
    for k, s in enumerate(targets):
        if enhance > 0:
            chain = "dialoguenhance=enhance={:g}:voice={:g},pan={}".format(
                enhance, voice, build_pan(s))
        else:
            chain = "pan={}".format(build_pan(s))
        if loudness is not None:
            chain += ",loudnorm=I={:g}:TP=-1.5:LRA=11,aresample=48000".format(loudness)
        fc_parts.append("[0:a:{}]{}[d{}]".format(s.pos, chain, k))
        labels.append("[d{}]".format(k))

    filter_complex = ";".join(fc_parts)

    stereo_codec = "libopus" if ext == ".webm" else "aac"
    stereo_bitrate = "160k" if ext == ".webm" else bitrate

    n_stereo = len(labels)

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", src]
    cmd += ["-map", "0:v"]

    # New stereo tracks are mapped BEFORE the originals so they become stream
    # indices 0..n-1, i.e. the first (and -> default) audio tracks in the output.
    stereo_labels = list(labels)
    for k in range(n_stereo):
        cmd += ["-map", labels[k]]

    original_map = []
    if replace:
        copies = [s for s in vf.audio if not needs_downmix(s)]
        for s in copies:
            cmd += ["-map", "0:a:%d" % s.pos]
            original_map.append(s.pos)
    else:
        cmd += ["-map", "0:a"]
        original_map = list(range(len(vf.audio)))

    if filter_complex:
        cmd += ["-filter_complex", filter_complex]

    cmd += ["-c:v", "copy"]

    for k in range(n_stereo):
        cmd += [
            "-c:a:%d" % k, stereo_codec,
            "-b:a:%d" % k, stereo_bitrate,
            "-ac:a:%d" % k, "2",
            "-metadata:s:a:%d" % k, "title=Nightmix Stereo",
            "-metadata:s:a:%d" % k, "language=eng",
        ]

    for idx, _a in enumerate(original_map):
        cmd += ["-c:a:%d" % (n_stereo + idx), "copy"]

    # Explicitly make the first (new stereo) track the default audio selected by
    # players/Plex instead of leaving it up to container heuristics.
    if n_stereo:
        cmd += ["-disposition:a:0", "default"]

    if vf.subtitle_codecs and ext == ".mkv":
        cmd += ["-map", "0:s", "-c:s", "copy"]
    elif vf.subtitle_codecs and ext in (".mp4", ".m4v", ".mov"):
        for i, codec in enumerate(vf.subtitle_codecs):
            if codec in ("mov_text", "text"):
                cmd += ["-map", "0:s:%d" % i]
        if any(c in ("mov_text", "text") for c in vf.subtitle_codecs):
            cmd += ["-c:s", "copy"]

    if ext in (".mp4", ".m4v", ".mov"):
        cmd += ["-movflags", "+faststart"]

    cmd += [dst]
    return cmd


def resolve_output_path(src, base, output_dir, in_place):
    if in_place:
        ext = os.path.splitext(src)[1]
        stem = os.path.splitext(os.path.basename(src))[0]
        tmp = os.path.join(
            os.path.dirname(src) or ".",
            ".%s.%s%s" % (stem, uuid.uuid4().hex[:8], ext),
        )
        return tmp, True
    if base:
        rel = os.path.relpath(src, base)
    else:
        rel = os.path.basename(src)
    dst = os.path.join(output_dir, rel)
    parent = os.path.dirname(dst) or "."
    os.makedirs(parent, exist_ok=True)
    return dst, False


def run_ffmpeg(cmd, verbose):
    if verbose:
        log.debug("Running: %s", subprocess.list2cmdline(cmd))
    proc = run_capture(cmd)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise RuntimeError("ffmpeg exited {}:\n{}".format(proc.returncode, tail))


def remux_with_mkvmerge(mkvmerge, path, verbose):
    """Re-mux a matroska file through mkvmerge for VLC-friendly clusters/cues.

    ffmpeg's stream-copied matroska output occasionally produces cluster/cue
    layouts that VLC's reader handles poorly on long seeks (e.g. DVD-era
    MPEG-2 files freeze past halfway). mkvmerge rewrites the container the way
    VLC expects while leaving every track's payload untouched.
    """
    if not shutil_which(mkvmerge):
        raise ToolError(
            "mkvmerge not found on PATH; install MKVToolNix or pass --no-remux"
        )
    ext = os.path.splitext(path)[1].lower()
    stem = os.path.splitext(os.path.basename(path))[0]
    parent = os.path.dirname(path) or "."
    tmp = os.path.join(
        parent, ".%s.%s.remux%s" % (stem, uuid.uuid4().hex[:8], ext),
    )
    cmd = [mkvmerge, "-o", tmp, path]
    if verbose:
        log.debug("Running: %s", subprocess.list2cmdline(cmd))
    proc = run_capture(cmd)
    if proc.returncode != 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise RuntimeError("mkvmerge exited {}:\n{}".format(proc.returncode, tail))
    try:
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError("Could not replace output with remuxed file: {}".format(exc))


class ProgressBar(object):
    """Live, single-line progress display written to stderr (TTY only).

    Swallows any terminal errors so progress can never crash a run.
    """

    def __init__(self, total, enabled):
        self.total = total
        self.enabled = enabled
        self._lock = threading.Lock()
        self._done = 0
        self._current = ""
        self._last = 0
        self._width = 100
        try:
            self._width = shutil.get_terminal_size((100, 24)).columns
        except Exception:
            pass

    def show_current(self, name):
        with self._lock:
            self._current = name
            self._draw()

    def completed(self):
        with self._lock:
            self._done += 1
            self._draw()

    def finish(self):
        self.clear()

    def clear(self):
        if not self.enabled:
            return
        try:
            sys.stderr.write("\r" + " " * self._last + "\r")
            sys.stderr.flush()
            self._last = 0
        except Exception:
            pass

    def _draw(self):
        if not self.enabled:
            return
        try:
            total = self.total
            done = self._done
            pct = (100.0 * done / total) if total > 0 else 100.0
            width = max(20, min(self._width, 120))
            bar_w = max(10, width - 24)
            filled = int(round(bar_w * pct / 100.0)) if total else bar_w
            bar = "#" * filled + "-" * (bar_w - filled)
            line = "\r[%s] %d/%d %3.0f%% | %s" % (
                bar, done, total, pct, self._current)
            if len(line) > width:
                line = line[:width]
            pad = " " * max(0, self._last - len(line))
            self._last = len(line)
            sys.stderr.write(line + pad + "\r")
            sys.stderr.flush()
        except Exception:
            pass


def process_one(args, src, base):
    try:
        vf = analyze_file(src, args.ffprobe)
    except (RuntimeError, json.JSONDecodeError, OSError) as exc:
        return src, base, STATUS_FAILED, "Probe failed: {}".format(exc)

    if not vf.audio:
        return src, base, STATUS_NO_AUDIO, "No audio tracks"

    targets = [s for s in vf.audio if needs_downmix(s)]
    if not targets:
        layouts = ", ".join(
            "%s/%dch" % (s.codec or "?", s.channels) for s in vf.audio)
        return src, base, STATUS_UNCHANGED, "No surround track found ({})".format(layouts)

    if has_english_stereo(vf) and not args.force:
        return src, base, STATUS_SKIP_STEREO, "Already has an English stereo track"

    dst, is_temp = resolve_output_path(src, base, args.output, args.in_place)
    if not is_temp and os.path.abspath(dst) == os.path.abspath(src):
        return src, base, STATUS_FAILED, "Output path equals input path (refusing to overwrite)"

    cmd = build_command(
        args.ffmpeg, src, dst, vf,
        enhance=args.enhance,
        voice=args.voice,
        bitrate=args.bitrate,
        replace=args.replace,
        loudness=args.loudness if args.loudnorm else None,
    )

    remux = args.remux and os.path.splitext(dst)[1].lower() in (".mkv", ".webm")

    if args.dry_run:
        log.info("%s -> %s", src, subprocess.list2cmdline(cmd))
        if remux:
            tmp = os.path.join(
                os.path.dirname(dst) or ".",
                ".{}.XXXX.remux{}".format(
                    os.path.splitext(os.path.basename(dst))[0],
                    os.path.splitext(dst)[1]),
            )
            log.info("     then remux: %s", subprocess.list2cmdline(
                [args.mkvmerge, "-o", tmp, dst]))
        return src, base, STATUS_PROCESSED, "dry-run"

    try:
        run_ffmpeg(cmd, args.verbose)
    except (RuntimeError, OSError) as exc:
        if is_temp and os.path.exists(dst):
            try:
                os.remove(dst)
            except OSError:
                pass
        return src, base, STATUS_FAILED, str(exc)

    if args.remux and os.path.splitext(dst)[1].lower() in (".mkv", ".webm"):
        try:
            remux_with_mkvmerge(args.mkvmerge, dst, args.verbose)
        except (ToolError, RuntimeError, OSError) as exc:
            if os.path.exists(dst):
                try:
                    os.remove(dst)
                except OSError:
                    pass
            return src, base, STATUS_FAILED, str(exc)

    if is_temp:
        try:
            os.replace(dst, src)
        except OSError as exc:
            try:
                os.remove(dst)
            except OSError:
                pass
            return src, base, STATUS_FAILED, "Could not replace original: {}".format(exc)

    modes = "replace" if args.replace else "add"
    n_tracks = len(targets)
    return src, base, STATUS_PROCESSED, "{} track(s) -> stereo [{}]".format(n_tracks, modes)


def make_parser():
    parser = argparse.ArgumentParser(
        prog="audio_downmix",
        description="Add nightmode (dialogue-boosted) stereo tracks to surround-sound videos.",
        epilog="Run with --help for details; see the module docstring for examples.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="+", metavar="PATH",
                        help="input video files or directories (searched recursively)")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_DIR,
                        help="output directory mirroring the input tree (default: %(default)s)")
    parser.add_argument("--in-place", action="store_true",
                        help="overwrite each original file with the result instead of writing to --output")
    parser.add_argument("--replace", action="store_true",
                        help="drop the original surround track(s) and keep only the new stereo ones")
    parser.add_argument("--force", action="store_true",
                        help="process a file even if it already contains an English stereo track")
    parser.add_argument("--enhance", type=float, default=1.5,
                        help="dialoguenhance boost factor, 0..3 (0 disables the filter, default: %(default)s)")
    parser.add_argument("--voice", type=float, default=2.0,
                        help="dialoguenhance voice-detection sensitivity, 2..32 (default: %(default)s)")
    parser.add_argument("--bitrate", default="192k",
                        help="bitrate for the new stereo AAC track (default: %(default)s)")
    parser.add_argument("--ext", nargs="+", default=sorted(VIDEO_EXTENSIONS),
                        metavar="EXT",
                        help="file extensions to process (default: %(default)s)")
    parser.add_argument("--recursive", action="store_true", default=True,
                        help="scan directories recursively (default: on)")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false",
                        help="only process the immediate files of each directory")
    parser.add_argument("--no-progress", action="store_true",
                        help="disable the live progress bar (also disabled when output is not a TTY)")
    parser.add_argument("--jobs", type=int, default=1,
                        help="number of files to process in parallel (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be done without running ffmpeg")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="path to ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe", help="path to ffprobe")
    parser.add_argument("--mkvmerge", default="mkvmerge",
                        help="path to mkvmerge (used to re-mux matroska output "
                             "into VLC-friendly clusters/cues; default: %(default)s)")
    parser.add_argument("--no-remux", action="store_true",
                        help="skip the mkvmerge re-mux step (output may seek "
                             "poorly in VLC for DVD-era MPEG-2 files)")
    parser.add_argument("--loudness", type=float, default=-16.0,
                        help="target integrated loudness (LUFS) for the new "
                             "stereo track via loudnorm (default: %(default)s)")
    parser.add_argument("--no-loudnorm", dest="loudnorm",
                        action="store_false", default=True,
                        help="disable loudnorm/loudness normalization so the "
                             "new stereo track keeps its natural level")
    parser.add_argument("--pause", action="store_true",
                        help="in a recursive sequential run (--jobs 1), after "
                             "each completed directory ask whether to continue "
                             "to the next one (interactive TTY only)")
    parser.add_argument("--log", metavar="FILE", help="also write log output to this file")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors and the summary")
    parser.add_argument("-v", "--verbose", action="store_true", help="print ffmpeg commands and details")
    return parser


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)

    level = logging.DEBUG if args.verbose else (logging.ERROR if args.quiet else logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    if args.log:
        fh = logging.FileHandler(args.log, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)

    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    if args.jobs < 1:
        parser.error("--jobs must be >= 1")
    if not (0.0 <= args.enhance <= 3.0):
        parser.error("--enhance must be between 0 and 3")
    if not (2.0 <= args.voice <= 32.0):
        parser.error("--voice must be between 2 and 32")
    if not args.bitrate.endswith("k") and not args.bitrate.endswith("K"):
        parser.error("--bitrate should be like '192k'")

    args.remux = not args.no_remux

    try:
        check_tools(args.ffmpeg, args.ffprobe, require_mkvmerge=args.remux)
        check_filter_support(args)
    except ToolError as exc:
        log.error("%s", exc)
        if args.dry_run:
            log.error("ffmpeg/ffprobe are still required for probing during --dry-run.")
        return 2

    if args.in_place and args.output != DEFAULT_OUTPUT_DIR:
        log.error("--in-place and --output are mutually exclusive; ignoring --output")

    files = scan_for_files(args.paths, args.ext, recursive=args.recursive)
    if not files:
        log.error("No video files found in the given paths.")
        return 1

    log.info("Queue: %d video file(s) to examine.", len(files))
    for i, (path, base) in enumerate(files, 1):
        log.info("  %d. %s", i, path)

    if args.dry_run:
        log.info("Dry run - no changes will be made.\n")

    counts = {s: 0 for s in (STATUS_PROCESSED, STATUS_SKIP_STEREO, STATUS_NO_AUDIO,
                            STATUS_UNCHANGED, STATUS_FAILED)}
    failures = []

    use_bar = (
        not args.quiet
        and not args.no_progress
        and not args.dry_run
        and sys.stderr.isatty()
    )
    progress = ProgressBar(len(files), enabled=use_bar)

    def finish(i, res):
        src, base, status, msg = res
        counts[status] = counts.get(status, 0) + 1
        rel = src if base is None else os.path.relpath(src, base)
        if status == STATUS_FAILED:
            log.info("[%d/%d] %-8s %s", i, len(files), status.upper(), rel)
            log.error("    %s", msg)
            failures.append(src)
        elif args.dry_run:
            log.info("[%d/%d] QUEUED  %s  (%s)", i, len(files), rel, msg)
        elif status == STATUS_PROCESSED:
            log.info("[%d/%d] %-8s %s  (%s)", i, len(files), "OK", rel, msg)
        else:
            log.info("[%d/%d] %-8s %s  (%s)", i, len(files), status.upper(), rel, msg)

    def started(i, rel):
        if not args.quiet:
            log.info("[%d/%d] processing %s ...", i, len(files), os.path.basename(rel))

    if args.jobs == 1 or args.dry_run:
        prev_dir = None
        for i, (src, base) in enumerate(files, 1):
            rel = src if base is None else os.path.relpath(src, base)
            cur_dir = os.path.dirname(src)
            if (args.pause and prev_dir is not None and cur_dir != prev_dir
                    and not args.dry_run and not args.quiet
                    and sys.stdin.isatty()):
                progress.clear()
                sys.stdout.write(
                    "\nFinished directory {!r}. Continue to {!r}? [Y/n] ".format(
                        os.path.basename(os.path.normpath(prev_dir)),
                        os.path.basename(os.path.normpath(cur_dir))))
                sys.stdout.flush()
                try:
                    answer = sys.stdin.readline().strip().lower()
                except (EOFError, OSError):
                    answer = "n"
                if answer in ("n", "no"):
                    log.info("Skipped the remaining %d file(s) after %r.",
                             len(files) - i + 1,
                             os.path.basename(os.path.normpath(prev_dir)))
                    break
            prev_dir = cur_dir
            started(i, rel)
            progress.show_current(rel)
            res = process_one(args, src, base)
            progress.completed()
            progress.clear()
            finish(i, res)
    else:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = []
            for i, (src, base) in enumerate(files, 1):
                rel = src if base is None else os.path.relpath(src, base)
                started(i, rel)
                progress.show_current(rel)
                futures.append(pool.submit(process_one, args, src, base))
            for i, fut in enumerate(as_completed(futures), 1):
                res = fut.result()
                progress.completed()
                progress.clear()
                finish(i, res)
    progress.finish()

    log.info("\nSummary:")
    log.info("  Processed : %d", counts[STATUS_PROCESSED])
    log.info("  Skipped (already has English stereo) : %d", counts[STATUS_SKIP_STEREO])
    log.info("  Skipped (no audio) : %d", counts[STATUS_NO_AUDIO])
    log.info("  Unchanged (no surround) : %d", counts[STATUS_UNCHANGED])
    log.info("  Failed : %d", counts[STATUS_FAILED])
    if failures:
        log.info("  Failed files:")
        for f in failures:
            log.info("    - %s", f)

    return 1 if counts[STATUS_FAILED] else 0


if __name__ == "__main__":
    sys.exit(main())