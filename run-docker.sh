#!/usr/bin/env bash
set -euo pipefail

# Run mediafix without Docker Compose.
# Use this when the compose v2 plugin is not installed on the host.

cd "$(dirname "$0")"

IMAGE="${MEDIAFIX_IMAGE:-mediafix:latest}"
MEDIA_ROOT="${MEDIA_ROOT:-/mnt/Plex/TV}"
HF_DIR="${MEDIAFIX_HF_DIR:-$HOME/.cache/mediafix/hf}"

USE_GPU=1
[ "${MEDIAFIX_NO_GPU:-0}" = "1" ] && USE_GPU=0

if [ "$USE_GPU" = "1" ] && ! docker info 2>/dev/null | grep -qi nvidia; then
    printf 'warn: no NVIDIA runtime visible to docker; falling back to CPU transcription\n' >&2
    printf 'warn: install nvidia-container-toolkit to use the GPU\n' >&2
    USE_GPU=0
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    printf '==> building %s\n' "$IMAGE"
    docker build -t "$IMAGE" .
fi

mkdir -p "$HF_DIR"

GPU_ARGS=()
ENV_ARGS=()
if [ "$USE_GPU" = "1" ]; then
    GPU_ARGS=(--gpus all)
    ENV_ARGS=(
        -e NVIDIA_VISIBLE_DEVICES="${NVIDIA_VISIBLE_DEVICES:-0}"
        -e NVIDIA_DRIVER_CAPABILITIES=compute,utility
    )
else
    ENV_ARGS=(-e MEDIAFIX_DEVICE=cpu -e MEDIAFIX_COMPUTE_TYPE=int8)
fi

if [ "$#" -eq 0 ]; then
    set -- tui
fi

exec docker run --rm -it \
    "${GPU_ARGS[@]}" \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -e MEDIA_ROOT=/media \
    -e HF_HOME=/hf \
    "${ENV_ARGS[@]}" \
    -v "$MEDIA_ROOT":/media \
    -v "$HF_DIR":/hf \
    "$IMAGE" "$@"