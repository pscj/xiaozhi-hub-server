"""停止词只控制当前回答，其他插话不打断也不进入下一轮。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import core.handle.receiveAudioHandle as receive
import core.providers.asr.base as asr_base
from core.handle.helloHandle import handleHelloMessage
from core.handle.sendAudioHandle import send_tts_message
from core.handle.textHandler.listenMessageHandler import ListenTextMessageHandler
from core.providers.asr.dto.dto import InterfaceType
from core.utils import reply_echo, speech_interrupt, turn_gen
from tests.core.test_instant_abort import SlowASR, make_conn


def keyword_conn():
    conn = make_conn()
    conn.features = {"speech_interrupt": "keywords", "vad_end": True}
    conn.client_listen_mode = "realtime"
    conn.interrupt_only = True
    return conn


@pytest.fixture
def asked(monkeypatch):
    start = AsyncMock()
    monkeypatch.setattr(asr_base, "startToChat", start)
    monkeypatch.setattr(asr_base, "correct_and_record", lambda conn, text: text)
    monkeypatch.setattr(asr_base, "enqueue_asr_report", MagicMock())
    return start


@pytest.mark.parametrize("text", ["停止", "暂停", " 停止！ ", "暂停。"])
async def test_stop_words_abort_without_asking_model(text, asked):
    conn, asr = keyword_conn(), SlowASR(text)
    asr.release.set()
    conn.asr_audio_queue.put(b"other speech during reply")
    await asr.handle_voice_stop(conn, [b"\0" * 16000])
    asked.assert_not_awaited()
    assert conn.client_abort and conn.asr_audio_queue.empty()
    sent = [json.loads(c.args[0]) for c in conn.websocket.send.call_args_list]
    assert len(sent) == 1
    assert sent[0]["interrupted"] and sent[0]["reason"] == "stop_word"


@pytest.mark.parametrize("text", ["今天天气怎么样", "不要停止", "暂停功能是什么", "停止以后再说", "", "咳"])
async def test_other_speech_during_reply_is_ignored(text, asked):
    conn, asr = keyword_conn(), SlowASR(text)
    asr.release.set()
    await asr.handle_voice_stop(conn, [b"\0" * 16000])
    asked.assert_not_awaited()
    conn.websocket.send.assert_not_awaited()
    assert not conn.client_abort


async def test_stop_word_heard_in_playback_echo_is_not_a_command(asked):
    conn, asr = keyword_conn(), SlowASR("停止")
    conn.client_is_speaking = True
    reply_echo.note_reply(conn, "需要时请说停止。")
    conn.asr_echo_context = reply_echo.capture_context(conn)
    asr.release.set()
    await asr.handle_voice_stop(conn, [b"\0" * 16000])
    asked.assert_not_awaited()
    conn.websocket.send.assert_not_awaited()


@pytest.mark.parametrize("mode,manual,text", [("keywords", False, "正常提问"), ("keywords", False, "暂停"),
                                           ("any", False, "新问题"), ("keywords", True, "手动提问")])
async def test_normal_listening_and_free_interruption_still_ask_model(mode, manual, text, asked):
    conn, asr = keyword_conn(), SlowASR(text)
    conn.features["speech_interrupt"] = mode
    conn.interrupt_only = mode == "any" or manual
    conn.client_listen_mode = "manual" if manual else "realtime"
    asr.release.set()
    await asr.handle_voice_stop(conn, [b"\0" * 16000])
    asked.assert_awaited_once()
    assert asked.await_args.args[1] == text


async def test_off_mode_drops_even_stop_word(asked):
    conn, asr = keyword_conn(), SlowASR("停止")
    conn.features["speech_interrupt"] = "off"
    asr.release.set()
    await asr.handle_voice_stop(conn, [b"\0" * 16000])
    asked.assert_not_awaited()
    conn.websocket.send.assert_not_awaited()


async def test_recognition_from_reply_is_dropped_after_listening_resumes(asked):
    conn, asr = keyword_conn(), SlowASR("今天天气怎么样")
    conn.asr_interrupt_only = True
    task = asyncio.create_task(asr.handle_voice_stop(conn, [b"\0" * 16000]))
    await asr.started.wait()
    await ListenTextMessageHandler().handle(conn, {"state": "start", "mode": "realtime"})
    asr.release.set()
    await task
    asked.assert_not_awaited()
    assert not conn.interrupt_only
    conn.websocket.send.assert_not_awaited()


async def test_stale_stop_word_cannot_abort_new_turn(asked):
    conn, asr = keyword_conn(), SlowASR("暂停")
    task = asyncio.create_task(asr.handle_voice_stop(conn, [b"\0" * 16000]))
    await asr.started.wait()
    turn_gen.bump(conn)
    asr.release.set()
    await task
    asked.assert_not_awaited()
    conn.websocket.send.assert_not_awaited()


async def test_short_stop_word_passes_reply_audio_length_gate(asked):
    conn, asr = keyword_conn(), SlowASR("停止")
    asr.interface_type = InterfaceType.NON_STREAM
    conn.asr = asr
    conn.asr_audio = []
    conn.asr_voice_end = 0
    conn.client_voice_stop = conn.client_have_voice = True
    asr.release.set()
    await asr.receive_audio(conn, b"\0" * 12800, True)  # 0.4 秒，低于普通提问的 0.9 秒门槛
    asked.assert_not_awaited()
    assert conn.client_abort


async def test_aec_voice_detection_does_not_bypass_stop_words(monkeypatch):
    conn = keyword_conn()
    conn.client_aec = conn.client_is_speaking = True
    conn.vad = SimpleNamespace(is_vad=MagicMock(return_value=True))
    conn.asr = SimpleNamespace(receive_audio=AsyncMock())
    abort = AsyncMock()
    monkeypatch.setattr(receive, "handleAbortMessage", abort)
    monkeypatch.setattr(receive, "no_voice_close_connect", AsyncMock())
    await receive.handleAudioMessage(conn, b"unrecognized speech")
    abort.assert_not_awaited()
    conn.asr.receive_audio.assert_awaited_once()


async def test_hello_confirms_stop_word_support():
    conn = keyword_conn()
    conn.welcome_msg = {"type": "hello", "features": {"other": True}}
    await handleHelloMessage(conn, {"features": conn.features})
    welcome = json.loads(conn.websocket.send.await_args.args[0])
    assert welcome["features"] == {"other": True, "vad_end": True, "speech_interrupt": True}


async def test_reply_scope_covers_thinking_and_local_playback_buffer():
    conn = keyword_conn()
    conn.interrupt_only = False
    await send_tts_message(conn, "start")
    assert speech_interrupt.active(conn)
    conn.client_is_speaking = False  # 服务端已发完，但设备还有播放缓冲
    assert speech_interrupt.active(conn)
    await ListenTextMessageHandler().handle(conn, {"state": "start", "mode": "realtime"})
    assert not speech_interrupt.active(conn)
