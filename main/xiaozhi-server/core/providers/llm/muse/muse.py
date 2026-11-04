"""Muse Gadget SDK provider，复用网关的 Agent 生命周期与语音输出。"""
from pathlib import Path
import asyncio

from config.config_loader import get_project_dir
from core.agent.agent_session import AgentSessionError, Done, ErrorKind
from core.agent.muse_session import voice_for
from core.agent.muse_runtime import RuntimeSession as MuseSession
from core.agent.session_store import SessionStore
from core.providers.llm.claude_cli.claude_cli import LLMProvider as AgentProvider


class LLMProvider(AgentProvider):
    AGENT_LABEL = "Muse"
    BACKGROUND_TURNS = False

    def __init__(self, config):
        self.model_name = "Muse"
        self.voice_hint = str(config.get("voice_hint", "回复会被语音朗读，请简短口语化，不要使用表格或代码。") or "")
        # Retire the former built-in phrase even in older persisted configurations.
        options = dict(config)
        if options.get("first_reply_filler") == "交给 Muse 了":
            options["first_reply_filler"] = ""
        self._init_common(options, filler_default="", filler_delay_default=6)
        self.store = SessionStore(Path(get_project_dir()) / "data" / "agent_sessions.json", "muse")
        self.bridge = None
        self._start_loop()

    def pause_for_pairing(self):
        async def pause():
            with self._states_lock:
                states = list(self._states.values())
            for state in states:
                if state.turn_task and not state.turn_task.done():
                    state.turn_task.cancel()
                if state.agent:
                    await state.agent.close()
                    state.agent = None
            self.bridge = None
        return asyncio.run_coroutine_threadsafe(pause(), self._loop)

    def _voice_error(self, kind):
        return voice_for(kind)

    def abort(self, conn):
        st = self._states.get(conn.session_id)
        if st is not None:
            st.abort_gen = st.turn_gen
        super().abort(conn)

    async def _ensure_agent(self, st, text, out, system_prompt):
        if st.agent and st.agent.alive:
            return False
        record = self.store.get(st.key) or {}
        agent = MuseSession(self.bridge, record.get("session_id"), self.voice_hint, st.key)
        await agent.start()
        if st.closed:
            await agent.close()
            raise AgentSessionError(ErrorKind.PROCESS_EXITED)
        if getattr(st, "abort_gen", None) == st.turn_gen:
            await agent.close()
            out.put(Done(interrupted=True))
            return True
        st.agent = agent
        return False
