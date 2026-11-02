FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 TZ=Asia/Shanghai \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    UV_CONCURRENT_DOWNLOADS=2 UV_CONCURRENT_INSTALLS=1 \
    HUB_SERVER_DIR=/opt/xiaozhi-hub/main/xiaozhi-server

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg libopus0 libgomp1 libsndfile1 libpq5 tzdata \
    git curl bash build-essential pkg-config libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /bin/bash xiaozhi

COPY deploy/router/requirements-router.txt /opt/router/requirements.txt
RUN pip install --no-cache-dir uv \
    && uv pip install --system --no-cache -r /opt/router/requirements.txt \
    && python -c "import pathlib,claude_agent_sdk; p=pathlib.Path(claude_agent_sdk.__file__).parent/'_bundled'/'claude'; assert p.is_file(); pathlib.Path('/usr/local/bin/claude').symlink_to(p)" \
    && claude --version

WORKDIR /opt/xiaozhi-hub/main/xiaozhi-server
COPY main/xiaozhi-server/ ./
COPY scripts/ /opt/xiaozhi-hub/scripts/
COPY hub-docs/ /opt/xiaozhi-hub/hub-docs/
COPY deploy/router/config.router.yaml deploy/router/entrypoint.py deploy/router/download_models.py /opt/router/
ARG HF_ENDPOINT=https://huggingface.co
RUN HF_ENDPOINT="$HF_ENDPOINT" python /opt/router/download_models.py

EXPOSE 16688 16689
HEALTHCHECK --interval=60s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:16688/admin', timeout=3).close()"
ENTRYPOINT ["python", "/opt/router/entrypoint.py"]
