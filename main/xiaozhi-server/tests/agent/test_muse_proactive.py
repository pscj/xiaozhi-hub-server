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


async def test_marker_found_in_deltas_when_final_text_lacks_it(inbox):
    # 实测回归：定时消息的最终文本（content/display_text）丢了开头的播报标记，
    # 增量里却完整——两个来源都查标记，带标记的来源优先。
    link = WatchLink()
    bridge = make_bridge(link)
    bridge.ensure_watcher("chat", "device-1")
    await link.queue.put(event("delta.text_append", "chat", text="【播报】叮，时间到了"))
    await link.queue.put(event("delta.message_done", "chat", content="叮，时间到了"))
    await asyncio.sleep(1.2)
    assert inbox == [("device-1", "muse", "叮，时间到了")]
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


# ---------- 单一订阅泵：回合与 watcher 共用一条订阅流 ----------

class PumpLink(WatchLink):
    """带订阅计数的假传输，send_chat 后把预置事件推入订阅队列。"""

    def __init__(self, events=None):
        super().__init__()
        self.events = events or []
        self.sent = []
        self.subscriptions = 0

    async def subscribe_chat(self, chat):
        self.subscriptions += 1
        async for value in super().subscribe_chat(chat):
            yield value

    async def send_chat(self, text, chat, output_modality):
        self.sent.append((text, chat, output_modality))
        for raw in self.events:
            await self.queue.put(raw)
        return {"ok": True, "status": 200,
                "response": {"message_id": "u1", "session_id": chat, "is_thread": True}}


class PumpBridge:
    """回合侧替身：锁和 start 自己提供，订阅泵三件套委托给真正的 MuseBridge。"""

    def __init__(self, link):
        self.lock = asyncio.Lock()
        self.link = link
        self.real = MuseBridge()
        self.real.session = link

    async def start(self):
        return self.link

    def listen(self, session_id):
        return self.real.listen(session_id)

    def unlisten(self, session_id, queue):
        self.real.unlisten(session_id, queue)

    async def wait_ready(self, session_id, timeout=15):
        return await self.real.wait_ready(session_id, timeout)

    def claim(self, session_id, message_id):
        self.real.claim(session_id, message_id)

    def consumed_ids(self, session_id):
        return self.real.consumed_ids(session_id)

    def ensure_watcher(self, session_id, device_key):
        self.real.ensure_watcher(session_id, device_key)


async def test_turn_and_watcher_share_one_subscription(inbox):
    link = PumpLink([
        event("delta.message_done", "chat", parent="u1", message="r1", content="你好。"),
        event("task.status", "chat", message="task", status="completed"),
    ])
    bridge = PumpBridge(link)
    session = MuseSession(bridge, "chat", voice_hint="", device_key="device-1")
    await session.start()  # 武装 watcher（订阅泵的第一位听众）
    events = [ev async for ev in session.send("你好")]
    assert events[-1].__class__.__name__ == "Done"
    # 回合的回复已被认领，watcher 不重复汇报；整个过程只开了一条订阅流
    await link.queue.put(event("delta.message_done", "chat", parent="external-user",
                               message="r9", content="【播报】泵测试消息"))
    await asyncio.sleep(1.2)
    assert inbox == [("device-1", "muse", "泵测试消息")]
    assert link.subscriptions == 1
    await bridge.real.stop()


async def test_pump_first_send_creates_missing_chat():
    from musegadget.link_client import HttpStreamError

    class NewChatLink(PumpLink):
        def __init__(self):
            super().__init__([
                event("delta.message_done", "chat", parent="u1", content="你好"),
                event("task.status", "chat", message="task", status="completed"),
            ])
            self.created = False

        async def subscribe_chat(self, chat):
            if not self.created:
                raise HttpStreamError(404, "/chat/subscribe")
            async for value in super().subscribe_chat(chat):
                yield value

        async def send_chat(self, text, chat, output_modality):
            self.created = True
            return await super().send_chat(text, chat, output_modality)

    link = NewChatLink()
    bridge = PumpBridge(link)
    session = MuseSession(bridge, "chat", voice_hint="")

    async def collect_all():
        return [ev async for ev in session.send("你好")]

    events = await asyncio.wait_for(collect_all(), 10)
    assert [e.text for e in events if e.__class__.__name__ == "TextDelta"] == ["你好\n"]
    assert events[-1].__class__.__name__ == "Done"
    await bridge.real.stop()
