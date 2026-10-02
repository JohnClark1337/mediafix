# CPU-only build: no CUDA base image and no nvidia-* pip wheels. Transcription
# runs through CTranslate2's CPU backend (int8), so no GPU runtime is needed.
# The base is 24.04 regardless of the host distro; ffmpeg 6.1 is required for
# the dialoguenhance filter that audio_downmix depends on.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/mediafix \
    HF_HOME=/home/mediafix/.cache/huggingface

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv ffmpeg mkvtoolnix ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -hide_banner -filters 2>/dev/null | grep -q " dialoguenhance " \
    && mkvmerge --version > /dev/null

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY audio_downmix.py ./
COPY mediafix/ ./mediafix/

# ubuntu:24.04 (and anything derived from it) already ships an "ubuntu" user
# at UID 1000, so APP_UID must not collide with it.
ARG APP_UID=10001
ARG APP_GID=10001

RUN set -eux; \
    groupadd --gid "${APP_GID}" mediafix; \
    useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home \
            --shell /usr/sbin/nologin mediafix; \
    mkdir -p /media /home/mediafix/.cache/huggingface; \
    chown -R mediafix:mediafix /app /media /home/mediafix; \
    # compose and run-docker.sh override the runtime uid with the host user's
    # so files written to /media keep their ownership. That uid must still be
    # able to traverse into the cache directory, which means /home/mediafix
    # itself needs o+x - chmodding only .cache left it unreachable and
    # makedirs failed with Permission denied on the parent.
    chmod a+x /home/mediafix /home/mediafix/.cache; \
    chmod -R a+rwX /home/mediafix/.cache

USER mediafix

ENTRYPOINT ["python3", "-m", "mediafix"]
CMD ["tui"]