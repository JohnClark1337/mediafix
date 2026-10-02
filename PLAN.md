# mediafix — build plan

**Status:** implemented and running CPU-only. Last updated 2026-10-01.
For user-facing docs see [README.md](README.md); this file records the design
decisions and the reasoning behind them.

## 1. Context

Two pieces already exist:

- `audio_downmix` (published as `media-downmixer`) — working downmix CLI; its engine is
  vendored verbatim here as `audio_downmix.py`.
- `faster-whisper-p2000` — transcription stack (faster-whisper + CTranslate2 + PyAV),
  whose audio-decode path is ported into `mediafix/audio.py`.

**Goal:** one terminal app that recursively scans a media folder, lists what's missing English subtitles and/or an English stereo downmix, lets the user tick per-item actions, then runs them (sidecars + in-place downmix).

## 2. Verified constraints

| Constraint | Detail |
|---|---|
| Host is Ubuntu 20.04 | GPU/driver path abandoned; CPU-only. Container base is independent of the host distro. |
| No CUDA | Image uses plain `ubuntu:24.04`; no `nvidia-*` wheels, no `--gpus`. |
| CPU inference | CTranslate2 CPU backend, `device="cpu"`, `compute_type="int8"`. |
| FFmpeg >= 6.0 for `dialoguenhance` | Container base Ubuntu 24.04 ships 6.1.1, independent of the 20.04 host. |
| Base image | `ubuntu:24.04` (Python 3.12, ffmpeg 6.1.1, mkvtoolnix). |
| Python >= 3.11 | `tomllib` for `config.toml`, plus `X \| Y` type syntax. |
| `APP_UID` must not be 1000 | `ubuntu:24.04` ships an `ubuntu` user at UID 1000; image uses 10001. |

### Why the GPU was dropped

The host is a P2000 (Pascal `sm_61`) on driver 535.183.01. GPU transcription was
attempted and abandoned because every workable combination needed two pins that
had to be kept in agreement:

- The `nvidia/cuda:12.8.1-runtime-ubuntu24.04` base declares
  `NVIDIA_REQUIRE_CUDA "cuda>=12.8"`, which a 535 driver refuses.
- `ctranslate2` 4.5+ requires cuDNN 9, and cuDNN 9 dropped Pascal in 9.22, so
  4.4.0 was the only usable version.

Pascal is *not* the problem in CUDA terms: CUDA 12.x still ships `sm_61`
kernels, and it is only removed in CUDA 13. The 535 driver caps CUDA at 12.2,
which is why the pins above cluster around 12.2/cuDNN 8. Rather than carry that,
the build targets CPU and the image carries no GPU code at all.

## 3. Locked decisions

1. Run in Docker on the Ubuntu box, library bind-mounted.
2. Sidecar `.srt` files: `<stem>.eng.srt` (or `.srt` for "en" → mapped to `eng`).
3. "Missing subtitle" = no embedded English subtitle AND no English sidecar (`.srt`, `.en.srt`, `.eng.srt`). Other languages do not count.
4. Downmix in place, atomically (vendor temp file + `os.replace`).
5. Model default `small`; configurable via `--model`.
6. CPU-only transcription. The GPU path was removed rather than pinned: see §2.
   `--device cuda` is still accepted so the plumbing can be restored.

## 4. Architecture

```
scan (ffprobe, parallel, cache) → selection TUI/apply → runner
  ├─ subtitle worker (CPU/int8, serial, chunked 600s)
  └─ downmix pool (CPU/I/O, N parallel)
```

## 5. Reuse

- `audio_downmix.py` vendored byte-identical. Reused: `probe_file`, `needs_downmix`, `is_english`, `has_english_stereo`, `process_one`, `make_parser`, `resolve_output_path`, `check_tools`, `check_filter_support`.
- faster-whisper + PyAV extraction ported to `mediafix/audio.py` (16 kHz mono float32, chunked transcription).

## 6. Module map

```
mediafix/
  __init__.py  cli.py  config.py  probe.py  scan.py
  srt.py  audio.py  subtitles.py  downmix.py  runner.py  tui.py  selftest.py
audio_downmix.py (vendored)
config.example.toml  Dockerfile  docker-compose.yml  requirements.txt  PLAN.md
tests/
  test_scan.py  test_downmix.py  test_runner.py  test_integration.py  test_cli.py
```

## 7. Detection rules

- Exclude: `@eaDir`, `.Trash*`, `nightmix_output`, dotfiles/dot-dirs.
- ffprobe cache: JSON at `cache_path` keyed by (path,size,mtime).
- Needs subtitle: no English embedded subtitle and no English sidecar.
- Needs audio: has ≥6ch track and no English stereo track.

## 8. CLI

- `mediafix tui [paths...]` (default) — Textual UI. Takes path roots only; it
  deliberately has no `--filter`/`--only`/`--limit`, which would collide with
  the UI's own key bindings.
- `mediafix scan [paths...]` — report, `--json`, `--only`, `--filter`, `--limit`.
- `mediafix apply [paths...]` — non-interactive, `--dry-run`, `--force`, `-y`.
- `mediafix check` — tools, ctranslate2, model, SRT roundtrip, downmix roundtrip.

All four accept `--config`; every config key is overridable via
`MEDIAFIX_<KEY>`. Paths default to `$MEDIA_ROOT` when omitted.

## 9. Safety

- Atomic sidecar writes (`os.replace`), no partial files.
- In-place downmix: temp next to source, atomic replace, disk preflight (margin).
- Per-job errors surfaced, non-zero exit on failures.
- Runs as `${UID}:${GID}` in container.

## 10. Tests

84 tests total: unit, cli, runner, integration (real ffmpeg). All green.

## 11. Known notes

- `args.remux` not present when calling `process_one` directly — derived from `args.no_remux` in wrapper.
- Duration may come from format or streams; probe has fallback.
- CPU-only: GPU path removed from the image, compose file, and run scripts.
- `compose run` never rebuilds, so a `git pull` needs an explicit `compose build`
  or the old image runs. `run-docker.sh` self-heals via a `.build-hash` stamp.

## Textual API and attribute-name constraints

Found only by running the real TUI, not by unit tests:

- Never name a widget attribute after a `DOMNode` property. `self.visible: list = []`
  shadows `DOMNode.visible`; the annotation still assigns, and Textual's setter
  then got a list, giving `TypeError: unhashable type: 'list'`. The filtered-row
  list is `self.shown`.
- `DataTable` has no `update_cell_at_row`. Use
  `update_cell_at(Coordinate(row, col), value)`; passing string row/column keys
  to `update_cell` raises `CellDoesNotExist`, because the table stores its own
  `RowKey`/`ColumnKey` objects.
- Both `SelectScreen._refresh` and `RunScreen._apply` used the bad call, so every
  selection toggle and every progress update crashed.
- `textual>=8.0` is the floor for `update_cell_at`.

## Model cache path

`huggingface_hub` computes `HF_TOKEN_PATH = join(HF_HOME, "token")` **at import
time** (constants.py:250) and writes it during download. A relative or
unwritable `HF_HOME` therefore surfaces only as
`PermissionError: ... './hf/token'` with no clue which path failed.

- `SubtitleEngine.load()` calls `_check_cache()` *before* importing
  `faster_whisper`, so the failure is a `ModelCacheError` naming `HF_HOME`, the
  working directory, and the host-side `chown` fix.
- `selftest` gained a `[cache]` section that prints `HF_HOME`, `HOME`, uid, cwd,
  and the resolved cache/token paths, then proves writability. It runs before
  `[model]`.
- `HF_HOME` must be absolute. It is set to `/home/mediafix/.cache/huggingface` in
  both the Dockerfile and compose, and `/hf` in `run-docker.sh`.
- Because constants are import-time, changing `HF_HOME` in a running process has
  no effect; the cache tests run in a subprocess.

## ScanResult units

`duration_scanned` was assigned `sum(i.size for i in result.items)`, i.e. bytes,
so a 7.5 TB library reported a scan "duration" of 7579436368589. It now records
elapsed seconds from `time.monotonic()`; byte totals live in `total_bytes`.
- Transcribed audio is chunked at 600s with 2s overlap to bound peak memory.
- `tui` originally lacked the `paths` positional and crashed with `AttributeError`;
  `tests/test_cli.py` now guards path parsing for every subcommand.

## 12. Build commands

```bash
cd /path/to/mediafix
python -m unittest discover -s tests -v
```

On Ubuntu with Docker: `./quickstart.sh MEDIA_ROOT=/mnt/p2/TV`, then `./run-docker.sh tui`.