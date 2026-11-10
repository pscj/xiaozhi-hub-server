"""唤醒问候期间保留实时客户端的第一句音频。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import core.handle.receiveAudioHandle as receive


@pytest.mark.parametrize("mode", ["realtime", "auto", "manual"])
async def test_wake_greeting_keeps_realtime_audio(monkeypatch, mode):
    h = SimpleNamespace()
    h.just_woken_up = True
    h.client_listen_mode = mode
    h.client_aec = False
    h.vad = SimpleNamespace(is_vad=MagicMock(return_value=True))
    h.asr = SimpleNamespace(receive_audio=AsyncMock())
    idle = AsyncMock()
    monkeypatch.setattr(receive, "no_voice_close_connect", idle)
    await receive.handleAudioMessage(h, b"user speech")
    if mode == "realtime":
        h.asr.receive_audio.assert_awaited_once_with(h, b"user speech", True)
        idle.assert_awaited_once_with(h, True)
        assert not hasattr(h, "vad_resume_task")
    else:
        h.asr.receive_audio.assert_not_awaited()
        h.vad_resume_task.cancel()
        await asyncio.gather(h.vad_resume_task, return_exceptions=True)
