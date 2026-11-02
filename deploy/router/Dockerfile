FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 TZ=Asia/Shanghai \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    UV_CONCURRENT_DOWNLOADS=2 UV_CONCURRENT_INSTALLS=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg libopus0 libgomp1 tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY deploy/router/requirements-router.txt /opt/router/requirements.txt
RUN pip install --no-cache-dir uv \
    && uv pip install --system --no-cache -r /opt/router/requirements.txt

COPY main/xiaozhi-server/ /app/
COPY deploy/router/config.router.yaml deploy/router/entrypoint.py /opt/router/
EXPOSE 16688 16689
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:16688/admin', timeout=3).close()"
ENTRYPOINT ["python", "/opt/router/entrypoint.py"]
