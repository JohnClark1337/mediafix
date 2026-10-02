import argparse
import os
import sys
import threading
from queue import Empty

from mediafix import config as config_mod
from mediafix import downmix as downmix_mod
from mediafix import scan as scan_mod
from mediafix import selftest as selftest_mod

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_USAGE = 2


def _add_engine_options(parser) -> None:
    group = parser.add_argument_group("subtitle engine")
    group.add_argument("--model", help="whisper model name (default: small)")
    group.add_argument("--device", choices=["cpu", "cuda"], help="inference device (default cpu)")
    group.add_argument("--compute-type", dest="compute_type", help="ctranslate2 compute type")
    group.add_argument("--cpu-threads", dest="cpu_threads", type=int, help="cpu thread count")
    group.add_argument("--beam-size", dest="beam_size", type=int, help="beam size")
    group.add_argument("--language", dest="sub_language", help="source/subtitle language code")
    group.add_argument("--translate", action="store_true", default=None,
                       help="translate to English instead of transcribing verbatim")
    group.add_argument("--audio-stream", dest="audio_stream", type=int,
                       help="force a specific audio stream index")
    group.add_argument("--sidecar-ext", dest="sidecar_ext", help="subtitle sidecar extension")


def _add_downmix_options(parser) -> None:
    group = parser.add_argument_group("downmix")
    group.add_argument("--enhance", type=float, help="dialoguenhance boost factor (0 disables)")
    group.add_argument("--voice", type=float, help="dialoguenhance voice sensitivity")
    group.add_argument("--bitrate", help="AAC bitrate for the new stereo track")
    group.add_argument("--loudness", type=float, help="loudnorm target in LUFS")
    group.add_argument("--no-loudnorm", dest="loudnorm", action="store_false", default=None)
    group.add_argument("--replace", action="store_true", default=None,
                       help="drop original surround tracks instead of adding to them")
    group.add_argument("--no-remux", dest="remux", action="store_false", default=None)
    group.add_argument("--downmix-jobs", dest="downmix_jobs", type=int,
                       help="parallel ffmpeg downmix workers")


def _add_run_options(parser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    parser.add_argument("--force", action="store_true", help="downmix even if stereo already exists")
    parser.add_argument("--no-cache", action="store_true", help="ignore the ffprobe cache")
    parser.add_argument("--cache", help="path to the ffprobe cache file")


def _add_scan_options(parser) -> None:
    parser.add_argument("paths", nargs="*", help="files or directories (searched recursively)")
    parser.add_argument("--only", choices=("sub", "audio", "both"),
                        help="restrict to one kind of problem")
    parser.add_argument("--filter", choices=scan_mod.FILTERS, default=scan_mod.FILTER_ALL)
    parser.add_argument("--limit", type=int, help="process at most this many files")


def _overrides(args) -> dict:
    keys = (
        "model", "device", "compute_type", "cpu_threads", "beam_size", "sub_language",
        "translate", "audio_stream", "sidecar_ext", "enhance", "voice", "bitrate",
        "loudness", "loudnorm", "replace", "remux", "downmix_jobs",
    )
    return {key: getattr(args, key) for key in keys if getattr(args, key, None) is not None}


def _resolve_paths(args) -> list[str]:
    paths = list(getattr(args, "paths", None) or [])
    if not paths:
        env_root = os.environ.get("MEDIA_ROOT") or "/media"
        paths = [env_root]
    missing = [p for p in paths if not os.path.exists(os.path.expanduser(p))]
    if missing:
        raise SystemExit(f"path not found: {', '.join(missing)}")
    return paths


def _print_scan(result, items, limit=None) -> None:
    from mediafix.scan import human_duration, human_size

    print(
        f"scanned {len(result.items)} file(s) in {len(result.roots)} root(s) "
        f"({result.cached} cached) "
        f"[sub:{result.needs_subtitle_count} audio:{result.needs_audio_count} "
        f"both:{result.both_count} clean:{result.clean_count} error:{result.failed_count}]"
    )
    if not items:
        print("nothing to do")
        return
    shown = items if limit is None else items[:limit]
    for item in shown:
        flags = []
        flags.append("SUB" if item.want_subtitle else "   ")
        flags.append("AUD" if item.want_audio else "   ")
        status = item.error or (
            f"{item.size and human_size(item.size)} {human_duration(item.duration)}".strip()
        )
        print(f"  [{''.join(flags)}] {item.path}  ({status})")
    if limit is not None and len(items) > limit:
        print(f"  ... {len(items) - limit} more (use --limit 0 for all)")


def _select(items, only: str) -> list:
    chosen = []
    for item in items:
        if not item.selectable:
            continue
        want_sub = item.want_subtitle and only in ("sub", "both")
        want_aud = item.want_audio and only in ("audio", "both")
        if not (want_sub or want_aud):
            continue
        item.selected = True
        item.want_subtitle = want_sub
        item.want_audio = want_aud
        chosen.append(item)
    return chosen


def _run_batch(config, items, dry_run, force, assume_yes):
    from mediafix import runner as runner_mod

    if not items:
        print("nothing selected")
        return EXIT_OK

    counts = {
        "subtitle": sum(1 for i in items if i.want_subtitle),
        "downmix": sum(1 for i in items if i.want_audio),
    }
    print(f"planned {counts['subtitle']} subtitle job(s), {counts['downmix']} downmix job(s)")

    if dry_run:
        print("dry-run: no files will be modified")
        for item in items:
            actions = []
            if item.want_subtitle:
                actions.append("subtitle")
            if item.want_audio:
                actions.append("downmix")
            print(f"  {' + '.join(actions)}: {item.path}")
        return EXIT_OK

    if not assume_yes:
        reply = input("proceed? [y/N] ").strip().lower()
        if reply not in {"y", "yes"}:
            print("aborted")
            return EXIT_OK

    try:
        for warning in downmix_mod.preflight(config, force=force):
            print(f"warning: {warning}")
    except downmix_mod.ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    engine_runner = runner_mod.Runner(config, dry_run=dry_run, force=force)
    jobs = engine_runner.plan(items)
    if not jobs:
        print("nothing selected")
        return EXIT_OK

    total = len(jobs)
    seen: set[str] = set()
    print(f"running {total} job(s)\n")

    def drain() -> None:
        while True:
            try:
                job, state, message, progress = engine_runner.events.get(timeout=0.5)
            except Empty:
                if engine_runner.wait(0.1):
                    return
                continue
            if job is None:
                print(f"  ! {state}: {message}")
                continue
            key = id(job)
            marker = f"{job.label}|{state}"
            if state in runner_mod.TERMINAL_STATES and marker in seen:
                continue
            if state in runner_mod.TERMINAL_STATES:
                seen.add(marker)
            width = 24
            filled = int(progress * width) if progress else 0
            bar = "#" * filled + "." * (width - filled)
            if state == runner_mod.JOB_RUNNING:
                print(f"  [{bar}] {job.kind:<8} {job.label}  {message}")
            elif state in runner_mod.TERMINAL_STATES:
                print(f"  [{state.upper():<8}] {job.kind:<8} {job.label}  {message}")

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    summary = engine_runner.run()
    reader.join(timeout=5)

    print(
        f"\n{summary.total} job(s): {summary.done} done, {summary.skipped} skipped, "
        f"{summary.failed} failed, {summary.cancelled} cancelled "
        f"in {summary.elapsed / 60:.1f} min"
    )
    if summary.failures:
        print("\nfailures:")
        for kind, label, message in summary.failures:
            print(f"  {kind:<8} {label}: {message}")
    return summary.exit_code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="mediafix",
        description="Find and repair missing English subtitles and stereo downmixes in a media library.",
    )
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="verify tools, ctranslate2 and model, then run a downmix roundtrip")
    p_check.add_argument("--skip-model", action="store_true", help="do not load the whisper model")
    p_check.add_argument("--skip-downmix", action="store_true", help="do not run the downmix roundtrip")

    p_tui = sub.add_parser("tui", help="interactive terminal interface (default)")
    # Only the path roots here: the TUI drives filtering/selection itself and
    # defining --filter/--only/--limit would collide with those bindings.
    p_tui.add_argument("paths", nargs="*", help="files or directories (searched recursively)")
    _add_engine_options(p_tui)
    _add_downmix_options(p_tui)

    p_scan = sub.add_parser("scan", help="report what is missing without changing anything")
    _add_scan_options(p_scan)
    p_scan.add_argument("--no-cache", action="store_true", help="ignore the ffprobe cache")
    p_scan.add_argument("--cache", help="path to the ffprobe cache file")
    p_scan.add_argument("--json", action="store_true", help="emit machine-readable json")

    p_apply = sub.add_parser("apply", help="run repairs non-interactively")
    _add_scan_options(p_apply)
    _add_run_options(p_apply)
    _add_engine_options(p_apply)
    _add_downmix_options(p_apply)
    p_apply.add_argument("-y", "--yes", action="store_true", help="do not prompt for confirmation")

    args = parser.parse_args(argv)
    command = args.command or "tui"

    if command == "check":
        config = config_mod.load(args.config, {})
        return selftest_mod.run(
            config, model=not args.skip_model, downmix=not args.skip_downmix
        )

    config = config_mod.load(args.config, _overrides(args))
    paths = _resolve_paths(args)

    if command == "scan":
        print(f"scanning {', '.join(paths)} ...", flush=True)
        result = scan_mod.scan(
            paths, config,
            progress=lambda done, total: print(f"\r  probed {done}/{total}", end="", flush=True),
            use_cache=not args.no_cache,
        )
        print("\r" + " " * 32 + "\r", end="")
        items = scan_mod.filter_items(result.items, args.filter)
        if args.json:
            import json

            print(json.dumps({
                "roots": result.roots,
                "needs_subtitle": result.needs_subtitle_count,
                "needs_audio": result.needs_audio_count,
                "files": [i.path for i in items],
            }, indent=2))
            return EXIT_OK
        _print_scan(result, items, args.limit or None)
        return EXIT_OK

    if command == "apply":
        result = scan_mod.scan(paths, config, use_cache=not args.no_cache)
        items = _select(scan_mod.filter_items(result.items, args.filter), args.only or "both")
        if args.limit:
            items = items[:args.limit]
        _print_scan(result, items)
        return _run_batch(config, items, args.dry_run, args.force, args.yes)

    if command == "tui":
        from mediafix.tui import MediaFixApp

        MediaFixApp(config=config, roots=paths).run()
        return EXIT_OK

    parser.print_help()
    return EXIT_USAGE