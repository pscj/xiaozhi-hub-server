# 拉取代码后，一条命令重新部署

适用于本仓库 `deploy/router` 创建的标准 Docker 部署，不要求宿主机安装 Python 或 Compose。

```sh
cd /mnt/data/xiaozhi-hub-server
sh deploy/router/redeploy.sh
```

每次拉取完代码都执行这条命令。脚本部署当前工作目录中的源码，不会自动拉取 Git，也不会重置本地修改。第一次使用脚本文件前，先执行 `git pull --ff-only`。

首次安装、没有现成容器时，需提供 LAN IPv4（OpenWrt 会尝试从 UCI 读取）：

```sh
sh deploy/router/redeploy.sh 192.168.1.1
```

已有 `xiaozhi-hub` 容器时，会沿用它的 IP、端口、环境变量、日志参数、常用资源限制和持久化挂载；位置不需要与当前源码目录相同。自定义网络、非标准入口、设备直通，以及未持久化 data/home 等情况会停止自动部署，不删除原容器。建议在路由器 root 终端执行，以便读取和备份旧数据。

## 脚本会做什么

1. 检查 Docker，获取部署锁，记录旧容器配置。
2. 构建当前代码及依赖，包括 Muse Gadget SDK；构建失败时旧容器继续运行。
3. 停止并保留旧容器，将 data 和 home 做离线 tar 备份，包含配置、数据库、Claude 登录，以及位于这些目录内的 Muse 配对文件。
4. 新容器复用原数据和模型卷。源码使用新镜像里的版本，不再挂载宿主机 core 目录；以后修改源码需再次执行部署脚本。
5. 默认等待约 300 秒检查容器内 `/admin` HTTP 服务。失败时移除本次新容器，并恢复旧容器名称和运行状态；成功后更新 `xiaozhi-hub-router:local` 标签。

可选参数：

```sh
# 模型下载镜像站，以及更长的启动等待窗口
HF_ENDPOINT=https://hf-mirror.com HUB_DEPLOY_TIMEOUT=600 sh deploy/router/redeploy.sh
```

HTTP 成功只表示后台已启动，不代表 ASR、TTS、Muse 账号或真实语音链路已验证。脚本不会覆盖已有配置、自动填写 API Key、开启 Muse 或替你完成手机配对。Muse 配对与启用仍按[接入说明](../../main/xiaozhi-server/docs/muse.md)操作。

## 备份与恢复

每次备份写入 `deploy/router/backups/<时间>-<随机值>`，权限仅当前用户访问，已从 Git 和镜像构建上下文排除。里面的 `container.json`、`plan.sh` 可能含凭证，不要公开。备份包括 data/home；大体积模型卷和其他自定义挂载不会复制，原挂载仍保留。

旧容器保留为终端输出的 `xiaozhi-hub-update-...` 名称，镜像和历史备份不自动清理。确认新版正常后，可按终端记录手动移除对应的旧容器、旧镜像和过期备份，勿删除模型卷。

自动恢复只恢复旧容器及其运行状态，不会把数据库恢复为升级前版本。若未来版本迁移了数据结构，需要停止容器，再根据本次 data/home 备份人工恢复。若旧容器曾挂载工作目录的 core 源码，恢复后仍读取该工作目录当前版本，不能视为完整代码回滚。

构建成功前不会主动停止旧容器；若旧容器挂载了仓库 core，之前执行的 `git pull` 已改变该挂载内容，构建期间应避免继续发起对话。

同一部署目录不允许并发执行。中断或失败会释放锁；若被强制断电留下 `.redeploy.lock`，确认没有部署进程后手动移除该空目录，再重试。

查看日志：

```sh
docker logs --tail 80 xiaozhi-hub
```
