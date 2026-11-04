"""启动问候只发一次，并且必须等待设备就绪、空闲。"""

import asyncio
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core.agent.startup_greeting import StartupGreeting


def connection(device="dev-1", ready=True):
    event = asyncio.Event()
    if ready:
        event.set()
    return SimpleNamespace(
        device_id=device, components_ready=event, stop_event=threading.Event(),
        need_bind=False, client_have_voice=False, client_is_speaking=False,
        calling=False, logger=Mock(),
    )


def speaker(monkeypatch):
    speak = Mock()
    monkeypatch.setitem(sys.modules, "core.agent.reports", SimpleNamespace(speak=speak))
    return speak


def test_once_per_device_until_restart(monkeypatch):
    speak = speaker(monkeypatch)

    async def run():
        greeting = StartupGreeting()
        await greeting.notify(connection())
        await greeting.notify(connection())  # 同一设备重连
        await greeting.notify(connection("dev-2"))
        assert speak.call_count == 2
        await StartupGreeting().notify(connection())  # 重启后的新实例
        assert speak.call_count == 3
        assert all(call.args[1] == "我准备好了" for call in speak.call_args_list)

    asyncio.run(run())


def test_waits_for_initialization_and_idle(monkeypatch):
    speak = speaker(monkeypatch)

    async def run():
        conn = connection(ready=False)
        conn.client_have_voice = True
        task = asyncio.create_task(StartupGreeting().notify(conn))
        await asyncio.sleep(0)
        speak.assert_not_called()
        conn.components_ready.set()
        await asyncio.sleep(0.01)
        speak.assert_not_called()
        conn.client_have_voice = False
        await asyncio.wait_for(task, timeout=1)
        speak.assert_called_once_with(conn, "我准备好了")

    asyncio.run(run())


def test_offline_and_unbound_devices_are_skipped(monkeypatch):
    speak = speaker(monkeypatch)

    async def run():
        greeting = StartupGreeting()
        offline = connection()
        offline.stop_event.set()
        await greeting.notify(offline)
        unbound = connection()
        unbound.need_bind = True
        await greeting.notify(unbound)
        speak.assert_not_called()
        await greeting.notify(connection())
        speak.assert_called_once()

    asyncio.run(run())


def test_cancelled_initialization_can_retry_on_reconnect(monkeypatch):
    speak = speaker(monkeypatch)

    async def run():
        greeting = StartupGreeting()
        task = asyncio.create_task(greeting.notify(connection(ready=False)))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        speak.assert_not_called()
        await greeting.notify(connection())
        speak.assert_called_once()

    asyncio.run(run())


def test_failed_greeting_can_retry(monkeypatch):
    speak = speaker(monkeypatch)
    speak.side_effect = [RuntimeError("TTS unavailable"), None]

    async def run():
        greeting = StartupGreeting()
        await greeting.notify(connection())
        await greeting.notify(connection())
        assert speak.call_count == 2

    asyncio.run(run())


def test_hello_is_sent_before_greeting_and_duplicate_hello_is_ignored():
    from core.handle.helloHandle import handleHelloMessage

    async def run():
        conn = connection()
        conn.welcome_msg = {"type": "hello"}
        conn.websocket = SimpleNamespace(send=AsyncMock())
        conn._startup_greeting_task = None

        async def notify(client):
            client.websocket.send.assert_awaited()

        notify_mock = AsyncMock(side_effect=notify)
        conn.server = SimpleNamespace(startup_greeting=SimpleNamespace(notify=notify_mock))
        await handleHelloMessage(conn, {})
        await conn._startup_greeting_task
        await handleHelloMessage(conn, {})
        notify_mock.assert_awaited_once_with(conn)

    asyncio.run(run())


def test_components_ready_only_after_successful_initialization(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    # 初始化状态测试不需要加载新闻等外部插件。
    with monkeypatch.context() as patch:
        patch.setattr("plugins_func.loadplugins.auto_import_modules", lambda *args: None)
        from core.connection import ConnectionHandler

    async def run():
        for result in (True, None):
            conn = connection(ready=False)
            conn._initialize_private_config_async = AsyncMock()
            conn._initialize_components = Mock(return_value=result)
            with ThreadPoolExecutor(max_workers=1) as executor:
                conn.executor = executor
                await ConnectionHandler._background_initialize(conn)
            assert conn.components_ready.is_set() == bool(result)

    asyncio.run(run())
