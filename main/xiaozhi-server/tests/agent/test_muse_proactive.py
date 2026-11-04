"""常驻订阅（主动消息）测试：假传输 + 捕获待汇报箱入箱，不需要 Muse 账号。"""
import asyncio

import pytest

pytest.importorskip("musegadget")
from core.agent import muse_session, reports
from core.agent.muse_session import MuseBridge, MuseSession


@pytest.fixture(autouse=True)
def real_async_sleep(monkeypatch):
    # 同 test_muse.py：全局 conftest 的 HTTP mock 把 asyncio.sleep 替成了不让出
    # 循环的函数，订阅/组装这类并发测试必须用真实调度。
    monkeypatch.setattr(asyncio, "sleep", asyncio.tasks.sleep)


def event(kind, chat, parent="external-user", message="r1", **fields):
    return {"type": "event", "event": kind, "payload": {
        "session_id": chat, "message_id": message,
        "reply_to_message_id": parent, **fields}}


class WatchLink:
    """常驻订阅用的假 LinkSession：registered 常亮，事件从队列来。"""

    def __init__(self):
        self.registered = asyncio.Event()
        self.registered.set()
        self.chat_subscribed = asyncio.Event()
        self.queue = asyncio.Queue()

    async def subscribe_chat(self, chat):
        self.chat_subscribed.set()
        try:
            while True:
                yield await self.queue.get()
        finally:
            self.chat_subscribed.clear()


class TurnLink(WatchLink):
    """回合用的假传输：send_chat 后把预置事件推入订阅队列。"""

    def __init__(self, events=None):
        super().__init__()
        self.events = events or []
        self.sent = []

    async def send_chat(self, text, chat, output_modality):
        self.sent.append((text, chat, output_modality))
        for raw in self.events:
            await self.queue.put(raw)
        return {"ok": True, "status": 200,
                "response": {"message_id": "u1", "session_id": chat, "is_thread": True}}


@pytest.fixture(autouse=True)
def fast_grace(monkeypatch):
    monkeypatch.setattr(muse_session, "_PROACTIVE_GRACE_SECONDS", 0.3)


@pytest.fixture
def inbox(monkeypatch):
    got = []
    monkeypatch.setattr(reports, "add", lambda key, bot_id, text: got.append((key, bot_id, text)))
    return got


def make_bridge(link):
    bridge = MuseBridge()
    bridge.session = link
    return bridge


async def test_proactive_message_goes_to_reports(inbox):
    link = WatchLink()
    bridge = make_bridge(link)
    bridge.ensure_watcher("chat", "device-1")
    await link.queue.put(event("delta.text_append", "chat", text="【播报】闹钟"))
    await link.queue.put(event("delta.message_done", "chat", content="【播报】闹钟时间到啦"))
    await asyncio.sleep(1.2)
    assert inbox == [("device-1", "muse", "闹钟时间到啦")]
    await bridge.stop()


async def test_streamed_text_is_assembled_without_final_content(inbox):
    link = WatchLink()
    bridge = make_bridge(link)
    bridge.ensure_watcher("chat", "device-1")
    await link.queue.put(event("delta.text_append", "chat", text="【播报】你好"))
    await link.queue.put(event("delta.text_append", "chat", text="世界"))
    await link.queue.put(event("delta.message_done", "chat"))
    await asyncio.sleep(1.2)
    assert inbox == [("device-1", "muse", "你好世界")]
    await bridge.stop()


async def test_unmarked_proactive_message_is_not_reported(inbox):
    link = WatchLink()
    bridge = make_bridge(link)
    bridge.ensure_watcher("chat", "device-1")
    await link.queue.put(event("delta.message_done", "chat", content="普通对话回复，不带播报标记"))
    await asyncio.sleep(1.2)
    assert inbox == []
    await bridge.stop()


async def test_claimed_turn_reply_is_not_reported(inbox):
    link = WatchLink()
    bridge = make_bridge(link)
    bridge.claim("chat", "r1")  # 本轮回复，回合已认领
    bridge.ensure_watcher("chat", "device-1")
    await link.queue.put(event("delta.message_done", "chat", content="本轮回复"))
    await asyncio.sleep(1.2)
    assert inbox == []
    await bridge.stop()


async def test_user_and_other_chat_events_are_ignored(inbox):
    link = WatchLink()
    bridge = make_bridge(link)
    bridge.ensure_watcher("chat", "device-1")
    await link.queue.put(event("delta.message_done", "chat", role="user", content="用户的话"))
    await link.queue.put(event("delta.message_done", "other-chat", content="别人的回复"))
    await asyncio.sleep(1.2)
    assert inbox == []
    await bridge.stop()


async def test_watcher_reconnects_after_subscription_ends(inbox):
    class DropOnce(WatchLink):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def subscribe_chat(self, chat):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("dropped")
            async for value in super().subscribe_chat(chat):
                yield value

    link = DropOnce()
    bridge = make_bridge(link)
    bridge.ensure_watcher("chat", "device-1")
    await asyncio.sleep(0.3)
    await link.queue.put(event("delta.message_done", "chat", content="【播报】重连后的消息"))
    await asyncio.sleep(4)  # 订阅失败有 2 秒重连退避，消息在队列里等重订阅后投递
    assert inbox == [("device-1", "muse", "重连后的消息")]
    await bridge.stop()


async def test_turn_claims_reply_and_seeds_retirements():
    class StubBridge:
        def __init__(self, link):
            self.lock = asyncio.Lock()
            self.link = link
            self.claimed = []
            self.watched = []

        async def start(self):
            return self.link

        def ensure_watcher(self, session_id, key):
            self.watched.append((session_id, key))

        def claim(self, session_id, message_id):
            self.claimed.append((session_id, message_id))

        def consumed_ids(self, session_id):
            return {"old-proactive"}

    link = TurnLink([
        event("delta.message_done", "chat", parent="u1", content="你好。"),
        event("task.status", "chat", message="task", status="completed"),
    ])
    bridge = StubBridge(link)
    session = MuseSession(bridge, "chat", voice_hint="", device_key="device-1")
    await session.start()
    assert bridge.watched == [("chat", "device-1")]
    events = [ev async for ev in session.send("你好")]
    assert bridge.claimed == [("chat", "r1")]
    assert ("message", "old-proactive") in session._replay_scope.ids
    assert events[-1].__class__.__name__ == "Done"
