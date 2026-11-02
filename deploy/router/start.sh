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
  echo 'xiaozhi-hub 已存在。查看日志：docker logs --tail 80 xiaozhi-hub' >&2
  echo '启动现有容器：docker start xiaozhi-hub；重启：docker restart xiaozhi-hub' >&2
  exit 1
fi
mkdir -p data tmp
chmod 700 data
docker run -d \
  --name xiaozhi-hub \
  --restart unless-stopped \
  --memory 384m --memory-swap 384m \
  --cap-drop ALL --security-opt no-new-privileges \
  --log-driver json-file --log-opt max-size=5m --log-opt max-file=2 \
  -e "HUB_PUBLIC_HOST=$LAN_IP" \
  -p "$LAN_IP:16688:16688" \
  -p "$LAN_IP:16689:16689" \
  -v "$TASK_DIR/data:/app/data" \
  -v "$TASK_DIR/tmp:/app/tmp" \
  xiaozhi-hub-router:local
echo "管理后台：http://$LAN_IP:16688/admin"
echo "设备 OTA：http://$LAN_IP:16688/xiaozhi/ota/"
