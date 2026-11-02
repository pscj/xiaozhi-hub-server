#!/bin/sh
set -eu
cd "$(dirname "$0")"
TASK_DIR="$(pwd -P)"
LAN_IP="${1:-$(uci -q get network.lan.ipaddr || true)}"
LAN_IP="${LAN_IP%%/*}"
case "$LAN_IP" in
  ''|*[!0-9.]*|0.0.0.0|127.*) echo '请指定路由器 LAN IPv4：sh start.sh 192.168.1.1' >&2; exit 1 ;;
esac
if docker inspect xiaozhi-hub >/dev/null 2>&1; then
  echo 'xiaozhi-hub 已存在；更新镜像后请按 README 备份并重建容器。' >&2
  exit 1
fi
mkdir -p data tmp home
chmod 700 data home
# No fixed memory cap: do not prevent upstream features from loading.
# Resource limits are optional and chosen by the operator.
set --
if [ -n "${HUB_MEMORY:-}" ]; then
  set -- --memory "$HUB_MEMORY"
fi
docker run -d \
  --name xiaozhi-hub \
  --restart unless-stopped \
  "$@" \
  --security-opt no-new-privileges \
  --log-driver json-file --log-opt max-size=5m --log-opt max-file=2 \
  -e "HUB_PUBLIC_HOST=$LAN_IP" \
  -e "HUB_DEFAULT_LLM=${HUB_DEFAULT_LLM:-ClaudeAPI}" \
  -p "$LAN_IP:16688:16688" \
  -p "$LAN_IP:16689:16689" \
  -v "$TASK_DIR/data:/opt/xiaozhi-hub/main/xiaozhi-server/data" \
  -v "$TASK_DIR/tmp:/opt/xiaozhi-hub/main/xiaozhi-server/tmp" \
  -v "$TASK_DIR/home:/home/xiaozhi" \
  -v xiaozhi-hub-models:/opt/xiaozhi-hub/main/xiaozhi-server/models \
  xiaozhi-hub-router:local
echo "管理后台：http://$LAN_IP:16688/admin"
echo "设备 OTA：http://$LAN_IP:16688/xiaozhi/ota/"
