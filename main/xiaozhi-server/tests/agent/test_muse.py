"""使用真实 SDK 回复关联器与假传输，不需要 Muse 账号。"""
import asyncio

import pytest

pytest.importorskip("musegadget")
from core.agent.muse_session import MuseBridge, MuseSession
from core.agent.agent_session import AgentSessionError, Done, Error, ErrorKind, TextDelta


@pytest.fixture(autouse=True)
def real_async_sleep(monkeypatch):
    # 全局 conftest 的 HTTP mock 将 asyncio.sleep 替成不让出循环的函数；
    # 传输并发测试必须使用真实调度。
    monkeypatch.setattr(asyncio, "sleep", asyncio.tasks.sleep)


def event(kind, chat, parent="u1", message="r1", **fields):
    return {"type": "event", "event": kind, "payload": {
        "session_id": chat, "message_id": message,
        "reply_to_message_id": parent, **fields}}


class FakeLink:
    def __init__(self, events=None, fail=False):
        self.chat_subscribed = asyncio.Event()
        self.events = events or []
        self.sent = []
        self.fail = fail
        self.closed = False
        self.queue = asyncio.Queue()

    async def subscribe_chat(self, chat):
        if self.fail:
            raise ConnectionError("synthetic secret")
        self.chat_subscribed.set()
        try:
            while True:
                yield await self.queue.get()
        finally:
            self.closed = True
            self.chat_subscribed.clear()

    async def send_chat(self, text, chat, output_modality):
        assert self.chat_subscribed.is_set()
        self.sent.append((text, chat, output_modality))
        for raw in self.events:
            await self.queue.put(raw)
        return {"ok": True, "status": 200, "response": {"message_id": "u1", "session_id": chat, "is_thread": True}}


class FakeBridge:
    def __init__(self, link):
        self.lock = asyncio.Lock()
        self.link = link

    async def start(self):
        return self.link


async def collect(session):
    return [ev async for ev in session.send("你好")]


async def test_reply_before_ack_and_other_chats_are_filtered():
    link = FakeLink([
        event("delta.message_done", "other", content="别人的回复"),
        event("delta.message_done", "chat", parent="old", message="old", content="上一轮迟到"),
        event("delta.message_done", "chat", content="你好。"),
        event("task.status", "chat", message="task", status="completed"),
    ])
    session = MuseSession(FakeBridge(link), "chat", voice_hint="")
    events = await asyncio.wait_for(collect(session), 5)
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["你好。\n"]
    assert isinstance(events[-1], Done)
    assert link.sent == [("你好", "chat", "text")]
    assert link.closed and not session.bridge.lock.locked()


async def test_interrupt_queue_does_not_send_and_releases_lock():
    link = FakeLink()
    bridge = FakeBridge(link)
    await bridge.lock.acquire()
    session = MuseSession(bridge, "chat")
    task = asyncio.create_task(collect(session))
    await asyncio.sleep(.02)
    await session.interrupt()
    events = await asyncio.wait_for(task, 1)
    assert isinstance(events[-1], Done) and events[-1].interrupted
    assert not link.sent
    assert bridge.lock.locked()  # 属于前一调用的锁没有被错误释放
    bridge.lock.release()


async def test_interrupt_after_send_closes_subscription():
    link = FakeLink()
    session = MuseSession(FakeBridge(link), "chat")
    task = asyncio.create_task(collect(session))
    while not link.sent:
        await asyncio.sleep(.01)
    await session.interrupt()
    events = await asyncio.wait_for(task, 1)
    assert events[-1].interrupted and link.closed
    assert not session.bridge.lock.locked()


async def test_subscription_failure_does_not_send_or_leak_error_text():
    link = FakeLink(fail=True)
    session = MuseSession(FakeBridge(link), "chat")
    events = await asyncio.wait_for(collect(session), 1)
    assert isinstance(events[-1], Error)
    assert events[-1].detail == ""
    assert not link.sent and not session.bridge.lock.locked()


async def test_no_pairing_is_auth_error(monkeypatch):
    from musegadget import config
    monkeypatch.setattr(config, "load_json", lambda _: None)
    with pytest.raises(AgentSessionError) as exc:
        await MuseBridge().start()
    assert exc.value.kind == ErrorKind.AUTH


async def test_interrupt_before_iteration_does_not_send():
    link = FakeLink()
    session = MuseSession(FakeBridge(link), "chat")
    await session.interrupt()
    events = await collect(session)
    assert events[-1].interrupted and not link.sent


async def test_cancel_releases_lock_and_subscription():
    link = FakeLink()
    session = MuseSession(FakeBridge(link), "chat")
    task = asyncio.create_task(collect(session))
    while not link.sent:
        await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert link.closed and not session.bridge.lock.locked()


async def test_wrong_chat_ack_is_rejected():
    class WrongChat(FakeLink):
        async def send_chat(self, text, chat, output_modality):
            return {"ok": True, "status": 200, "response": {"message_id": "u1", "session_id": "other", "is_thread": True}}
    session = MuseSession(FakeBridge(WrongChat()), "chat")
    events = await collect(session)
    assert isinstance(events[-1], Error)
    assert not any(isinstance(ev, TextDelta) for ev in events)


async def test_provider_startup_abort_never_sends(monkeypatch, tmp_path):
    import queue
    from types import SimpleNamespace
    from core.providers.llm.muse import muse
    from core.agent.session_store import SessionStore
    class PausedSession:
        def __init__(self, *args):
            self.closed = False
        async def start(self):
            state.abort_gen = state.turn_gen
        async def close(self):
            self.closed = True
    monkeypatch.setattr(muse, "MuseSession", PausedSession)
    provider = muse.LLMProvider.__new__(muse.LLMProvider)
    provider.bridge = FakeBridge(FakeLink())
    provider.voice_hint = ""
    provider.store = SessionStore(tmp_path / "sessions.json", "muse")
    state = SimpleNamespace(agent=None, key="device", turn_gen=1, closed=False)
    output = queue.Queue()
    assert await provider._ensure_agent(state, "你好", output, "")
    assert output.get_nowait().interrupted and state.agent is None


async def test_bridge_registers_no_host_commands(monkeypatch):
    from types import SimpleNamespace
    from musegadget import config, identity
    from musegadget.service import Service
    monkeypatch.setattr(config, "load_json", lambda _: {"access_token": "synthetic"})
    monkeypatch.setattr(config, "sdk_token", lambda: None)
    monkeypatch.setattr(identity, "load_or_create", lambda: SimpleNamespace(node_id="homelink-abcdef"))
    bridge = MuseBridge()
    async def run(service):
        registered = asyncio.Event()
        registered.set()
        bridge.session = SimpleNamespace(registered=registered)
        await asyncio.Event().wait()
    monkeypatch.setattr(Service, "run", run)
    try:
        await bridge.start()
        description = bridge.service.describe()
        assert description.commands == {}
        assert bridge.service.executor is None
        assert bridge.service.run_command("system.run", {})["ok"] is False
        assert bridge.service.run_command("file.read", {})["ok"] is False
    finally:
        bridge.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bridge.task
