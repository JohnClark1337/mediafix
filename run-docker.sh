#!/usr/bin/env bash
set -euo pipefail

# Run mediafix without Docker Compose.
# Use this when the compose v2 plugin is not installed on the host.

cd "$(dirname "$0")"

IMAGE="${MEDIAFIX_IMAGE:-mediafix:latest}"
MEDIA_ROOT="${MEDIA_ROOT:-/mnt/Plex/TV}"
HF_DIR="${MEDIAFIX_HF_DIR:-$HOME/.cache/mediafix/hf}"

build() {
    printf '==> building %s\n' "$IMAGE"
    docker build -t "$IMAGE" .
    # Stamp only on success, so a failed build retries next time.
    touch .build-hash
}

# Rebuild whenever the inputs change, not just when the tag is missing.
# mediafix/ is COPYed into the image, so a stale tag silently runs old code.
NEEDS_BUILD=0
case "${MEDIAFIX_REBUILD:-0}" in
    1) NEEDS_BUILD=1 ;;
    0) ;;
    *) printf 'warn: MEDIAFIX_REBUILD must be 0 or 1, got %s\n' "${MEDIAFIX_REBUILD}" >&2 ;;
esac

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    # No such image (never built, or removed with `docker rmi`).
    NEEDS_BUILD=1
elif [ ! -e .build-hash ]; then
    NEEDS_BUILD=1
elif [ -n "$(find . -maxdepth 2 -name '*.py' -newer .build-hash -not -path './.git/*' -print -quit)" ] \
  || [ -n "$(find . -maxdepth 1 \( -name Dockerfile -o -name requirements.txt \) -newer .build-hash -print -quit)" ]; then
    # Any source file newer than the stamp means the image is out of date.
    NEEDS_BUILD=1
fi

if [ "$NEEDS_BUILD" = "1" ]; then
    build
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