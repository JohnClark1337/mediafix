# mediafix — build plan

**Status:** implemented and running CPU-only; censorship ("bleeparr") added.
Last updated 2026-10-06.
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
  ├─ CPU pipeline, strictly in plan order: subtitle then censor per file
  └─ downmix pool (CPU/I/O, N parallel), gated until censor finishes per file
```

The CPU pipeline is a single serial thread: a subtitle job, then that file's
censor job (Whisper models are cached and shared), then the next file that
needs CPU work. Downmix workers wait on a per-path event before touching the
file so they always derive stereo from the *censored* audio. Stalls before the
censor gate are impossible: every gate is pre-created and released in a
`finally`, and a strict-upstream skip propagates as a note to the dependent
downmix rather than a deadlock. A fatal subtitle error aborts the remaining CPU
pipeline (bounded memory) but never leaves the downmix pool stuck.

## 5. Reuse

- `audio_downmix.py` vendored. Reused: `probe_file`, `needs_downmix`, `is_english`, `has_english_stereo`, `process_one`, `make_parser`, `resolve_output_path`, `check_tools`, `check_filter_support`. Two small local changes keep default behavior byte-identical: `analyze_file` parses an audio stream `title` (for the censored-track prefix) and `has_english_stereo`/`build_command` accept a `censor_prefix=None` so a downmix limiter can restrict targets to censored surround tracks when one exists.
- faster-whisper + PyAV extraction ported to `mediafix/audio.py` (16 kHz mono float32, chunked transcription).
- Censorship ported from `davidpeele/bleeparr_CLI` into `mediafix/censor.py`, library-style: no module-level argparse, no `exit()`, scratch files under a per-job `tempfile` dir (never `clips/` next to the movie), Whisper models cached per `CensorEngine`, and every failure is a result/exception instead of a hard exit.

## 6. Module map

```
mediafix/
  __init__.py  cli.py  config.py  probe.py  scan.py
  srt.py  audio.py  subtitles.py  censor.py  downmix.py  runner.py  tui.py  selftest.py
audio_downmix.py (vendored, small local patch)
config.example.toml  Dockerfile  docker-compose.yml  requirements.txt  PLAN.md
tests/
  test_scan.py  test_downmix.py  test_runner.py  test_integration.py  test_cli.py
  test_censor.py  test_regressions.py  test_cache.py  test_subtitles.py
```

## 7. Detection rules

- Exclude: `@eaDir`, `.Trash*`, `nightmix_output`, dotfiles/dot-dirs.
- ffprobe cache: JSON at `cache_path` keyed by (path,size,mtime).
- Needs subtitle: no English embedded subtitle and no English sidecar.
- Needs audio: has ≥6ch track and no English stereo track.
- Needs censor: no `Censored (Bleeparr)` audio track AND (an English sidecar whose cheap regex scan finds a swear, or no English sidecar yet — a "candidate" confirmed at job time). A provably clean sidecar is skipped, marked three-state in `SwearMatcher.search_file` (True/False/None).

## 8. CLI

- `mediafix tui [paths...]` (default) — Textual UI. Takes path roots only; it
  deliberately has no `--filter`/`--only`/`--limit`, which would collide with
  the UI's own key bindings.
- `mediafix scan [paths...]` — report, `--json`, `--only`, `--filter`, `--limit`.
- `mediafix apply [paths...]` — non-interactive, `--dry-run`, `--force`, `-y`.
- `mediafix check` — tools, ctranslate2, model, SRT roundtrip, downmix roundtrip,
  censorship roundtrip (`--skip-model`, `--skip-downmix`, `--skip-censor`).

`--only` and the TUI add a third flag: subtitle / audio / **censor**. The censor
roundtrip in `check` runs the full pipeline against a generated 2 s clip with a
seeded `.eng.srt` sidecar and an injected fake Whisper model, then verifies a
`Censored (Bleeparr)` track exists via ffprobe.

All four accept `--config`; every config key is overridable via
`MEDIAFIX_<KEY>`. Paths default to `$MEDIA_ROOT` when omitted.

## 9. Safety

- Atomic sidecar writes (`os.replace`), no partial files.
- In-place downmix: temp next to source, atomic replace, disk preflight (margin).
- Per-job errors surfaced, non-zero exit on failures.
- Runs as `${UID}:${GID}` in container.

## 10. Tests

162 tests total: unit, cli, runner, integration (real ffmpeg) plus a dedicated
`test_censor.py` and censor coverage in scan/runner/cli. 12 require `textual` /
`faster_whisper` / `huggingface_hub` and are skipped in a bare host environment
(subliminal/`srt` gate the corresponding sections and checks). All green in the
Ubuntu container.

## 11. Known notes

- `args.remux` not present when calling `process_one` directly — derived from `args.no_remux` in wrapper.
- Duration may come from format or streams; probe has fallback.
- CPU-only: GPU path removed from the image, compose file, and run scripts.
- `compose run` never rebuilds, so a `git pull` needs an explicit `compose build`
  or the old image runs. `run-docker.sh` self-heals via a `.build-hash` stamp.
- The censored audio track is appended *after* the original streams so a
  remux/downmix that drops "generated tracks" is a one-flag change: downmix
  targets are restricted by the `Censored (Bleeparr)` title prefix instead.
- A censor job resolves a subtitle one-off (sidecar → embedded → one-off
  download) and never persists it, so a pure-censor run cannot scribble sidecars
  the user did not ask for.
- `beep_mode` defaults to `""` (no beep). `words`/`segments`/`both` only take
  effect when `beep = true`.
- A filename `subliminal`'s `guessit` cannot parse (e.g. `Apocalpyse Now.mkv`)
  used to raise `GuessingError: Insufficient data to process the guess` out of
  `Video.fromname`, which `_safe_cpu` reported as "subtitle/censor pipeline
  crashed" and silently cancelled every remaining CPU job. `download_subtitle_text`
  now treats a guessing failure as *no online subtitle found* (log + return
  `None`), so the file degrades to Whisper transcription / "no subtitles for
  censorship" like any other unsuccessful search; `_run_subtitle` and
  `run_censor_job` additionally swallow per-file exceptions into a failed result,
  so no single file can abort the pipeline.

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

## Model cache permissions

`makedirs` failed on `/home/mediafix/.cache` even though the Dockerfile does
`chmod -R a+rwX /home/mediafix/.cache`. Two causes, both addressed:

- The chmod applied to `.cache` but **not to `/home/mediafix`**, so a runtime uid
  other than 10001 (compose sets `${MEDIAFIX_UID:-1000}`) could not traverse into
  it. `/home/mediafix` now gets `a+x`.
- The Dockerfile never set `HOME`; it inherited `/` from the base image, which
  is not writable. Now `HOME=/home/mediafix`.

The bind mount is `${MEDIAFIX_HF_DIR:-./.hf-cache}`. When that variable is unset
compose uses a relative `./.hf-cache`, and Docker creates missing bind-mount
sources as **root**, leaving a directory the container user cannot write.
`quickstart.sh` writes `MEDIAFIX_HF_DIR` into `.env` to avoid that; setting it by
hand works too. If the mount is not in place, `makedirs` still has to create the
cache and the `[cache]` check reports which parent is unwritable.

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