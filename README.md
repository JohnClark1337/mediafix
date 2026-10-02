# mediafix

Finds and repairs two things missing from a media library:

1. **English subtitles** — transcribes the audio and writes a sidecar `.srt`
   (`<name>.eng.srt`) next to the video. Plex/Jellyfin pick these up automatically.
2. **English stereo downmix** — reuses the proven engine from
   `audio_downmix` to add a dialogue-boosted stereo track (with `dialoguenhance`
   and `loudnorm`) to surround films, rewritten in place atomically.

Both pipelines run together: the transcriber works while CPU/IO workers downmix
in parallel. Everything runs in Docker; nothing is installed on the host beyond
Docker itself.

## Requirements

- FFmpeg 6.0+ (for the `dialoguenhance` filter), ffprobe, MKVToolNix.
- Python 3.11+ (`tomllib`, used to read `config.toml`, is 3.11+; the code also
  uses `X | Y` type syntax, so 3.10 is not enough).
- Transcription runs on **CPU with `int8`**. No GPU, driver, or CUDA runtime is
  needed, and none is installed in the image.

## Quick start (local)

```bash
pip install -r requirements.txt
python -m mediafix check                     # verify tools + ctranslate2 + model
python -m mediafix scan ~/Videos             # report only, change nothing
python -m mediafix tui                       # interactive (default)
python -m mediafix apply ~/Videos -y         # batch, unattended
```

## Quick start (Docker)

```bash
./quickstart.sh MEDIA_ROOT=/mnt/p2/TV
```

Safe to re-run: each step checks before it changes anything.

The script checks prerequisites, writes `.env` with your uid/gid so files stay
writable, builds the image, and runs the self-check. Model weights persist in
`$MEDIAFIX_HF_DIR` (default `~/.cache/mediafix/hf`), so they download once.

The container is Ubuntu 24.04 regardless of your host distro — it needs ffmpeg
6.1 for `dialoguenhance`. Verified on an Ubuntu 20.04 host.

Then:

```bash
MEDIA_ROOT=/mnt/p2/TV docker compose run --rm mediafix tui
```

### If `docker compose` is not installed

Compose v2 is a separate plugin. Without it, use the compose-free runner, which
does the same thing with plain `docker run`:

```bash
MEDIA_ROOT=/mnt/p2/TV ./run-docker.sh tui
```

`run-docker.sh` runs the same container with plain `docker run` and always
transcribes on CPU. It builds the image on first use.

To install compose v2 on Ubuntu:

```bash
sudo apt-get update && sudo apt-get install -y docker-compose-plugin
```

## Performance notes

- Transcription is CPU-only, `int8` compute type. There is no CUDA base image,
  no `nvidia-*` pip wheels, and no `--gpus` flag in the run scripts.
- `small` (default) is the practical ceiling. `medium` is slow; `large-v3` is
  impractical on CPU. Whisper transcription is roughly real-time on a modern
  desktop CPU at `small`/`int8` and slower than that on a NAS-class CPU.
- Subtitle work is serial (one model, one pass), while downmixing fans out
  across `downmix_jobs` parallel ffmpeg workers. On a many-core box the downmix
  half finishes long before transcription does.
- Transcribed audio is processed in 600s chunks with 2s overlap to bound peak
  memory on long films.
- To trade speed for accuracy, set `compute_type = "float32"` in `config.toml`.
- `--only audio` skips transcription entirely, which is much faster if you only
  want downmixes.

### Re-enabling the GPU

The GPU path was removed because driver/library version skew on a Pascal (P2000,
`sm_61`) host proved hard to pin; see git history for the details. Restoring it
means all of:

1. A CUDA base image, or `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` back in
   `requirements.txt`.
2. `ctranslate2` at `4.4.0`. Version 4.5 moved to cuDNN 9, and cuDNN 9 dropped
   Pascal support in 9.22, so 4.4.0 is the last release that works on `sm_61`.
3. Host driver below the 590 branch, which dropped Pascal entirely. CUDA 12.x
   still supports `sm_61` (it is removed only in CUDA 13).
4. `deploy.resources.reservations.devices` restored in `docker-compose.yml` and
   `--gpus all` in `run-docker.sh`, plus `device = "cuda"`.

The `--device` flag still accepts `cuda`; only the plumbing is gone.

## CLI reference

| Command | What it does |
|---|---|
| `mediafix check` | Verify tools, CTranslate2, model load, SRT roundtrip, downmix roundtrip. `--skip-model`, `--skip-downmix` to trim it. |
| `mediafix scan [paths...]` | Report only; changes nothing. `--json` for machine output. |
| `mediafix tui [paths...]` | Interactive Textual UI (default). |
| `mediafix apply [paths...]` | Unattended repairs. |

`scan` and `apply` take `--only sub|audio|both`, `--filter all|sub|audio|both|clean`,
and `--limit N`. `apply` adds `--dry-run`, `--force`, and `-y`. All four accept
`[paths...]`; when omitted, the root comes from `MEDIA_ROOT` (default `/media`
in the container, else the current directory). `tui` deliberately omits
`--filter`/`--only`/`--limit` because the UI drives those interactively.

Engine flags (`--model`, `--device`, `--compute-type`, `--cpu-threads`,
`--beam-size`, `--language`, `--translate`, `--audio-stream`, `--sidecar-ext`)
and downmix flags (`--enhance`, `--voice`, `--bitrate`, `--loudness`,
`--no-loudnorm`, `--replace`, `--no-remux`, `--downmix-jobs`) apply to `tui` and
`apply`. Run `mediafix <command> --help` for the authoritative list.

## TUI keys

| Key | Action |
|---|---|
| `space` | toggle selection of the highlighted file |
| `s` / `a` | toggle subtitle / downmix for the file |
| `S` / `A` | toggle subtitle / downmix for every visible file |
| `f` | cycle filter: all / sub / audio / both / clean |
| `r` | toggle dry-run |
| `enter` | start the run |
| `esc` | back, or cancel a running job |

While a run is in progress, `esc` or `c` cancels; a sidecar already written is
left in place and a partially-written one is never published.

## Configuration

Copy `config.example.toml` to `config.toml`. Any key can be overridden with a
`MEDIAFIX_<KEY>` environment variable (e.g. `MEDIAFIX_MODEL=medium`), and most
have CLI flags. `--config` points at an alternate file, and `MEDIAFIX_CONFIG`
does the same via the environment.

`audio_stream` picks which track gets transcribed. Left unset, it prefers a
non-surround English non-commentary stream, then the default disposition, then
any non-commentary stream, then whatever is first. Set it to an index to pin a
specific stream.

## How detection works

A file **needs subtitles** when it has no embedded English subtitle stream and
no English sidecar (`name.srt`, `name.en.srt`, `name.eng.srt`; other languages
do not count).

A file **needs a downmix** when it has a ≥6-channel audio track and no existing
two-channel English track. The generated track is tagged `language=eng` and
`title=Nightmix Stereo`, so re-runs detect and skip it.

`@eaDir`, `.Trash*`, `nightmix_output`, and dot-directories are skipped. ffprobe
results are cached to JSON and invalidated when a file's size or mtime changes.

## Safety

- Sidecars are written to a temp file then `os.replace`d, so Plex never sees a
  partial `.srt`.
- In-place downmixes write a hidden temp next to the source and atomically
  replace it; the original is only overwritten after ffmpeg + mkvmerge succeed.
  Each file is preflighted for free space (file size + margin).
- Every job failure is reported with a non-zero exit code.
- `--dry-run` shows the plan and changes nothing.

## Layout

```
mediafix/            package (cli, scan, probe, srt, audio, subtitles,
                     downmix, runner, tui, selftest, config)
audio_downmix.py     vendored verbatim; the downmix engine
tests/               84 unit, cli, runner, and real-ffmpeg integration tests
```

Run tests: `python -m unittest discover -s tests -v`.