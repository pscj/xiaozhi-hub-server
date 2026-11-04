"""官方 Muse Gadget SDK 的文字会话适配；音频继续由网关 ASR/TTS 处理。"""
import asyncio
import contextlib
import logging
import re
import traceback
import time
import uuid
from collections import OrderedDict

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


# ---------- 主动消息（不是设备本轮提问触发的 Muse 回复） ----------
# 定时任务等在设备专属聊天里产生的回复，在回合之外到达。常驻订阅把它们组装成全文，
# 交给待汇报箱（core/agent/reports.py），由它找设备空闲时念出来。
# 专属聊天是设备的通道，但不是每条消息都要念：只有以 PROACTIVE_MARKER 开头的消息
# 才播报（Muse 侧发送闹钟、提醒等要念给设备听的消息时在正文开头加标记，进待汇报箱
# 前去掉）；手机 App 在该聊天里的普通对话回复不带标记，只留在聊天里，不出声。
PROACTIVE_BOT_ID = "muse"  # 待汇报箱里标记是谁的话
PROACTIVE_MARKER = "【播报】"  # 主动消息正文开头的播报标记
_PROACTIVE_GRACE_SECONDS = 5.0  # 消息完成后等这么久：本轮回复会被回合认领，剩下的才是主动消息
_PROACTIVE_MAX_CHARS = 1000  # 主动播报的单条上限，过长的截断，避免设备长时间朗读
_PROACTIVE_ID_LIMIT = 200  # 每个聊天保留的已认领/已处理消息 ID 上限
_MESSAGE_EVENTS = ("delta.message_start", "delta.text_append", "delta.message_done", "message.assistant")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _proactive_final_text(payload, message_id):
    """从 message_done / message.assistant 事件里取最终全文，规则与 SDK 的 MuseTurn 一致。"""
    text = payload.get("display_text")
    if not isinstance(text, str):
        transcript = payload.get("transcript")
        if isinstance(transcript, str):
            text = transcript
        else:
            from musegadget.muse_turn import transcript_text

            text = transcript_text(transcript, message_id)
        if not text and not isinstance(transcript, str):
            content = payload.get("content")
            text = content if isinstance(content, str) else ""
    if not isinstance(text, str):
        return ""
    return _CONTROL_CHARS.sub("", text).strip()


class MuseBridge:
    """一个注册连接；串行使用 SDK 的订阅就绪标记，避免多设备订阅竞争。"""
    def __init__(self):
        self.task = None
        self.session = None
        self.lock = asyncio.Lock()
        self.error = ""
        self._watchers = {}  # session_id -> {"task": Task, "key": 设备 ID}
        self._claims = {}  # session_id -> OrderedDict[message_id, None]：已被某轮回合认领的回复
        self._consumed = {}  # session_id -> OrderedDict[message_id, None]：已处理（认领或已汇报）的消息
        self._pumps = {}  # session_id -> 订阅泵状态：每个聊天只有一条订阅流，回合与 watcher 共用

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
        for watcher in self._watchers.values():
            watcher["task"].cancel()
        self._watchers.clear()
        for state in self._pumps.values():
            state["task"].cancel()
        self._pumps.clear()
        if self.task:
            self.service.stop()
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        self.task = None
        self.session = None

    # ---------- 主动消息：认领登记与常驻订阅 ----------

    @staticmethod
    def _remember(store, session_id, message_id):
        ids = store.setdefault(session_id, OrderedDict())
        ids[message_id] = None
        ids.move_to_end(message_id)
        while len(ids) > _PROACTIVE_ID_LIMIT:
            ids.popitem(last=False)

    def claim(self, session_id, message_id):
        """某轮回合已经把这条回复念给设备了：常驻订阅不要再把它当主动消息汇报。"""
        if session_id and message_id:
            self._remember(self._claims, session_id, message_id)
            self._remember(self._consumed, session_id, message_id)

    def consumed_ids(self, session_id):
        """已处理过的消息 ID，供新回合的 ReplayScope 退休，避免重放被误认成本轮回复。"""
        return set(self._consumed.get(session_id, ()))

    # ---------- 单一订阅泵：一个聊天只有一条订阅流，回合与 watcher 都是听众 ----------
    # 同一 LinkSession 上并行开两条订阅会互相竞争（chat_subscribed 是会话级标记，
    # 服务端也只认一条），后开的会让先开的静默失聪。所以订阅统一由泵持有一条，
    # 事件扇出给所有听众队列；会话轮换、断线重连都在泵内处理，听众无感。

    def _ensure_pump(self, session_id):
        state = self._pumps.get(session_id)
        if state is None:
            state = {"listeners": set(), "ready": asyncio.Event(), "task": None,
                     "attempt": 0, "missing_attempt": 0}
            state["task"] = asyncio.create_task(self._pump(session_id, state))
            self._pumps[session_id] = state
        return state

    def listen(self, session_id):
        """登记一个听众队列并返回它；事件由订阅泵扇出。用完须 unlisten。"""
        state = self._ensure_pump(session_id)
        queue = asyncio.Queue(maxsize=512)
        state["listeners"].add(queue)
        return queue

    def unlisten(self, session_id, queue):
        state = self._pumps.get(session_id)
        if state is not None:
            state["listeners"].discard(queue)

    async def wait_ready(self, session_id, timeout=15):
        """等这个聊天的订阅就绪。聊天尚不存在时抛 HttpStreamError(404)，与直连订阅一致。"""
        from musegadget.link_client import HttpStreamError

        state = self._ensure_pump(session_id)
        entry_attempt = state["attempt"]
        deadline = time.monotonic() + timeout
        while True:
            if state["ready"].is_set():
                return
            # 等待期间又失败了一次且是 404：聊天确实尚不存在（而不是上一轮的旧标记）
            if state["missing_attempt"] > entry_attempt:
                raise HttpStreamError(404, "/chat/subscribe")
            if time.monotonic() >= deadline:
                raise asyncio.TimeoutError()
            await asyncio.sleep(0.02)

    async def _pump(self, session_id, state):
        while True:
            session = self.session
            if session is None or not session.registered.is_set():
                state["ready"].clear()
                await asyncio.sleep(0.5)
                continue
            async def run_stream():
                async for event in session.subscribe_chat(session_id):
                    for queue in list(state["listeners"]):
                        if queue.full():  # 听众消费太慢：丢最旧的，不能拖住泵
                            with contextlib.suppress(asyncio.QueueEmpty):
                                queue.get_nowait()
                        queue.put_nowait(event)
                raise ConnectionError("Muse subscription ended")

            state["attempt"] += 1
            stream = asyncio.create_task(run_stream())
            try:
                # 固定版本的 SDK 没有 opened 回调；订阅被接受时它会自己点亮
                # chat_subscribed，而现在只有泵会订阅，靠这个标记判断就绪。
                while not session.chat_subscribed.is_set():
                    if stream.done():
                        await stream
                    if self.session is not session:
                        raise ConnectionError("Muse session rotated")
                    await asyncio.sleep(0.02)
                state["ready"].set()
                logger.bind(tag="Muse").info("订阅泵已订阅聊天，开始接收事件")
                await stream
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                from musegadget.link_client import HttpStreamError

                state["ready"].clear()
                if isinstance(exc, HttpStreamError) and exc.status == 404:
                    state["missing_attempt"] = state["attempt"]  # 聊天尚不存在：等首次发送创建后自动就绪
                else:
                    logger.bind(tag="Muse").info("订阅泵中断（{}），稍后重连", type(exc).__name__)
                await asyncio.sleep(2)
            finally:
                stream.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await stream

    def ensure_watcher(self, session_id, device_key):
        """保证这个专属聊天有一条常驻订阅在收主动消息。须在桥所在事件循环里调用。"""
        if not session_id or not device_key:
            return
        current = self._watchers.get(session_id)
        if current is not None and not current["task"].done():
            current["key"] = device_key
            return
        task = asyncio.create_task(self._watch(session_id, device_key))
        self._watchers[session_id] = {"task": task, "key": device_key}
        logger.bind(tag="Muse").info("常驻监听已启动，设备={}", device_key)

    def _report_proactive(self, session_id, device_key, message_id, text):
        self._remember(self._consumed, session_id, message_id)
        if len(text) > _PROACTIVE_MAX_CHARS:
            text = text[:_PROACTIVE_MAX_CHARS] + "……"
        try:
            from core.agent import reports

            reports.add(device_key, PROACTIVE_BOT_ID, text)
        except Exception:
            logger.bind(tag="Muse").warning("主动消息放入待汇报箱失败，设备={}", device_key)

    async def _watch(self, session_id, device_key):
        """常驻监听一个专属聊天：回合之外的完整回复组装后，带播报标记的进待汇报箱。

        事件来自桥的单一订阅泵（与回合共用同一条订阅流）。回合认领过的回复按
        message_id 跳过；组装顺序与 SDK 的 MuseTurn 一致，但不经过它——没有 ack
        的 MuseTurn 不产出事件。未完成的组装保留，重放靠已处理 ID 去重。"""
        pending = {}  # message_id -> {"text": 累积增量, "final": str|None, "done_at": float|None}

        def flush():
            now = time.monotonic()
            for message_id, item in list(pending.items()):
                if item["done_at"] is None:
                    if now - item["born"] > 600:  # 迟迟没有下文的半条消息，丢弃
                        pending.pop(message_id, None)
                    continue
                if now - item["done_at"] < _PROACTIVE_GRACE_SECONDS:
                    continue
                pending.pop(message_id, None)
                if message_id in self._claims.get(session_id, ()) or message_id in self._consumed.get(session_id, ()):
                    logger.bind(tag="Muse").info("常驻监听：一条消息已被回合认领或处理过，跳过")
                    self._remember(self._consumed, session_id, message_id)
                    continue
                # 最终文本（display_text/transcript）与增量累积是两个来源：实测发现
                # 定时消息的最终文本会丢开头的【播报】标记、增量里却完整（反之亦可能），
                # 所以两个来源都查标记，用带标记的那个来源播报。
                final_text = (item["final"] or "").strip()
                delta_text = (item["text"] or "").strip()
                logger.bind(tag="Muse").info("常驻监听收到完整消息：final=「{}」deltas=「{}」",
                                               final_text[:40], delta_text[:40])
                if final_text.startswith(PROACTIVE_MARKER):
                    text = final_text
                elif delta_text.startswith(PROACTIVE_MARKER):
                    text = delta_text
                else:
                    # 专属聊天里的普通消息（如 App 对话回复）：不念，记下避免重放再处理
                    self._remember(self._consumed, session_id, message_id)
                    continue
                text = text[len(PROACTIVE_MARKER):].strip()
                if text:
                    logger.bind(tag="Muse").info("收到主动消息，转入待汇报：{}", text[:60])
                    self._report_proactive(session_id, device_key, message_id, text)

        def handle(event):
            if not isinstance(event, dict) or event.get("type") != "event":
                return
            name = event.get("event")
            if name not in _MESSAGE_EVENTS:
                return
            payload = event.get("payload")
            if not isinstance(payload, dict):
                return
            if (payload.get("session_id") or event.get("session_id")) not in (None, session_id):
                return
            if payload.get("role") == "user":
                return
            message_id = payload.get("message_id") or event.get("message_id") or payload.get("id")
            if not isinstance(message_id, str) or not message_id:
                return
            if message_id in self._consumed.get(session_id, ()):
                return
            item = pending.setdefault(message_id, {"text": "", "final": None, "done_at": None, "born": time.monotonic()})
            if name == "delta.text_append" and item["done_at"] is None:
                delta = payload.get("text")
                if isinstance(delta, str):
                    item["text"] += _CONTROL_CHARS.sub("", delta)
            elif name in ("delta.message_done", "message.assistant"):
                final = _proactive_final_text(payload, message_id)
                if final:
                    item["final"] = final
                if name == "delta.message_done" or payload.get("display_text_ready") is not False:
                    if item["done_at"] is None:
                        item["done_at"] = time.monotonic()
            if len(pending) > 64:  # 异常堆积时丢最旧的未完成项
                for old_id, old in list(pending.items()):
                    if old["done_at"] is None:
                        pending.pop(old_id, None)
                        break

        queue = self.listen(session_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 0.2)
                except asyncio.TimeoutError:
                    flush()
                    continue
                handle(event)
                flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.bind(tag="Muse").exception("常驻监听异常退出，等待下次会话启动时重建")
        finally:
            self.unlisten(session_id, queue)

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
    def __init__(self, bridge, session_id=None, voice_hint="回复会被语音朗读，请简短口语化，不要使用表格或代码。", device_key=""):
        self.bridge = bridge
        self._id = session_id or str(uuid.uuid4())
        self.voice_hint = voice_hint
        self.device_key = device_key  # 待汇报箱按设备投递，常驻订阅靠它找设备
        self.alive = False
        self._interrupt = asyncio.Event()
        self._replay_scope = None

    @property
    def session_id(self):
        return self._id

    async def start(self):
        await self.bridge.start()
        self.alive = True
        ensure_watcher = getattr(self.bridge, "ensure_watcher", None)
        if ensure_watcher is not None and self.device_key:
            ensure_watcher(self._id, self.device_key)

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
        listener_q = None
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
            consumed_ids = getattr(self.bridge, "consumed_ids", None)
            if consumed_ids is not None:
                # 常驻订阅已处理过的消息（主动消息、往轮回复）退休掉，
                # 免得订阅重放时被误认成本轮的回复
                self._replay_scope.retire(("message", mid) for mid in consumed_ids(self._id))

            # 有订阅泵的桥（正式 MuseBridge）走泵：整个会话只有一条订阅流，
            # 回合与 watcher 都是听众；没有泵的桥（测试替身）直连订阅。
            bridge_listen = getattr(self.bridge, "listen", None)

            async def consume():
                if listener_q is not None:
                    while True:
                        await queue.put(await listener_q.get())
                async for event in session.subscribe_chat(self._id):
                    await queue.put(event)
                raise ConnectionError("Muse subscription ended")

            async def subscribe():
                nonlocal pump, listener_q
                if bridge_listen is not None:
                    if listener_q is None:
                        listener_q = bridge_listen(self._id)
                        pump = asyncio.create_task(consume())
                    await self.bridge.wait_ready(self._id, 15)
                    return
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
                if bridge_listen is None:
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
                        claim = getattr(self.bridge, "claim", None)
                        if claim is not None:
                            claim(self._id, reply.message_id)  # 这条已念给设备，常驻订阅不要重复汇报
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
            if listener_q is not None:
                self.bridge.unlisten(self._id, listener_q)
            if turn is not None:
                turn.retire()
            # acquire 与 interrupt 同时完成时也须释放锁。
            if locked or (acquired.done() and not acquired.cancelled() and acquired.exception() is None and acquired.result()):
                self.bridge.lock.release()
            self._interrupt.clear()
