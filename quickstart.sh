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

[ "$(id -u)" -eq 0 ] && die "run this as your normal user, not root (it needs your uid for file ownership)"

say "checking prerequisites"
for tool in docker git; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed"
done
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    die "docker compose is not available (install the compose v2 plugin)"
fi
ok "$($COMPOSE version --short 2>/dev/null || echo docker-compose)"

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
    warn "media root '$MEDIA_ROOT' does not exist; edit MEDIA_ROOT in .env before scanning."
fi

if [ ! -f config.toml ]; then
    cp config.example.toml config.toml
    ok "created config.toml from config.example.toml"
else
    ok "config.toml already exists (left alone)"
fi

printf 'MEDIA_ROOT=%s\nMEDIAFIX_UID=%s\nMEDIAFIX_GID=%s\n' \
    "$MEDIA_ROOT" "$(id -u)" "$(id -g)" > .env
ok "wrote .env (MEDIA_ROOT=$MEDIA_ROOT, running as $(id -u):$(id -g))"

say "building the image (this downloads CUDA/Python layers on first run)"
$COMPOSE build
ok "image built"

say "verifying tools, GPU, model and a downmix roundtrip"
if $COMPOSE run --rm mediafix check; then
    ok "all checks passed"
else
    warn "some checks failed (see above). Scanning still works; only the failed part is unusable."
    warn "re-check later with: $COMPOSE run --rm mediafix check"
fi

cat <<EOF

$(printf '%s' "$GREEN")Ready.$(printf '%s' "$OFF")

  interactive  $COMPOSE run --rm mediafix tui
  report only  $COMPOSE run --rm mediafix scan
  unattended   $COMPOSE run --rm mediafix apply -y --only audio
  dry run      $COMPOSE run --rm mediafix apply --dry-run

Default model is 'small'. For hard-to-hear dialogue try:
  MEDIAFIX_MODEL=medium $COMPOSE run --rm mediafix tui

The library is mounted read-write at $MEDIA_ROOT. Keep the driver on
570.x/580.x or a P2000 will stop being detected.
EOF