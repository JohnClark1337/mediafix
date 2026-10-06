# mediafix

Finds and repairs three things missing from a media library:

1. **English subtitles** — transcribes the audio and writes a sidecar `.srt`
   (`<name>.eng.srt`) next to the video. Plex/Jellyfin pick these up automatically.
2. **English stereo downmix** — reuses the proven engine from
   `audio_downmix` to add a dialogue-boosted stereo track (with `dialoguenhance`
   and `loudnorm`) to surround films, rewritten in place atomically.
3. **Profanity censorship ("bleeparr")** — finds swear words in the subtitles,
   refines each occurrence to per-word timestamps with Whisper, and appends a
   `Censored (Bleeparr)` audio track that mutes them. Original streams are
   untouched; the generated track is skipped on re-runs.

All three pipelines run together: subtitles and censorship share the serial,
CPU/int8 Whisper engine (subtitles first, censorship right after per file),
while CPU/IO workers downmix in parallel. Everything runs in Docker; nothing is
installed on the host beyond Docker itself.

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

### After a `git pull`: rebuild first

`docker compose run` and `docker-compose run` **do not rebuild**. They reuse
whatever `mediafix:latest` already is, so pulling new code and running it will
silently execute the old image. Rebuild explicitly:

```bash
docker-compose build          # or: docker compose build
```

`run-docker.sh` handles this itself — it compares source mtimes against a
`.build-hash` stamp and rebuilds when they differ, so it is safe to use
repeatedly. Force it any time with `MEDIAFIX_REBUILD=1 ./run-docker.sh tui`.

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
| `mediafix check` | Verify tools, CTranslate2, model load, SRT/downmix/censorship roundtrips. `--skip-model`, `--skip-downmix`, `--skip-censor` to trim it. |
| `mediafix scan [paths...]` | Report only; changes nothing. `--json` for machine output. |
| `mediafix tui [paths...]` | Interactive Textual UI (default). |
| `mediafix apply [paths...]` | Unattended repairs. |

`scan` and `apply` take `--only sub|audio|censor|both`,
`--filter all|sub|audio|both|censor|clean`, and `--limit N`. `apply` adds
`--dry-run`, `--force`, and `-y`. All four accept `[paths...]`; when omitted, the
root comes from `MEDIA_ROOT` (default `/media` in the container, else the current
directory). `tui` deliberately omits `--filter`/`--only`/`--limit` because the UI
drives those interactively.

Engine flags (`--model`, `--device`, `--compute-type`, `--cpu-threads`,
`--beam-size`, `--language`, `--translate`, `--audio-stream`, `--sidecar-ext`),
downmix flags (`--enhance`, `--voice`, `--bitrate`, `--loudness`,
`--no-loudnorm`, `--replace`, `--no-remux`, `--downmix-jobs`), and censorship
flags (`--swears`, `--bleeptool`, `--beep`, `--beep-mode`, `--pre-buffer`,
`--post-buffer`, `--boost-db`, `--censor-models`, `--no-subtitle-search`,
`--subliminal-providers`) apply to `tui` and `apply`. Run
`mediafix <command> --help` for the authoritative list.

## TUI keys

| Key | Action |
|---|---|
| `space` | toggle selection of the highlighted file |
| `s` / `a` / `c` | toggle subtitle / downmix / censorship for the file |
| `S` / `A` / `C` | toggle subtitle / downmix / censorship for every visible file |
| `f` | cycle filter: all / sub / audio / both / censor / clean |
| `r` | toggle dry-run |
| `enter` | start the run |
| `esc` | back, or cancel a running job |

Each file row shows a `Sub`/`Aud`/`Cen` column: `want` once picked, `miss`
when it needs work but is not picked, `-` when it needs nothing. While a run is
in progress, `esc` or `c` cancels; a sidecar already written is left in place,
a partially-written one is never published, and a censored track is only ever
swapped in after ffmpeg finishes.

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

A file **needs censorship** when it has no `Censored (Bleeparr)` audio track and
an English subtitle is (or could be) dirty with swear words. If an English
sidecar already exists it is scanned cheaply — clean files are not candidates.
The built-in swear list ships as `mediafix/swears.txt` (from the cleanvid
project); override it with `--swears` or `swears_path`.

Censorship resolves a subtitle (sidecar, embedded stream, or a one-off download
— never persisted), extracts an audio clip per bad section, refines each to
per-word timestamps with the Whisper `S M FSM` passes, then mutes (or beeps)
them on a new AAC track while every original stream is copied through. Because
downmix input derives from the original surround track, a fresh run downmixes
the *censored* audio only.

`@eaDir`, `.Trash*`, `nightmix_output`, and dot-directories are skipped. ffprobe
results are cached to JSON and invalidated when a file's size or mtime changes.

## Safety

- Sidecars are written to a temp file then `os.replace`d, so Plex never sees a
  partial `.srt`.
- In-place downmixes and censored tracks write a hidden temp next to the source
  and atomically replace it; the original is only overwritten after ffmpeg
  (+ mkvmerge, for downmixes) succeeds. Each file is preflighted for free space
  (file size + margin).
- Censor scratch audio lives in a per-job temp directory that is always removed,
  never next to the movie.
- Every job failure is reported with a non-zero exit code.
- `--dry-run` shows the plan and changes nothing.

## Credits

- Swear-word list: [mmguero/cleanvid](https://github.com/mmguero/cleanvid)
  (`mediafix/swears.txt`, BSD-3-Clause).
- Censorship pipeline: ported from
  [davidpeele/bleeparr_CLI](https://github.com/davidpeele/bleeparr_CLI) into a
  library-style module for mediafix (MIT).
- Online subtitle search: [subliminal](https://github.com/Diaoul/subliminal)
  (MIT).

## Layout

```
mediafix/            package (cli, scan, probe, srt, audio, subtitles,
                     censor, downmix, runner, tui, selftest, config)
audio_downmix.py     vendored downmix engine (audio stream title parsing and
                     a censor_prefix hook are the only local changes)
tests/               154 unit, cli, runner, and integration tests
```

Run tests: `python -m unittest discover -s tests -v`.