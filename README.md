# mediafix

Finds and repairs two things missing from a media library:

1. **English subtitles** — transcribes the audio and writes a sidecar `.srt`
   (`<name>.eng.srt`) next to the video. Plex/Jellyfin pick these up automatically.
2. **English stereo downmix** — reuses the proven engine from
   `audio_downmix` to add a dialogue-boosted stereo track (with `dialoguenhance`
   and `loudnorm`) to surround films, rewritten in place atomically.

Both pipelines run together: the GPU transcriber works while CPU/IO workers
downmix in parallel.

## Requirements

- FFmpeg 6.0+ (for the `dialoguenhance` filter), ffprobe, MKVToolNix.
- Python 3.11+.
- Optional: NVIDIA GPU (compute type `float16`) for fast transcription; falls
  back to CPU with `--device cpu`.

## Quick start (local)

```bash
pip install -r requirements.txt
python -m mediafix check                     # verify tools + GPU + model
python -m mediafix scan ~/Videos             # report only, change nothing
python -m mediafix tui                       # interactive (default)
python -m mediafix apply ~/Videos -y         # batch, unattended
```

## Quick start (Docker, recommended for the Ubuntu box)

```bash
cp config.example.toml config.toml           # optional
MEDIA_ROOT=/mnt/Plex/TV docker compose up --build
```

The library is mounted read-write. The container runs as `${MEDIAFIX_UID}:${MEDIAFIX_GID}`
(set these to your host user's ids so files stay writable). Model weights live in
a named `hf-cache` volume, so they are downloaded once.

See `check_gpu.py`-style notes below for the P2000.

## P2000 / NVIDIA notes

- The P2000 is Pascal (`sm_61`) and is supported by CTranslate2.
- Keep the **Linux driver at 570.x or 580.x**. NVIDIA dropped Pascal in the 590+
  branch; a newer driver makes the card disappear.
- CUDA 12.8 needs driver ≥ 570.26.
- Default compute type is `float16`; set `compute_type = "float32"` if you hit
  numerical issues.
- Use `small` (default) or `medium`. `large-v3` on a P2000 is very slow.

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