# mediafix — build plan

**Status:** implemented. Last written 2026-09-30.
**Resumes from:** this conversation.

## 1. Context

Two pieces already exist:

- `C:\Users\jdsco\scripts\audio_downmix\` — working downmix CLI; its engine is vendored verbatim.
- `C:\Users\jdsco\faster-whisper-p2000\` — GPU transcription stack (faster-whisper + CTranslate2 + PyAV).

**Goal:** one terminal app that recursively scans a media folder, lists what's missing English subtitles and/or an English stereo downmix, lets the user tick per-item actions, then runs them (sidecars + in-place downmix).

## 2. Verified constraints

| Constraint | Detail |
|---|---|
| P2000 is Pascal `sm_61` | CTranslate2 includes native kernels. |
| Driver 590+ drops Pascal | Host must run 570.x/580.x (avoid 590+). CUDA 12.8 needs ≥570.26. |
| No cuDNN needed | Wheels built `WITH_CUDNN=OFF`. |
| FFmpeg >= 6.0 for `dialoguenhance` | Ubuntu 24.04 ships 6.1.1. |
| Base image | `nvidia/cuda:12.8.1-runtime-ubuntu24.04` (Python 3.12, ffmpeg 6.1.1, mkvtoolnix 82.0). |
| CPU fallback | faster-whisper supports `device=cpu` if GPU missing. |

## 3. Locked decisions

1. Run in Docker on the Ubuntu box, library bind-mounted.
2. Sidecar `.srt` files: `<stem>.eng.srt` (or `.srt` for "en" → mapped to `eng`).
3. "Missing subtitle" = no embedded English subtitle AND no English sidecar (`.srt`, `.en.srt`, `.eng.srt`). Other languages do not count.
4. Downmix in place, atomically (vendor temp file + `os.replace`).
5. Model default `small` on GPU; configurable via `--model`.

## 4. Architecture

```
scan (ffprobe, parallel, cache) → selection TUI/apply → runner
  ├─ subtitle worker (GPU, serial, chunked 600s)
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
  test_scan.py  test_downmix.py  test_runner.py  test_integration.py
```

## 7. Detection rules

- Exclude: `@eaDir`, `.Trash*`, `nightmix_output`, dotfiles/dot-dirs.
- ffprobe cache: JSON at `cache_path` keyed by (path,size,mtime).
- Needs subtitle: no English embedded subtitle and no English sidecar.
- Needs audio: has ≥6ch track and no English stereo track.

## 8. CLI

- `mediafix tui` (default) — Textual UI.
- `mediafix scan` — report, `--json`, filters `all|sub|audio|both|clean`.
- `mediafix apply` — non-interactive, `--dry-run`, `--force`, `-y`, `--only sub|audio|both`.
- `mediafix check` — tools, GPU, model, SRT roundtrip, downmix roundtrip.

## 9. Safety

- Atomic sidecar writes (`os.replace`), no partial files.
- In-place downmix: temp next to source, atomic replace, disk preflight (margin).
- Per-job errors surfaced, non-zero exit on failures.
- Runs as `${UID}:${GID}` in container.

## 10. Tests

75 tests total: unit, runner, integration (real ffmpeg). All green.

## 11. Known notes

- `args.remux` not present when calling `process_one` directly — derived from `args.no_remux` in wrapper.
- Duration may come from format or streams; probe has fallback.
- GPU 590+ drops Pascal — `check` warns if compute capability looks >=9.

## 12. Build commands

```bash
cd C:\Users\jdsco\scripts\mediafix
python -m unittest discover -s tests -v
```

On Ubuntu with Docker/NVIDIA: `docker compose up --build` to run TUI.