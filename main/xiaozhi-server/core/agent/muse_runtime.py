"""One Muse registration for the gateway, shared by all provider event loops."""
import asyncio
import contextlib
from core.agent.muse_session import MuseBridge, MuseSession, configure_sdk_logging
from core.agent.agent_session import AgentSessionError, ErrorKind

_current = None


class Runtime:
    def __init__(self, enabled):
        configure_sdk_logging()
        self.loop = asyncio.get_running_loop()
        self.enabled = enabled
        self.bridge = MuseBridge()
        self.wake = asyncio.Event()
        self.state = {'state': 'starting', 'error': ''}
        self.task = asyncio.create_task(self.run())

    async def run(self):
        from core.agent import muse_pair_bridge
        try:
            while True:
                self.wake.clear()
                try:
                    from musegadget import config
                    paired = bool(config.load_json(config.PAIRING_FILE))
                    if muse_pair_bridge.pairing_active() or not self.enabled or not paired:
                        await self.bridge.stop()
                        state = 'pairing' if muse_pair_bridge.pairing_active() else ('disabled' if not self.enabled else 'unpaired')
                        self.state = {'state': state, 'error': ''}
                    else:
                        self.bridge.launch()
                        session = self.bridge.session
                        online = bool(session and session.registered.is_set())
                        self.state = {'state': 'online' if online else 'connecting',
                                      'error': '' if online else self.bridge.error}
                except ImportError:
                    self.state = {'state': 'unavailable', 'error': '未安装 Muse Gadget SDK'}
                except Exception:
                    self.state = {'state': 'error', 'error': '无法启动 Muse，请检查凭证、状态目录及服务端日志'}
                try:
                    await asyncio.wait_for(self.wake.wait(), 1)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.bridge.stop()

    async def pause(self):
        await self.bridge.stop()
        self.state = {'state': 'pairing', 'error': ''}

    async def close(self):
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task


def start(enabled):
    global _current
    if _current is None:
        _current = Runtime(enabled)
    return _current


async def close():
    global _current
    if _current:
        await _current.close()
        _current = None


def changed(enabled=None):
    runtime = _current
    if runtime:
        def update():
            if enabled is not None:
                runtime.enabled = enabled
            runtime.wake.set()
        runtime.loop.call_soon_threadsafe(update)


def snapshot():
    return dict(_current.state) if _current else {'state': 'stopped', 'error': 'Muse 注册服务尚未启动'}


async def pause():
    if _current:
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(_current.pause(), _current.loop))


class RuntimeSession:
    """Keep SDK objects on the runtime loop; provider loops only forward calls."""
    def __init__(self, bridge, session_id=None, voice_hint='', device_key=''):
        self.runtime = _current
        if self.runtime is None:
            raise AgentSessionError(ErrorKind.SERVER, 'Muse 注册服务尚未启动')
        self.session = MuseSession(self.runtime.bridge, session_id, voice_hint, device_key)

    @property
    def alive(self):
        return self.session.alive

    @property
    def session_id(self):
        return self.session.session_id

    async def _call(self, coroutine):
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coroutine, self.runtime.loop))

    async def start(self):
        if not self.runtime.enabled:
            raise AgentSessionError(ErrorKind.AUTH, 'Muse 未启用')
        await self._call(self.session.start())

    async def interrupt(self):
        await self._call(self.session.interrupt())

    async def close(self):
        await self._call(self.session.close())

    async def send(self, text):
        if not self.runtime.enabled:
            raise AgentSessionError(ErrorKind.AUTH, "Muse 未启用")
        stream = self.session.send(text)
        try:
            while True:
                try:
                    event = await self._call(stream.__anext__())
                except StopAsyncIteration:
                    break
                yield event
        finally:
            # Close on the owner loop after cancellation of an in-flight next().
            async def cleanup():
                while stream.ag_running:
                    await asyncio.sleep(.01)
                await stream.aclose()
            await self._call(cleanup())
