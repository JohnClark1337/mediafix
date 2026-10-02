#!/usr/bin/env bash
set -euo pipefail

# Run mediafix without Docker Compose.
# Use this when the compose v2 plugin is not installed on the host.

cd "$(dirname "$0")"

IMAGE="${MEDIAFIX_IMAGE:-mediafix:latest}"
MEDIA_ROOT="${MEDIA_ROOT:-/mnt/Plex/TV}"
HF_DIR="${MEDIAFIX_HF_DIR:-$HOME/.cache/mediafix/hf}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    printf '==> building %s\n' "$IMAGE"
    docker build -t "$IMAGE" .
fi

mkdir -p "$HF_DIR"

if [ "$#" -eq 0 ]; then
    set -- tui
fi

exec docker run --rm -it \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -e MEDIA_ROOT=/media \
    -e HF_HOME=/hf \
    -e MEDIAFIX_DEVICE=cpu \
    -e MEDIAFIX_COMPUTE_TYPE=int8 \
    -v "$MEDIA_ROOT":/media \
    -v "$HF_DIR":/hf \
    "$IMAGE" "$@"