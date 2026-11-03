# 小智 Hub：保留原项目功能的 ARM64 Docker 部署

此目录只负责容器化，不再使用之前的 API 精简方案。保留原项目代码及其全部已声明依赖，包括 PyTorch、torchaudio、FunASR、sherpa-onnx、ModelScope、Claude SDK、记忆模块和 MCP 等。
不修改模型路由、插件、工具调用、语音识别、合成、记忆和管理后台的实现；原项目支持的配置仍由用户选择。

## 与原项目保持一致的部分

- 新配置以 `scripts/config.template.yaml` 为基础，默认语音识别仍是本地 X-ASR；不会强制切换到豆包。
- 默认 X-ASR 下载与原生安装相同的五个文件，包含在镜像中。Silero VAD 使用仓库原有模型。
- 保留原模板的 Intent、Memory、TTS、日志等设置，不额外关闭工具、记忆或后台功能。
- 用户主要使用 API，因此首次配置默认 LLM 为原模板内的 `ClaudeAPI`，可在后台选择其他 API 或 ClaudeCLI；这只是初始选择，不限制其他功能。
- Claude SDK 已包含可执行 CLI，镜像将其暴露为 `claude`，支持在容器内登录；登录信息和工作目录持久化。
- 不再设置固定 384MiB 内存上限；如需限制，可由使用者通过 `HUB_MEMORY` 自行设置。
- 其他模块需要的专用模型、账号、API 密钥、外部服务仍按原项目配置，并非每个第三方模型和服务都预置在镜像内。

**功能保留不等于硬件足够。** JDCloud RE-CP-03 只有约 1GB 内存，同时运行本地模型、Claude Code 或较重记忆模块，可能耗尽路由器内存。应在实际设备上测量；不会通过删除功能来规避此限制。完整镜像和构建缓存也比旧精简版大。

## 1. 准备

先确认 `/mnt/data` 开机自动挂载，Docker 数据根目录是 `/mnt/data/docker`，存储驱动是 overlay2。
需要 ARM64 Docker、Git 和可用的网络；没有 Git 时也可下载仓库源码压缩包。

```sh
cd /mnt/data
git clone --depth 1 https://github.com/pscj/xiaozhi-hub-server.git
cd /mnt/data/xiaozhi-hub-server
sh deploy/router/build.sh
```

目录已存在时不要重复 clone。Python、系统依赖、CLI 和模型均装在镜像内，宿主机不需要 Python 或 Compose。
完整构建可能耗时较长，也可能超出路由器可用内存；这时可在另一台 ARM64 Linux 机器构建后，用 `docker save` / `docker load` 导入相同镜像，功能不变。

默认先从 Hugging Face 下载本地 X-ASR，重试失败或文件不完整时自动改用 `hf-mirror.com`。切换下载源会清理当前文件的未完成下载，避免混用两个来源的数据。显式指定其他 `HF_ENDPOINT` 时只使用指定源。

遇到模型下载 HTTP 503（下载服务暂时不可用）或下载较慢时，可直接指定镜像站重新构建：

```sh
HF_ENDPOINT=https://hf-mirror.com sh deploy/router/build.sh
```

重新构建时不要加 `--no-cache`，让 Docker 复用此前成功的依赖安装层。失败的模型下载步骤会重新执行；传统 Docker 构建不会保留失败层里的 `.part` 文件。

## 2. 启动

```sh
cd /mnt/data/xiaozhi-hub-server/deploy/router
sh start.sh
```

特殊网络配置下手动指定 LAN IPv4：

```sh
sh start.sh 192.168.1.1
```

首次初始化默认为 API 模型；如需要首次即使用 Claude Code：

```sh
HUB_DEFAULT_LLM=ClaudeCLI sh start.sh 192.168.1.1
```

此变量只影响新建配置，重启不覆盖已有配置、密码或 API Key。
端口仅发布到指定 LAN IPv4；服务仍只适用于可信局域网。Docker 隔离边界仍适用，Claude Code 只能访问容器内或明确挂载的文件。

容器先初始化挂载目录的属主，然后以普通用户 `xiaozhi`（UID/GID 1000）运行，支持 Claude Code 的原有权限模式。首次启动会将以下挂载内容改为该用户所有；旧版 root 属主的配置文件也能继续读取。

| 数据 | 宿主机 / Docker 卷 |
| --- | --- |
| 配置、聊天记录和后台数据 | `deploy/router/data` |
| 临时文件和应用日志 | `deploy/router/tmp` |
| Claude 登录、用户设置和 `~/xiaozhi-workspace` | `deploy/router/home` |
| 默认识别模型、其他模型和识别缓存 | 命名卷 `xiaozhi-hub-models` |

命名卷首次创建时由 Docker 复制镜像内的模型。以后更新镜像不会覆盖已有卷；新增或更新模型时按原项目说明管理卷内文件。

可选的资源上限（不指定则不增加容器内存上限）：

```sh
HUB_MEMORY=768m sh start.sh 192.168.1.1
```

这不是 1GB 路由器的推荐容量，仅示范参数格式。Compose 使用者可自行添加 `mem_limit`。

## 3. 配置和连接

打开启动脚本输出的后台地址，先设置密码，再按原项目说明配置模型、语音识别、音色、插件、工具和记忆。

```text
后台：http://路由器LAN-IP:16688/admin
OTA：http://路由器LAN-IP:16688/xiaozhi/ota/
WebSocket：ws://路由器LAN-IP:16689/xiaozhi/v1/
```

主要使用 API 时，在后台填写真实厂商、模型、地址和密钥即可；是否使用云端语音识别由你选择。
本地 X-ASR 不需要语音识别 API Key。模型支持、接口和第三方服务的限制与原项目相同。

Claude Code 登录：

```sh
docker exec -it -u xiaozhi -e HOME=/home/xiaozhi xiaozhi-hub claude
```

按 CLI 提示登录，然后在管理后台选择 ClaudeCLI。需要给 Agent 使用的项目，可放在持久化的 `deploy/router/home/xiaozhi-workspace`；其他宿主机目录需要自行增加 Docker 挂载。

## 4. 从之前的精简容器更新

先备份现有 `deploy/router/data`、`tmp` 和原镜像，再更新源码并构建成功，最后才替换旧容器：

```sh
cd /mnt/data/xiaozhi-hub-server
git pull --ff-only
sh deploy/router/build.sh
```

确认构建成功后：

```sh
docker stop xiaozhi-hub
docker rm xiaozhi-hub
cd /mnt/data/xiaozhi-hub-server/deploy/router
sh start.sh
```

原数据目录保持不变。为保护密码、API Key 和用户选择，已有配置不会自动恢复默认：若之前使用豆包 / 自定义 API，升级后仍保持该选择，需要在后台自行切换。
如果曾使用独立压缩包，数据可能在 `/mnt/data/xiaozhi-router/data`，应先备份并迁移到本仓库的 `deploy/router/data`，不要直接启动空配置覆盖原使用环境。

## 5. 日常管理

```sh
docker logs --tail 80 xiaozhi-hub
docker stats --no-stream xiaozhi-hub
docker inspect --format '{{.State.Health.Status}}' xiaozhi-hub
docker inspect --format '{{.State.OOMKilled}}' xiaozhi-hub
docker restart xiaozhi-hub
```

Healthcheck 只表示后台 HTTP 服务正常，不代表所有模型、API、音频设备和外部服务都已验证。
完整模型组合需要在目标设备验证延迟和内存。路由器透明代理也需要检查容器的实际联网情况。

Docker 部署通过更新源码、重建镜像和重建容器升级，不在运行中的镜像内执行原生系统服务安装或一键源码更新脚本。原生安装的开机自启由 Docker restart policy 替代。
不挂载宿主机 Docker socket，也不启用 privileged。宿主机设备、USB 等访问如有需要，应像其他 Docker 服务一样按实际模块配置。

## 6. 依赖与验证

`requirements-router.in` 直接引用原项目三份依赖清单，额外启用 HTTPX 的 SOCKS 支持。
重新生成 ARM64 / Python 3.10 锁定清单：

```sh
uv pip compile deploy/router/requirements-router.in \
  --python-version 3.10 --python-platform aarch64-manylinux_2_36 \
  -o deploy/router/requirements-router.txt
```

部分上游依赖（例如 jieba）只有源码分发，不能对整个依赖集合使用 `--only-binary :all:`。镜像包含编译工具，原生关键组件的 ARM64 wheel 可用性单独检查。
检查范围和未验证项目见 [VALIDATION.txt](VALIDATION.txt)。不把“依赖已声明”当作所有功能在目标硬件已实测通过。

## 7. 修复旧容器的唤醒词缓存权限错误

若日志出现 `PermissionError: config/assets/wakeup_words`，这是早期 Docker 启动脚本漏建可写缓存目录造成的，服务会在导入阶段退出。当前版本在切换普通用户前创建并赋权此目录，镜像构建也增加了普通用户读写检查。

已存在容器可以先更新启动文件，不必重新安装依赖或删除容器：

```sh
cd /mnt/data/xiaozhi-hub-server
git pull --ff-only
docker stop xiaozhi-hub
docker cp deploy/router/entrypoint.py xiaozhi-hub:/opt/router/entrypoint.py
docker start xiaozhi-hub
docker logs --tail 80 xiaozhi-hub
```

这只修复现有容器的启动文件，不改变镜像。之后重新构建镜像，确保未来新建容器也包含修复。配置、聊天记录、模型卷和 Claude 登录挂载保持不变。
