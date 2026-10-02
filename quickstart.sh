#!/usr/bin/env bash
set -euo pipefail

# mediafix quickstart for an Ubuntu host. Transcription runs on CPU (int8).
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

ok "transcription device: cpu (int8)"

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

say "building the image (this downloads Python layers on first run)"
if [ -n "$COMPOSE" ]; then
    $COMPOSE build
else
    docker build -t "${MEDIAFIX_IMAGE:-mediafix:latest}" .
fi
# Stamp so run-docker.sh does not immediately rebuild what we just built.
touch .build-hash
ok "image built"

say "verifying tools, ctranslate2, model and a downmix roundtrip"
if $RUN check; then
    ok "all checks passed"
else
    warn "some checks failed (see above). Scanning still works; only the failed part is unusable."
    warn "re-check later with: $RUN check"
fi

if [ -n "$COMPOSE" ]; then
    REBUILD_HINT="  $COMPOSE build"
else
    REBUILD_HINT="  run-docker.sh rebuilds automatically when sources change."
fi

cat <<EOF

$(printf '%s' "$GREEN")Ready.$(printf '%s' "$OFF")

  interactive  MEDIA_ROOT=$MEDIA_ROOT $RUN tui
  report only  MEDIA_ROOT=$MEDIA_ROOT $RUN scan
  unattended   MEDIA_ROOT=$MEDIA_ROOT $RUN apply -y --only audio
  dry run      MEDIA_ROOT=$MEDIA_ROOT $RUN apply --dry-run

Model cache: ${MEDIAFIX_HF_DIR:-$HOME/.cache/mediafix/hf}

After a git pull, rebuild once before running - 'compose run' will not do it:
${REBUILD_HINT}

Default model is 'small'. For hard-to-hear dialogue try:
  MEDIAFIX_MODEL=medium MEDIA_ROOT=$MEDIA_ROOT $RUN tui

The library is mounted read-write at $MEDIA_ROOT. Transcription is CPU-only
(int8); expect it to be slower than a GPU, especially on 'medium' or 'large-v3'.
EOF