FROM nvidia/cuda:12.8.1-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
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

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin mediafix \
    && mkdir -p /media /home/mediafix/.cache/huggingface \
    && chown -R mediafix:mediafix /app /media /home/mediafix

USER mediafix

ENTRYPOINT ["python3", "-m", "mediafix"]
CMD ["tui"]