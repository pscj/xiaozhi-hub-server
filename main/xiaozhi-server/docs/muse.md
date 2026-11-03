# Muse 接入

链路：安卓现有录音 → 网关 ASR → Muse Gadget SDK（Noise 加密）→ Muse 回复文字 → 网关现有 TTS → 安卓播放。安卓无需安装额外 SDK 或填写 Muse 凭证；管理 HTTP 地址配置正确后，在模型列表选择 Muse。

使用官方 [Muse Gadget SDK](https://github.com/facebookincubator/muse-gadget-sdk)，固定版本 `b166d88102a3e542c9666743dcb113e32ee35047`。Android 示例是视频测试工具，此处使用 Linux SDK。桥接只注册空命令列表，不调用官方 Executor，不开放主机执行命令或读写文件能力。

## 安装与配对

运行主机需要 Python 3.10+、Linux、可用的 BLE 蓝牙适配器及 BlueZ。获取自己的 [SDK Token](https://gadgets.muse.ai/settings/sdk-tokens)，阅读 [Gadget SDK Terms](https://gadgets.muse.ai/sdk-terms)。不要将 Token 提交到仓库。

在运行网关的 Python 环境安装：

```sh
python -m pip install -r requirements-muse.txt
```

配对依赖系统 `python3-dbus`、`python3-gi`、BlueZ。虚拟环境需使用系统 Python 并启用 `--system-site-packages`。按官方 Linux 指引将 `/etc/bluetooth/main.conf` 的 `[GATT] ExchangeMTU` 设为 `256`，避免手机长消息配对失败。

用与网关相同的运行用户和状态目录操作（目录仅该用户可读写）：

```sh
export MUSEGADGET_STATE_DIR=/你的私有持久化目录/musegadget
# 将 SDK Token 保存为状态目录下的 sdk_token 文件，权限 0600；或通过服务私有环境注入 MUSEGADGET_SDK_TOKEN。
musegadget pair
```

手机 Muse：Settings → Devices → Developer mode → Add Device，选择终端打印的 `MuseGadgetXXXXXX`，确认社区设备并完成配对。Linux 已联网时选择显示的当前连接。`musegadget info` 可检查状态，但输出可能涉及凭证，不要公开。

网关服务必须设置同一个 `MUSEGADGET_STATE_DIR`，并能读取其中的身份、配对和 SDK Token。不要同时运行官方 `musegadget run` 服务：本 provider 已经持有唯一的注册连接，两个服务会竞争注册。配对命令不会常驻注册。

Docker 环境建议在宿主 Linux 完成配对，将私有状态目录以可写卷挂载进容器（SDK 会刷新凭证），并设置状态目录变量；容器内也要安装 `requirements-muse.txt`。无需将蓝牙或 D-Bus 暴露给运行中的网关。新版 router 镜像已安装此依赖，拉取代码后执行 `sh deploy/router/redeploy.sh` 重建即可；脚本默认将 Muse 状态目录设为持久化的 `data/musegadget`，已有自定义持久化目录会沿用。

## 启用

主配置已提供默认关闭的 `LLM.Muse` 示例，配对后将 `enabled` 设为 `true`。已有配置或管理后台覆盖配置的部署可在 `data/.config.yaml` 的 `LLM` 下添加：

```yaml
LLM:
  Muse:
    type: muse
    enabled: true
    display_name: Muse
    turn_timeout_seconds: 600
    idle_timeout_seconds: 300
    voice_hint: "回复会被语音朗读，请简短口语化。"
```

重启网关后，安卓「选择模型」中选择 Muse。要作为默认模型，将 `selected_module.LLM` 设为 `Muse`，`selected_module.Intent` 设为 `nointent`。Memory/Intent 的其他模型调用仍需独立 API provider。只配置一个 Muse 类型条目，同一进程共享一条注册连接；不要多进程共享同一配对身份。

## 行为与限制

- 每个安卓设备有自己的 Muse side chat，ID 存入 `data/agent_sessions.json` 的 `muse` 命名空间，重连继续该会话。
- 先订阅回复，再发消息；HTTP ACK 仅表示收件，不当作答案。只朗读可关联到本次请求、已完成的 assistant 回复，忽略其他聊天和无关联的回放。若服务器只返回无父关联的文本，该文本不会被播报。
- 多设备的 Muse 请求串行执行；排队时可以打断，尚未发送的问题不会在离开后补发。单轮默认最多等待 600 秒。
- 打断会停止本地等待和播报。官方文字接口没有云端任务取消能力，Muse 可能继续工作；迟到回复不自动播报，后续提问仍在该设备的会话中。
- 使用现有 ASR/TTS，而非 Muse 实时语音或 Muse 音色；图片、视频、主动推送和远程设备工具尚未接入。
- 清除网关记录不会删除 Muse 云端聊天。重新配对另一账号前，应备份并移除 `agent_sessions.json` 中的 `muse` 命名空间，避免继续引用旧账号聊天 ID。
- 缺 SDK、未配对、断网会返回中文提示。SDK 自动重连和刷新配对凭证，不使用账号 owner token 降级。

## 验收

先用文字问答确认回复关联，再测试按钮语音、唤醒、播报中打断、网络恢复、服务重启续聊及两台设备上下文隔离。自动化测试使用官方 MuseTurn 与假的传输，不访问真实账号；真实配对和语音端到端仍需现场验证。
