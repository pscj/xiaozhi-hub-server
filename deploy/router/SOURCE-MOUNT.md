# 挂载源码更新服务

在服务器的仓库目录操作。首次使用新版脚本时，它会备份原容器的数据、保留旧容器，
然后用原镜像创建带源码挂载的新容器；不需要重新构建镜像：

```sh
cd /mnt/data/xiaozhi-hub-server
git pull --ff-only
sh deploy/router/redeploy.sh
```

之后每次更新，仍执行以上两条命令。宿主机的 `main/xiaozhi-server` 以只读方式映射到
容器的 `/opt/xiaozhi-hub/main/xiaozhi-server`。代码修改后必须重启服务；脚本会重建容器
以保持备份及失败回滚流程。数据、模型、临时目录和用户目录使用独立的持久化挂载；
唤醒词缓存使用 `deploy/router/wakeup_words` 可写目录。

源码映射不会更新镜像内的 Python 包、系统库、模型或 `/opt/router` 启动脚本。
这些内容变化时，执行下面的命令构建新镜像并重新部署：

```sh
sh deploy/router/redeploy.sh --build
```

首次创建容器仍会构建镜像，可提供局域网地址：

```sh
sh deploy/router/redeploy.sh 192.168.1.1
```

源码映射不是快照；更新或修改宿主机文件会影响所有引用该目录的容器。回滚到旧版本时，
需要同时把宿主机源码切换到相应版本。容器恢复不自动回滚宿主机源码。
