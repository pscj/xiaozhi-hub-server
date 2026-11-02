# 小智 Hub：ARM64 路由器 Docker 部署

针对当前 JDCloud RE-CP-03 / ImmortalWrt / Docker 20.10 的部署配置。
基于现有 Hub 源码运行，建议将仓库放在 `/mnt/data/xiaozhi-hub-server`。Docker 数据根目录需先设置为 `/mnt/data/docker`，并确认 `/mnt/data` 开机自动挂载。

## 功能和资源

- 大模型：OpenAI 兼容 API，可在后台更换厂商、地址、模型和密钥。
- 语音识别：默认使用上游支持的豆包流式识别，在后台填识别密钥。
- 语音合成：EdgeTTS；需要网络连通，不需要在此模板中填写密钥。
- 保留小型 Silero VAD 判断说话和停顿；不安装 PyTorch、本地 X-ASR、FunASR、sherpa-onnx。
- 保留管理后台和必要的共享 Python 依赖。包括上游共享组件需要导入的 Claude SDK，但不配置或启动 Claude Code。
- 容器内存上限 384MiB；实际内存、性能和兼容性需在目标路由器验证。内存不足会导致容器重启，不要立即放开上限挤占路由功能。
- 本部署目录不提供已构建镜像。首次需要联网下载 ARM64 Python 基础镜像、Debian 软件包和 Python 依赖，再在路由器构建。

大模型 API 与语音识别 API 是不同服务。只有聊天模型密钥，不能完成语音转文字。

## 1. 获取源码和构建镜像

路由器需要 Docker、Git 和可用的外网连接。没有 Git 时可以下载此仓库的源码压缩包，解压到相同目录。
在路由器 SSH 中执行（目录已存在时不要重复 clone）：

```sh
cd /mnt/data
git clone --depth 1 https://github.com/pscj/xiaozhi-hub-server.git
cd /mnt/data/xiaozhi-hub-server
sh deploy/router/build.sh
```

脚本实际运行的是：

```sh
docker build -f deploy/router/Dockerfile -t xiaozhi-hub-router:local .
```

Python、ffmpeg 和 Python 依赖均安装在镜像中，宿主机不需要安装 Python、uv 或 Compose。
构建在前台运行；若 SSH 断开，应检查镜像是否成功构建，再决定重跑构建脚本。
构建失败时保留最后 50 行报错，不要使用上游 `docker-setup.sh` 混装。

## 2. 启动

```sh
cd /mnt/data/xiaozhi-hub-server/deploy/router
sh start.sh
```

脚本从 `network.lan.ipaddr` 读取局域网 IPv4。特殊网络配置下手动指定：

```sh
sh start.sh 192.168.1.1
```

端口只发布到指定的 LAN IPv4，不使用 host 网络、不映射 WAN 地址、不使用 privileged。
Docker 数据目录保持为 `/mnt/data/docker`；本项目的配置和聊天记录在 `deploy/router/data`。
临时音频和应用日志在 `deploy/router/tmp`，Docker 自身日志限制为两份、每份 5MB。

首次启动创建 `data/.config.yaml`；后续重启不覆盖里面的密码和密钥。
路由器 IP 变更后需修改此配置中的 websocket、vision_explain，并重建容器的端口绑定。

## 3. 网页配置

使用脚本输出的实际路由器 IP 打开：

```text
http://192.168.1.1:16688/admin
```

1. 先设置管理密码。
2. 在“大模型”选择所用厂商或“自定义”，填写真实 API 地址、模型名称和 Key，并选为使用中的模型。
   模板 `api.example.com` 只是占位地址，必须替换；大模型地址通常填服务商文档提供的兼容 API 根地址。
3. 在“语音识别”使用豆包流式识别，填与已开通语音识别资源对应的 API Key，或按后台要求填写旧版 App ID / Access Token。
   模板资源 ID 为 `volc.seedasr.sauc.duration`，请确保与你实际开通的资源一致。
4. 在“语音合成”选择音色并测试 EdgeTTS。
5. 检查后台状态，再连接设备。

首次没有填 Key 时出现配置错误提示是预期行为，此时不能语音对话。不要把密钥发到聊天里。
本镜像没有本地识别组件，后台不要切换到本地 X-ASR，也不要选用 Claude Code。
后台中的插件可以按需配置；默认 Intent 是 nointent，主要用于基础 API 对话。

## 4. 设备连接

设备 OTA 地址（替换 IP，保留末尾斜杠）：

```text
http://192.168.1.1:16688/xiaozhi/ota/
```

若客户端直接填写 WebSocket 地址：

```text
ws://192.168.1.1:16689/xiaozhi/v1/
```

后台支持浏览器访问不代表设备协议、录音、回声消除和网络都已通过测试；需在实际 ThinkSmart 客户端验证完整对话。
只在可信局域网使用，不做公网端口转发。上游的设备身份认证有限，后台管理密码不等于语音连接认证。

## 5. 可选：换成其他兼容语音转写 API

在路由器上编辑：

```sh
vi /mnt/data/xiaozhi-hub-server/deploy/router/data/.config.yaml
```

把 `selected_module.ASR` 改成 `RouterTranscriptionASR`，然后修改同名条目的：

- `base_url`：完整 multipart 音频转写接口 URL，例如服务商的 `/v1/audio/transcriptions`。
- `api_key`：该识别服务的 Key。
- `model_name`：支持音频转写的模型，不能填写普通聊天模型名称。

保存后执行：

```sh
docker restart xiaozhi-hub
```

原后台的语音识别页主要支持豆包和本地识别，这个替代条目需要手动编辑。
当前兼容转写 Provider 为上游实现；具体服务商的响应格式和端到端延迟需实际测试。

## 6. 管理和排障

```sh
docker ps -a --filter name=xiaozhi-hub
docker logs --tail 80 xiaozhi-hub
docker stats --no-stream xiaozhi-hub
docker inspect --format '{{.State.Health.Status}}' xiaozhi-hub
docker inspect --format '{{.State.OOMKilled}}' xiaozhi-hub
docker restart xiaozhi-hub
docker stop xiaozhi-hub
docker start xiaozhi-hub
```

Healthcheck 只检测后台 HTTP 服务，不代表 ASR / LLM / TTS 的密钥和网络已正常。
日志里的配置错误请先检查后台；如日志含密钥或用户文字，分享报错前自行遮盖。
Docker 透明代理和宿主机代理的效果可能不同，需测试容器的实际外网连接。

路由器重启后 `/mnt/data` 必须先挂载、Docker 再启动。如果未自动启动，先确认挂载，再重启 dockerd。

## 7. 更新和移除

不要使用后台的一键源码更新或原生安装脚本更新容器。镜像使用构建时的仓库源码和已锁定的 Python 依赖。
先备份 `deploy/router/data` 和原镜像，再更新仓库、重新构建镜像，最后重建容器；挂载同一个 `data` 保留配置和历史。不要只重启旧容器就认为镜像已更新。
删除服务只需 `docker stop xiaozhi-hub` 后 `docker rm xiaozhi-hub`；这不会删除宿主机 data。
无需卸载 Docker，不要删除 `/mnt/data/docker`，里面还有原来的 MiGPT 数据。

## 验证范围

检查结果见 [VALIDATION.txt](VALIDATION.txt)。未在你的 ARM64 路由器上执行 Docker 构建或实际语音对话；需要你运行后回传构建或启动结果。

## 8. 重新锁定 Python 依赖（开发者）

仅在升级依赖时执行；这一步需要开发机安装 uv，路由器部署不需要：

```sh
uv pip compile deploy/router/requirements-router.in \
  -c main/xiaozhi-server/requirements.txt \
  --python-version 3.10 --python-platform aarch64-manylinux_2_36 \
  --only-binary :all: -o deploy/router/requirements-router.txt
```

升级后重新验证 ARM64 依赖和服务启动。共享后台组件仍导入 Claude SDK，因此保留此 Python 依赖；默认配置不会启动 Claude Code。
