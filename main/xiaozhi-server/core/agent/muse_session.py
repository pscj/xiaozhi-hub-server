"""官方 Muse Gadget SDK 的文字会话适配；音频继续由网关 ASR/TTS 处理。"""
import asyncio
import contextlib
import logging
import traceback
import time
import uuid

from core.agent.agent_session import (
    Activity, AgentSession, AgentSessionError, Done, Error, ErrorKind, TextDelta,
)


from loguru import logger


class MuseSdkLogHandler(logging.Handler):
    def emit(self, record):
        logger.bind(tag=record.name).log(record.levelname if record.levelname in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else "INFO", "{}", self.format(record))


def configure_sdk_logging():
    sdk_logger = logging.getLogger("musegadget")
    if not any(isinstance(handler, MuseSdkLogHandler) for handler in sdk_logger.handlers):
        sdk_logger.addHandler(MuseSdkLogHandler())
    sdk_logger.setLevel(logging.INFO)
    sdk_logger.propagate = False


def failure_reason(exc, stage):
    """Expose useful diagnostics without logging remote text or credentials."""
    from musegadget.link_client import HttpStreamError, RequestRejected
    if isinstance(exc, (HttpStreamError, RequestRejected)):
        status = exc.status if type(exc.status) is int and 100 <= exc.status <= 599 else "未知"
        return f"Muse 拒绝请求，HTTP 状态码={status}"
    if isinstance(exc, asyncio.TimeoutError):
        return {"订阅回复": "等待订阅确认超过 15 秒", "发送消息并等待确认": "等待发送确认超过 30 秒",
                "等待设备注册": "等待设备注册超时"}.get(stage, "操作超时")
    known = {
        "Muse did not acknowledge the request": "Muse 未返回消息确认",
        "Muse did not acknowledge the device side chat": "Muse 未确认设备专属对话",
        "Muse did not acknowledge the gadget's dedicated side chat": "Muse 未确认设备专属对话",
        "Muse chat acknowledgement omitted the user message ID": "Muse 消息确认缺少消息 ID",
        "Muse returned an invalid chat acknowledgement": "Muse 返回的消息确认格式错误",
        "Muse returned no correlated reply": "Muse 未返回可关联的回复",
        "Muse subscription ended": "Muse 回复订阅意外结束",
        "Muse session is not connected": "Muse 连接已断开",
    }
    if type(exc) in (ValueError, ConnectionError) and str(exc) in known:
        return known[str(exc)]
    return "未分类异常；未输出异常原文以保护消息和凭证"


def voice_for(kind):
    if kind == ErrorKind.NOT_INSTALLED:
        return "还没有安装 Muse Gadget SDK，请先在服务器安装。"
    if kind == ErrorKind.AUTH:
        return "Muse 还没有配对，请先用手机上的 Muse 完成配对。"
    if kind == ErrorKind.TIMEOUT:
        return "Muse 回复超时了，稍后再试吧。"
    return "Muse 暂时无法连接，请检查服务器和 Muse 的连接。"


class MuseBridge:
    """一个注册连接；串行使用 SDK 的订阅就绪标记，避免多设备订阅竞争。"""
    def __init__(self):
        self.task = None
        self.session = None
        self.lock = asyncio.Lock()
        self.error = ""

    def launch(self):
        from core.agent import muse_pair_bridge
        if muse_pair_bridge.pairing_active():
            raise AgentSessionError(ErrorKind.AUTH, "正在重新配对")
        if self.task is None or self.task.done():
            if self.task and not self.task.cancelled():
                self.task.exception()
            try:
                from musegadget import config
                from musegadget.identity import load_or_create
                from musegadget.link_client import DeviceDescription
                from musegadget.service import Service
            except ImportError as exc:
                raise AgentSessionError(ErrorKind.NOT_INSTALLED) from exc
            if not config.load_json(config.PAIRING_FILE):
                raise AgentSessionError(ErrorKind.AUTH)
            self.error = ""
            bridge = self

            class ChatService(Service):
                def describe(self):
                    return DeviceDescription(self.identity.node_id, "小麦 ThinkSmart", "0.1.0", {})

                def run_command(self, name, params, timeout_ms=None):
                    return {"ok": False, "error": "This bridge exposes no device commands"}

                async def serve(self, session, vm, stop):
                    bridge.session = session
                    running = asyncio.create_task(session.run(stop))
                    registered = asyncio.create_task(session.wait_registered(timeout=25))
                    try:
                        done, _ = await asyncio.wait({running, registered}, return_when=asyncio.FIRST_COMPLETED)
                        if running not in done:
                            await registered
                            bridge.error = ""
                        outcome = await running
                        reasons = {"forbidden": "Muse 拒绝设备注册或连接", "auth_rejected": "Muse 拒绝连接认证",
                                   "unpaired": "Muse 已解除设备配对", "closed": "Muse 连接已断开，正在重试"}
                        bridge.error = reasons.get(outcome.value, "")
                        return outcome
                    except asyncio.TimeoutError:
                        bridge.error = "Muse 注册超时，正在重试"
                        raise
                    except Exception:
                        bridge.error = "连接或注册失败，请检查服务端 Muse 日志"
                        raise
                    finally:
                        for pending in (running, registered):
                            pending.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await pending
                        if bridge.session is session:
                            bridge.session = None

            # 不实例化 Executor：不会授予 Muse system.run / file.* 等主机能力。
            self.service = ChatService(load_or_create(), None, sdk_token=config.sdk_token())
            self.task = asyncio.create_task(self.service.run())

    async def stop(self):
        if self.task:
            self.service.stop()
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        self.task = None
        self.session = None

    async def start(self):
        self.launch()
        try:
            async def ready():
                while True:
                    if self.task.done():
                        self.task.result()
                        raise ConnectionError("Muse service stopped")
                    session = self.session
                    if session is not None and session.registered.is_set():
                        return session
                    await asyncio.sleep(.1)
            return await asyncio.wait_for(ready(), 25)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise AgentSessionError(ErrorKind.SERVER) from exc


class MuseSession(AgentSession):
    def __init__(self, bridge, session_id=None, voice_hint="回复会被语音朗读，请简短口语化，不要使用表格或代码。"):
        self.bridge = bridge
        self._id = session_id or str(uuid.uuid4())
        self.voice_hint = voice_hint
        self.alive = False
        self._interrupt = asyncio.Event()
        self._replay_scope = None

    @property
    def session_id(self):
        return self._id

    async def start(self):
        await self.bridge.start()
        self.alive = True

    async def interrupt(self):
        # 停止本地等待/播报；官方文字 SDK 没有取消云端任务的接口。
        self._interrupt.set()

    async def close(self):
        self.alive = False
        await self.interrupt()

    async def send(self, text):
        from musegadget.link_client import acknowledgement, HttpStreamError
        from musegadget.muse_turn import MuseTurn, Reply, ReplayScope

        if self._interrupt.is_set():
            self._interrupt.clear()
            yield Done(self._id, interrupted=True)
            return
        # 全局排队也可打断，不会在用户离开之后补发问题。
        acquired = asyncio.create_task(self.bridge.lock.acquire())
        interrupted = asyncio.create_task(self._interrupt.wait())
        pump = None
        locked = False
        turn = None
        stage = "等待对话锁"
        try:
            yield Activity("init", tools_running=1)
            done, _ = await asyncio.wait({acquired, interrupted}, return_when=asyncio.FIRST_COMPLETED)
            if interrupted in done:
                yield Done(self._id, interrupted=True)
                return
            await acquired
            locked = True
            stage = "等待设备注册"
            session = await self.bridge.start()
            if self._interrupt.is_set():
                yield Done(self._id, interrupted=True)
                return
            queue = asyncio.Queue(maxsize=128)
            if self._replay_scope is None:
                self._replay_scope = ReplayScope()
            turn = MuseTurn(self._id, owns_chat=True, replay_scope=self._replay_scope)

            async def consume():
                async for event in session.subscribe_chat(self._id):
                    await queue.put(event)
                raise ConnectionError("Muse subscription ended")

            async def subscribe():
                nonlocal pump
                session.chat_subscribed.clear()
                pump = asyncio.create_task(consume())
                async def subscribed():
                    while not session.chat_subscribed.is_set():
                        if pump.done():
                            await pump
                        await asyncio.sleep(.02)
                await asyncio.wait_for(subscribed(), 15)

            stage = "订阅回复"
            try:
                await subscribe()
                new_chat = False
            except HttpStreamError as exc:
                if exc.status != 404:
                    raise
                # A new side chat is created by the first send, not subscribe.
                new_chat = True
                pump = None
                logger.bind(tag="Muse").info("Muse 专属会话尚不存在，将通过首次发送创建")
            if self._interrupt.is_set():
                yield Done(self._id, interrupted=True)
                return
            yield Activity("init", tools_running=0)
            message = text + ("\n\n" + self.voice_hint if self.voice_hint else "")
            stage = "发送消息并等待确认"
            result = await asyncio.wait_for(session.send_chat(message, self._id, output_modality="text"), 30)
            if not result.get("ok"):
                logger.bind(tag="Muse").error("Muse send_chat 原始失败响应：{}", repr(result))
            ack = acknowledgement(result)
            details = ack.get("result", ack)
            if details.get("session_id") != self._id or details.get("is_thread") is not True:
                raise ValueError("Muse did not acknowledge the device side chat")
            turn.acknowledge(ack)
            if new_chat:
                stage = "订阅回复"
                await subscribe()
            stage = "等待 Muse 回复"
            yield Activity("Muse", tools_running=1)
            # 只输出确认完成的回复，避免 Muse 最终修订文字被朗读两次。
            emitted = set()
            while not self._interrupt.is_set():
                if pump.done():
                    await pump
                try:
                    event = await asyncio.wait_for(queue.get(), .2)
                    turn.feed(event)
                except asyncio.TimeoutError:
                    pass
                for reply in turn.replies.values():
                    if isinstance(reply, Reply) and reply.bound and reply.done and reply.text and reply.message_id not in emitted:
                        emitted.add(reply.message_id)
                        yield TextDelta(reply.text + "\n")
                if turn.complete(time.monotonic()) and (emitted or turn.task_finished):
                    if not emitted:
                        raise ValueError("Muse returned no correlated reply")
                    yield Done(self._id)
                    return
            yield Done(self._id, interrupted=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.bind(tag="Muse").warning("Muse 对话失败：阶段={}，错误类型={}，原因={}",
                                       stage, type(exc).__name__, failure_reason(exc, stage))
            logger.bind(tag="Muse").error("Muse 对话原始异常与调用栈：\n{}", "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            self.alive = False
            yield Error(ErrorKind.SERVER, voice=voice_for(ErrorKind.SERVER))
        finally:
            for task in (pump, acquired, interrupted):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            if turn is not None:
                turn.retire()
            # acquire 与 interrupt 同时完成时也须释放锁。
            if locked or (acquired.done() and not acquired.cancelled() and acquired.exception() is None and acquired.result()):
                self.bridge.lock.release()
            self._interrupt.clear()
