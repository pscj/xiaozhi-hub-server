import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

pytest.importorskip('musegadget')
from core.agent import muse_pair_bridge as bridge_module


@pytest.fixture(autouse=True)
def real_sleep(monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', asyncio.tasks.sleep)


class Socket:
    def __init__(self):
        self.messages = asyncio.Queue()

    async def send(self, raw):
        await self.messages.put(json.loads(raw))


@pytest.fixture
def paired_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv('MUSEGADGET_STATE_DIR', str(tmp_path))
    (tmp_path / 'sdk_token').write_text('mgst_' + 'A' * 43)
    monkeypatch.setattr(bridge_module, '_current', None)
    return tmp_path


async def test_selected_device_transports_official_sdk_response(paired_bridge):
    socket = Socket()
    conn = SimpleNamespace(websocket=socket, loop=asyncio.get_running_loop())
    bridge = bridge_module.start(conn, paired_bridge)
    try:
        start = await asyncio.wait_for(socket.messages.get(), 2)
        assert start['action'] == 'start' and start['name'].startswith('MuseGadget')
        packet = base64.b64encode(b'{"action":"get_device_info"}').decode()
        bridge.receive(SimpleNamespace(), {'pair_id': bridge.token, 'action': 'write', 'data': packet})
        bridge.receive(conn, {'pair_id': 'wrong', 'action': 'write', 'data': packet})
        assert socket.messages.empty()
        bridge.receive(conn, {'pair_id': bridge.token, 'action': 'write', 'data': packet})
        # SDK fragments at the initial ATT MTU. Reassemble the response exactly as the phone does.
        from musegadget.ble_framing import ChunkAssembler
        assembler = ChunkAssembler()
        response = None
        while response is None:
            message = await asyncio.wait_for(socket.messages.get(), 3)
            assert message['action'] == 'notify'
            response = assembler.feed(base64.b64decode(message['data']))
        info = json.loads(response)
        assert info['type'] == 'device_info' and info['node_id'] == bridge.ident.node_id
    finally:
        bridge.close()


async def test_close_and_disconnect_invalidate_session(paired_bridge):
    socket = Socket()
    conn = SimpleNamespace(websocket=socket, loop=asyncio.get_running_loop())
    bridge = bridge_module.start(conn, paired_bridge)
    await asyncio.wait_for(socket.messages.get(), 2)
    bridge_module.disconnected(SimpleNamespace())
    assert bridge.status == 'waiting'
    bridge_module.disconnected(conn)
    assert bridge.status == 'closed'
    assert (await asyncio.wait_for(socket.messages.get(), 2))['action'] == 'stop'
    bridge.receive(conn, {'pair_id': bridge.token, 'action': 'write', 'data': 'e30='})
    assert bridge.status == 'closed'


async def test_existing_pairing_cannot_be_replaced(paired_bridge):
    (paired_bridge / 'pairing.json').write_text('{}')
    conn = SimpleNamespace(websocket=Socket(), loop=asyncio.get_running_loop())
    with pytest.raises(ValueError):
        bridge_module.start(conn, paired_bridge)


async def test_cancelled_pairing_cannot_save_credentials(paired_bridge):
    from musegadget.ble_setup import ProvisionFailed
    conn = SimpleNamespace(websocket=Socket(), loop=asyncio.get_running_loop())
    bridge = bridge_module.start(conn, paired_bridge)
    bridge.close()
    with pytest.raises(ProvisionFailed):
        bridge.controller._provision(None, lambda save: save())
    assert not (paired_bridge / 'pairing.json').exists()


async def test_repair_preserves_old_credentials_until_success(paired_bridge):
    old = '{"access_token":"old","refresh_token":"old-refresh"}'
    (paired_bridge / 'pairing.json').write_text(old)
    socket = Socket()
    conn = SimpleNamespace(websocket=socket, loop=asyncio.get_running_loop())
    bridge = bridge_module.start(conn, paired_bridge, replace=True)
    try:
        await asyncio.wait_for(socket.messages.get(), 2)
        assert bridge_module.pairing_active()
        assert (paired_bridge / 'pairing.json').read_text() == old
    finally:
        bridge.close()
    assert not bridge_module.pairing_active()
    assert (paired_bridge / 'pairing.json').read_text() == old


async def test_previous_bridge_cannot_release_new_reservation(paired_bridge):
    conn = SimpleNamespace(websocket=Socket(), loop=asyncio.get_running_loop())
    first = bridge_module.start(conn, paired_bridge)
    first.close()
    bridge_module.reserve()
    try:
        first.close()
        assert bridge_module.pairing_active()
    finally:
        bridge_module.release()


async def test_muse_connection_does_not_start_during_pairing(paired_bridge):
    from core.agent.muse_session import MuseBridge
    from core.agent.agent_session import AgentSessionError
    bridge_module.reserve()
    try:
        with pytest.raises(AgentSessionError):
            await MuseBridge().start()
    finally:
        bridge_module.release()


async def test_ble_reconnect_uses_default_mtu(paired_bridge):
    socket = Socket()
    conn = SimpleNamespace(websocket=socket, loop=asyncio.get_running_loop())
    bridge = bridge_module.start(conn, paired_bridge)
    try:
        await asyncio.wait_for(socket.messages.get(), 2)
        bridge.receive(conn, {'pair_id': bridge.token, 'action': 'mtu', 'mtu': 247})
        assert bridge.mtu() == 247
        bridge.receive(conn, {'pair_id': bridge.token, 'action': 'disconnect'})
        assert bridge.mtu() == 23
        bridge.receive(conn, {'pair_id': bridge.token, 'action': 'write',
                              'data': base64.b64encode(b'{"action":"get_device_info"}').decode()})
        from musegadget.ble_framing import ChunkAssembler
        assembler = ChunkAssembler()
        response = None
        while response is None:
            message = await asyncio.wait_for(socket.messages.get(), 3)
            packet = base64.b64decode(message['data'])
            assert len(packet) <= 20
            response = assembler.feed(packet)
        assert json.loads(response)['type'] == 'device_info'
    finally:
        bridge.close()


@pytest.mark.parametrize('delay, milliseconds', [(0.3, 300), (0.5, 500)])
async def test_sdk_disconnect_delay_reaches_android(paired_bridge, delay, milliseconds):
    socket = Socket()
    conn = SimpleNamespace(websocket=socket, loop=asyncio.get_running_loop())
    bridge = bridge_module.start(conn, paired_bridge)
    try:
        await asyncio.wait_for(socket.messages.get(), 2)
        bridge.send_packets([b'error'])
        bridge.disconnect(delay)
        assert (await asyncio.wait_for(socket.messages.get(), 2))['action'] == 'notify'
        message = await asyncio.wait_for(socket.messages.get(), 2)
        assert message['action'] == 'disconnect'
        assert message['delay_ms'] == milliseconds
        assert message['pair_id'] == bridge.token
    finally:
        bridge.close()
