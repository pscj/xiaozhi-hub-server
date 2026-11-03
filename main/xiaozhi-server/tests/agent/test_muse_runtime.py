"""Runtime lifecycle uses real SDK Service with a fake cloud link."""
import asyncio
import time
from types import SimpleNamespace
import pytest

pytest.importorskip('musegadget')
from core.agent import muse_runtime, muse_pair_bridge


@pytest.fixture
def cloud(monkeypatch):
    from musegadget import config, identity, muse_api, service
    monkeypatch.setattr(asyncio, 'sleep', asyncio.tasks.sleep)
    monkeypatch.setattr(muse_runtime, '_current', None)
    monkeypatch.setattr(muse_pair_bridge, '_reserved', False)
    monkeypatch.setattr(muse_pair_bridge, '_current', None)
    state = SimpleNamespace(paired=True, links=[], acknowledge=False, reject=False)
    monkeypatch.setattr(config, 'load_json', lambda _: {'access_token': 'fake', 'refresh_token': 'fake', 'access_token_saved_at': time.time()} if state.paired else None)
    monkeypatch.setattr(config, 'sdk_token', lambda: None)
    monkeypatch.setattr(identity, 'load_or_create', lambda: SimpleNamespace(node_id='test-node'))
    monkeypatch.setattr(muse_api, 'fetch_vms_with_status', lambda *args: ([{'vm_id': 'vm', 'vm_name': 'vm', 'vm_auth_token': 'fake', 'is_default': True}], 200))

    class Link:
        def __init__(self, **kwargs):
            self.device = kwargs['device']
            self.registered = asyncio.Event()
            self.registered_at = None
            self.closed = False
            state.links.append(self)
        async def run(self, stop):
            try:
                if state.reject:
                    return service.Outcome.FORBIDDEN
                while not state.acknowledge:
                    await asyncio.sleep(.01)
                self.registered_at = time.monotonic()
                self.registered.set()
                await stop.wait()
                return service.Outcome.STOPPED
            finally:
                self.closed = True
                self.registered.clear()
        async def wait_registered(self, timeout):
            await asyncio.wait_for(self.registered.wait(), timeout)
    monkeypatch.setattr(service, 'LinkSession', Link)
    return state


async def until(predicate):
    async def wait():
        while not predicate():
            muse_runtime.changed()
            await asyncio.sleep(.01)
    await asyncio.wait_for(wait(), 3)


async def test_muse_restores_registration_without_a_conversation(cloud):
    runtime = muse_runtime.start(True)
    try:
        await until(lambda: len(cloud.links) == 1)
        assert muse_runtime.snapshot()['state'] != 'online'
        cloud.acknowledge = True
        await until(lambda: muse_runtime.snapshot()['state'] == 'online')
        assert cloud.links[0].device.commands == {}
        assert muse_runtime.start(True) is runtime
        await runtime.bridge.start()
        assert len(cloud.links) == 1
    finally:
        await muse_runtime.close()
    assert cloud.links[0].closed


async def test_muse_pairing_success_and_cancel_resume_registration(cloud):
    cloud.paired = False
    cloud.acknowledge = True
    muse_runtime.start(True)
    try:
        await until(lambda: muse_runtime.snapshot()['state'] == 'unpaired')
        muse_pair_bridge.reserve()
        await muse_runtime.pause()
        cloud.paired = True
        muse_pair_bridge.release()  # Same wakeup path as completed().
        await until(lambda: muse_runtime.snapshot()['state'] == 'online')
        first = cloud.links[0]
        muse_pair_bridge.reserve()
        await muse_runtime.pause()
        assert first.closed
        assert muse_runtime.snapshot()['state'] == 'pairing'
        muse_pair_bridge.release()  # Cancel retains the previous credentials.
        await until(lambda: len(cloud.links) == 2 and muse_runtime.snapshot()['state'] == 'online')
    finally:
        muse_pair_bridge.release()
        await muse_runtime.close()


async def test_muse_disabled_and_rejected_registration(cloud):
    muse_runtime.start(False)
    try:
        await until(lambda: muse_runtime.snapshot()['state'] == 'disabled')
        assert not cloud.links
        cloud.reject = True
        muse_runtime.changed(enabled=True)
        await until(lambda: bool(muse_runtime.snapshot()['error']))
        assert muse_runtime.snapshot()['state'] != 'online'
        assert '拒绝' in muse_runtime.snapshot()['error']
        muse_runtime.changed(enabled=False)
        await until(lambda: muse_runtime.snapshot()['state'] == 'disabled')
    finally:
        await muse_runtime.close()


async def test_muse_provider_loop_reuses_runtime_and_cleans_cancelled_stream(cloud, monkeypatch):
    from core.agent.muse_session import MuseSession
    cloud.acknowledge = True
    runtime = muse_runtime.start(True)
    owner_loop = asyncio.get_running_loop()
    closed = asyncio.Event()
    async def send(session, text):
        assert asyncio.get_running_loop() is owner_loop
        try:
            yield text
            await asyncio.Event().wait()
        finally:
            closed.set()
    monkeypatch.setattr(MuseSession, 'send', send)
    def provider_thread():
        async def work():
            session = muse_runtime.RuntimeSession(None, 'chat')
            await session.start()
            stream = session.send('hello')
            assert await stream.__anext__() == 'hello'
            pending = asyncio.create_task(stream.__anext__())
            await asyncio.sleep(.05)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await session.close()
        asyncio.run(work())
    try:
        await asyncio.wait_for(asyncio.to_thread(provider_thread), 3)
        await asyncio.wait_for(closed.wait(), 1)
        assert len(cloud.links) == 1
        assert runtime.bridge.task and not runtime.bridge.task.done()
    finally:
        await muse_runtime.close()
