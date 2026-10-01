#!/usr/bin/env bash
set -euo pipefail

# mediafix quickstart for an Ubuntu host with an NVIDIA P2000.
# Safe to re-run: every step checks before it changes anything.

cd "$(dirname "$0")"

BLUE=$'\033[1;34m'; GREEN=$'\033[1;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[1;31m'; OFF=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$BLUE" "$OFF" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '%swarn%s %s\n' "$YELLOW" "$OFF" "$*"; }
die()  { printf '%sfail%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then
    die "run this as your normal user, not root (it needs your uid for file ownership)"
fi

# Positional KEY=VALUE args override the environment.
for arg in "$@"; do
    case "$arg" in
        *=*) export "${arg%%=*}=${arg#*=}"; ok "override ${arg%%=*}=${arg#*=}" ;;
        *)   die "unexpected argument '$arg' (expected KEY=VALUE)" ;;
    esac
done

say "checking prerequisites"
for tool in docker git; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed"
done
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    COMPOSE=""
    warn "docker compose is not installed; using run-docker.sh instead."
    warn "to enable compose: sudo apt-get install -y docker-compose-plugin"
fi
if [ -n "$COMPOSE" ]; then
    ok "$($COMPOSE version --short 2>/dev/null || echo compose v1)"
    RUN="$COMPOSE run --rm mediafix"
else
    RUN="./run-docker.sh"
fi

if ! docker info 2>/dev/null | grep -qi nvidia; then
    warn "the NVIDIA container runtime does not appear to be wired into docker."
    warn "install nvidia-container-toolkit, or expect the GPU checks in 'check' to fail."
fi

if command -v nvidia-smi >/dev/null 2>&1; then
    GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || true)
    DRIVER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || true)
    ok "gpu: ${GPU:-unknown}  driver: ${DRIVER:-unknown}"
    case "${DRIVER%%.*}" in
        ""|0|1) ;;
        *)
            if [ "$(( ${DRIVER%%.*} ))" -ge 590 ]; then
                warn "driver ${DRIVER} dropped Pascal support; a P2000 disappears on 590+."
                warn "pin the 580.x branch or earlier before using the GPU."
            fi
            ;;
    esac
else
    warn "nvidia-smi not found; the container will fall back to CPU for transcription."
fi

say "configuring"
MEDIA_ROOT="${MEDIA_ROOT:-/mnt/Plex/TV}"
if [ ! -d "$MEDIA_ROOT" ]; then
    warn "media root '$MEDIA_ROOT' does not exist; edit MEDIA_ROOT before scanning."
fi

if [ ! -f config.toml ]; then
    cp config.example.toml config.toml
    ok "created config.toml from config.example.toml"
else
    ok "config.toml already exists (left alone)"
fi

if [ -n "$COMPOSE" ]; then
    # A bind mount (not a named volume) so the host uid owns the model cache.
    MEDIAFIX_HF_DIR="${MEDIAFIX_HF_DIR:-$HOME/.cache/mediafix/hf}"
    mkdir -p "$MEDIAFIX_HF_DIR"
    printf 'MEDIA_ROOT=%s\nMEDIAFIX_UID=%s\nMEDIAFIX_GID=%s\nMEDIAFIX_HF_DIR=%s\n' \
        "$MEDIA_ROOT" "$(id -u)" "$(id -g)" "$MEDIAFIX_HF_DIR" > .env
    ok "wrote .env (MEDIA_ROOT=$MEDIA_ROOT, running as $(id -u):$(id -g))"
fi
export MEDIA_ROOT

say "building the image (this downloads CUDA/Python layers on first run)"
if [ -n "$COMPOSE" ]; then
    $COMPOSE build
else
    docker build -t "${MEDIAFIX_IMAGE:-mediafix:latest}" .
fi
ok "image built"

say "verifying tools, GPU, model and a downmix roundtrip"
if $RUN check; then
    ok "all checks passed"
else
    warn "some checks failed (see above). Scanning still works; only the failed part is unusable."
    warn "re-check later with: $RUN check"
fi

cat <<EOF

$(printf '%s' "$GREEN")Ready.$(printf '%s' "$OFF")

  interactive  MEDIA_ROOT=$MEDIA_ROOT $RUN tui
  report only  MEDIA_ROOT=$MEDIA_ROOT $RUN scan
  unattended   MEDIA_ROOT=$MEDIA_ROOT $RUN apply -y --only audio
  dry run      MEDIA_ROOT=$MEDIA_ROOT $RUN apply --dry-run

Model cache: ${MEDIAFIX_HF_DIR:-$HOME/.cache/mediafix/hf}

Default model is 'small'. For hard-to-hear dialogue try:
  MEDIAFIX_MODEL=medium MEDIA_ROOT=$MEDIA_ROOT $RUN tui

The library is mounted read-write at $MEDIA_ROOT. Keep the driver on
570.x/580.x or a P2000 will stop being detected.
EOF