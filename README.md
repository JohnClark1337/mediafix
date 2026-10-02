# mediafix

Finds and repairs two things missing from a media library:

1. **English subtitles** — transcribes the audio and writes a sidecar `.srt`
   (`<name>.eng.srt`) next to the video. Plex/Jellyfin pick these up automatically.
2. **English stereo downmix** — reuses the proven engine from
   `audio_downmix` to add a dialogue-boosted stereo track (with `dialoguenhance`
   and `loudnorm`) to surround films, rewritten in place atomically.

Both pipelines run together: the transcriber works while CPU/IO workers
downmix in parallel.

## Requirements

- FFmpeg 6.0+ (for the `dialoguenhance` filter), ffprobe, MKVToolNix.
- Python 3.11+.
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

## Quick start (Docker, recommended for the Ubuntu box)

```bash
./quickstart.sh MEDIA_ROOT=/mnt/p2/TV
```

The script checks prerequisites, writes `.env` with your uid/gid so files stay
writable, builds the image, and runs the self-check. Model weights persist in
`$MEDIAFIX_HF_DIR` (default `~/.cache/mediafix/hf`), so they download once.

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
  impractical on CPU.
- Downmixing is unaffected: it is ffmpeg work, and runs in parallel with
  transcription via `downmix_jobs`.
- To trade speed for accuracy, set `compute_type = "float32"` in `config.toml`.
- GPU support can be added back later with `device = "cuda"`, but it would need
  a CUDA base image and `nvidia-cublas-cu12`/`nvidia-cudnn-cu12` wheels restored
  in `requirements.txt`.

## TUI keys

| Key | Action |
|---|---|
| `space` | toggle selection of the highlighted file |
| `s` / `a` | toggle subtitle / downmix for the file |
| `S` / `A` | toggle subtitle / downmix for every visible file |
| `f` | cycle filter: all / sub / audio / both / clean |
| `r` | toggle dry-run |
| `enter` | start the run |
| `esc` | back / cancel (finishes the current job) |

## Configuration

Copy `config.example.toml` to `config.toml`. Any key can be overridden with a
`MEDIAFIX_<KEY>` environment variable (e.g. `MEDIAFIX_MODEL=medium`), and most
have CLI flags. `audio_stream` defaults to auto-pick (English stereo first, then
default disposition); set it to pin a specific stream index.

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
tests/               75 unit, runner, and real-ffmpeg integration tests
```

Run tests: `python -m unittest discover -s tests -v`.