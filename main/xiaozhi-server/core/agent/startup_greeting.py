"""每次服务启动后，向每台就绪的设备问候一次。"""

import asyncio


class StartupGreeting:
    def __init__(self):
        # 只保存在本次服务进程内，重启后重新问候。
        self._notified = set()

    async def notify(self, conn):
        device_id = conn.device_id
        if not device_id or device_id in self._notified:
            return
        try:
            await asyncio.wait_for(conn.components_ready.wait(), timeout=60)
            while not conn.stop_event.is_set():
                if device_id in self._notified or conn.need_bind:
                    return
                # 避开用户正在说话、正在播放回复和通话的时段。
                if not (conn.client_have_voice or conn.client_is_speaking or conn.calling):
                    from core.agent.reports import speak

                    speak(conn, "我准备好了")
                    self._notified.add(device_id)
                    return
                await asyncio.sleep(0.2)
        except asyncio.TimeoutError:
            conn.logger.bind(tag=__name__).warning("组件尚未就绪，跳过启动问候")
        except Exception as exc:
            conn.logger.bind(tag=__name__).warning(f"启动问候失败: {exc}")
