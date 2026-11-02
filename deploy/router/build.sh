#!/bin/sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"
# Docker 20.10: buildx / Compose are not required.
DOCKER_BUILDKIT=0 docker build -f deploy/router/Dockerfile \
  --build-arg "HF_ENDPOINT=${HF_ENDPOINT:-https://huggingface.co}" \
  -t xiaozhi-hub-router:local .
