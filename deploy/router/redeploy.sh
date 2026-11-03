#!/bin/sh
# Rebuild the checked-out code; preserve the existing router deployment's state.
set -eu
umask 077
TASK_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
REPO_ROOT=$(CDPATH= cd -- "$TASK_DIR/../.." && pwd -P)
cd "$REPO_ROOT"
REBUILD=0
if [ "${1:-}" = "--build" ]; then REBUILD=1; shift; fi
case "${1:-}" in
  --help|-h) echo '用法：sh deploy/router/redeploy.sh [--build] [首次部署的 LAN-IP]'; echo '先 git pull --ff-only，再执行本脚本；已有容器自动沿用原地址及持久化挂载。'; exit 0 ;;
esac
TIMEOUT=${HUB_DEPLOY_TIMEOUT:-300}
case "$TIMEOUT" in ''|*[!0-9]*|0) echo 'HUB_DEPLOY_TIMEOUT 必须是正整数秒' >&2; exit 1 ;; esac
command -v docker >/dev/null 2>&1 || { echo '未安装 Docker' >&2; exit 1; }
docker info >/dev/null || { echo 'Docker 未运行，或当前用户没有 Docker 权限' >&2; exit 1; }
LOCK="$TASK_DIR/.redeploy.lock"
mkdir "$LOCK" 2>/dev/null || { echo '已有部署任务运行；确认没有任务后才能移除 deploy/router/.redeploy.lock' >&2; exit 1; }
OLD_MOVED=0
SUCCESS=0
WAS_RUNNING=false
OLD_NAME="xiaozhi-hub-update-$(date +%Y%m%d-%H%M%S)-$$"
IMAGE="xiaozhi-hub-router:deploy-$(date +%Y%m%d-%H%M%S)-$$"
cleanup() {
  result=$?
  trap - EXIT HUP INT TERM
  if [ "$OLD_MOVED" = 1 ] && [ "$SUCCESS" = 0 ]; then
    echo '部署未完成，正在恢复原容器……' >&2
    docker rm -f xiaozhi-hub >/dev/null 2>&1 || true
    if docker rename "$OLD_NAME" xiaozhi-hub; then
      if [ "$WAS_RUNNING" = true ]; then docker start xiaozhi-hub >/dev/null || true; fi
      echo '原容器已恢复；请检查日志后重试。备份和构建镜像均已保留。' >&2
    else
      echo "自动恢复失败；原容器名：$OLD_NAME，请手动恢复。" >&2
    fi
  elif [ "$SUCCESS" = 0 ] && [ "$HAD_OLD" = 0 ]; then
    # Only remove a container created by this invocation.
    if [ "$NEW_CREATED" = 1 ]; then docker rm -f xiaozhi-hub >/dev/null 2>&1 || true; fi
  fi
  rmdir "$LOCK" 2>/dev/null || true
  exit "$result"
}
HAD_OLD=0
NEW_CREATED=0
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
# Docker cannot create nested mount points after the source tree is mounted read-only.
SOURCE_DIR="$REPO_ROOT/main/xiaozhi-server"
[ -f "$SOURCE_DIR/app.py" ] && [ -d "$SOURCE_DIR/config/assets" ] || { echo '找不到完整的服务端源码，请检查仓库目录' >&2; exit 1; }
mkdir -p "$SOURCE_DIR/data" "$SOURCE_DIR/tmp" "$SOURCE_DIR/models" "$SOURCE_DIR/config/assets/wakeup_words"
mkdir -p "$TASK_DIR/backups" "$TASK_DIR/data" "$TASK_DIR/tmp" "$TASK_DIR/home" "$TASK_DIR/wakeup_words"
chmod 700 "$TASK_DIR/backups"
BACKUP=$(mktemp -d "$TASK_DIR/backups/$(date +%Y%m%d-%H%M%S)-XXXXXX")
if docker inspect xiaozhi-hub >"$BACKUP/container.json" 2>/dev/null; then
  HAD_OLD=1
  WAS_RUNNING=$(docker inspect --format '{{.State.Running}}' xiaozhi-hub)
else
  printf '[]\n' >"$BACKUP/container.json"
fi
LAN_IP=${1:-${LAN_IP:-}}
if [ -z "$LAN_IP" ] && command -v uci >/dev/null 2>&1; then LAN_IP=$(uci -q get network.lan.ipaddr || true); fi
LAN_IP=${LAN_IP%%/*}
if [ "$HAD_OLD" = 1 ] && [ "$REBUILD" = 0 ]; then
  IMAGE=$(docker inspect --format '{{.Image}}' xiaozhi-hub)
  echo '1/5 沿用现有镜像，使用宿主机源码挂载。'
else
  echo '1/5 构建镜像和依赖；此时不停止旧容器……'
  DOCKER_BUILDKIT=0 docker build -f deploy/router/Dockerfile \
    --build-arg "HF_ENDPOINT=${HF_ENDPOINT:-https://huggingface.co}" -t "$IMAGE" .
fi
# Let the image's Python safely quote existing Docker parameters for the host shell.
# No host Python, Compose or Docker socket mount is required.
docker run --rm -i --network none --mount "type=bind,source=$TASK_DIR/redeploy_plan.py,target=/tmp/redeploy_plan.py,readonly" --entrypoint python "$IMAGE" \
  /tmp/redeploy_plan.py "$IMAGE" "$TASK_DIR" "$BACKUP" "$LAN_IP" \
  <"$BACKUP/container.json" >"$BACKUP/plan.sh"
. "$BACKUP/plan.sh"
echo '2/5 停止旧容器，备份配置、聊天、Claude 登录和 Muse 配对数据……'
if [ "$HAD_OLD" = 1 ]; then
  # Rename first so every later failure can recover the old container.
  docker rename xiaozhi-hub "$OLD_NAME"
  OLD_MOVED=1
  docker stop --time 30 "$OLD_NAME" >/dev/null
fi
backup_data
echo '3/5 创建并启动新容器，沿用原端口、环境变量、资源限制和数据挂载……'
create_container >/dev/null
NEW_CREATED=1
docker start xiaozhi-hub >/dev/null
echo "4/5 等待后台 HTTP 就绪（最多 $TIMEOUT 秒）……"
deadline=$(( $(date +%s) + TIMEOUT ))
while ! docker exec xiaozhi-hub python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:16688/admin', timeout=3).close()" >/dev/null 2>&1; do
  if [ "$(docker inspect --format '{{.State.Running}}' xiaozhi-hub)" != true ] || [ "$(date +%s)" -ge "$deadline" ]; then
    echo "新容器未就绪；退出原因请查看日志。备份：$BACKUP" >&2
    docker logs --tail 80 xiaozhi-hub >&2 || true
    exit 1
  fi
  sleep 5
done
if [ "$REBUILD" = 1 ] || [ "$HAD_OLD" = 0 ]; then docker tag "$IMAGE" xiaozhi-hub-router:local; fi
SUCCESS=1
echo '5/5 部署完成。'
if [ "$HAD_OLD" = 1 ]; then
  # Retain this previous container. Never remove volumes or prune images here.
  echo "上一个容器（已停止）：$OLD_NAME"
fi
echo "数据备份：$BACKUP"
echo '查看日志：docker logs --tail 80 xiaozhi-hub'
echo 'HTTP 就绪不代表 Muse 已配对；首次接入仍需完成配对并启用 LLM.Muse。'
