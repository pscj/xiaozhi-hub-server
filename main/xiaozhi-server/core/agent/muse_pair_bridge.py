"""Muse SDK pairing over the selected Android device's existing WebSocket."""
import asyncio
import base64
import json
import secrets
import threading
import time

_current = None
_guard = threading.RLock()
_reserved = False


class Bridge:
    def __init__(self, conn, state_dir):
        from musegadget import __version__, config, identity, network
        from musegadget.ble_setup import ProvisionFailed, SetupController
        from musegadget.pair import _verify_and_save
        from musegadget.pairing import PairingSession
        self.conn = conn
        self.token = secrets.token_urlsafe(24)
        self.expires = time.monotonic() + 600
        self.status = 'waiting'
        self.error = ''
        self._mtu = 23
        self.ident = identity.load_or_create(state_dir)
        sdk_token = config.sdk_token(state_dir)
        if not sdk_token:
            raise ValueError('请先保存 SDK Token')
        pairing = PairingSession(node_id=self.ident.node_id, device_id=self.ident.device_id,
                                 mac=self.ident.mac, firmware_version=__version__, sdk_token=sdk_token)
        # SDK uses MUSEGADGET_STATE_DIR when saving credentials.
        def provision(credentials, commit):
            if self.status != 'waiting':
                raise ProvisionFailed('error_storage')
            def guarded_commit(save):
                return commit(lambda: self.status == 'waiting' and save())
            _verify_and_save(credentials, guarded_commit, sdk_token)
        self.controller = SetupController(pairing=pairing, identity=self.ident, version=__version__,
            transport=self, network=network, provision=provision, on_complete=self.completed)
        self.controller.start()
        self.timer = threading.Timer(600, self.close)
        self.timer.daemon = True
        self.timer.start()

    def send(self, action, **data):
        message = json.dumps({'type': 'muse_pair', 'action': action, 'pair_id': self.token, **data})
        asyncio.run_coroutine_threadsafe(self.conn.websocket.send(message), self.conn.loop)

    def mtu(self):
        return self._mtu

    def send_packets(self, packets):
        for packet in packets:
            if self.status != 'waiting': return
            self.send('notify', data=base64.b64encode(packet).decode())
            time.sleep(.05)

    def disconnect(self, delay):
        # Android waits for queued GATT notifications as well as this delay.
        self.send('disconnect', delay_ms=max(0, round(delay * 1000)))

    def completed(self):
        if self.status != 'waiting': return
        self.status = 'paired'
        self.send('finished')
        self.controller.on_disconnect()
        self.controller.stop()
        self.timer.cancel()
        release(self)

    def close(self):
        if self.status == 'waiting':
            self.status = 'closed'
            self.controller.on_disconnect()
            self.controller.stop()
            self.send('stop')
        self.timer.cancel()
        release(self)

    def receive(self, conn, msg):
        if conn is not self.conn or msg.get('pair_id') != self.token or self.status != 'waiting': return
        if time.monotonic() >= self.expires:
            self.close(); return
        action = msg.get('action')
        if action == 'write':
            data = msg.get('data')
            if not isinstance(data, str) or len(data) > 684: return
            try:
                packet = base64.b64decode(data, validate=True)
            except ValueError:
                return
            if not packet or len(packet) > 512 or self.controller._inbox.qsize() >= 64:
                self.close(); return
            self.controller.on_write(packet)
        elif action == 'mtu':
            value = msg.get('mtu')
            if isinstance(value, int) and 23 <= value <= 517: self._mtu = value
        elif action == 'disconnect':
            self._mtu = 23
            self.controller.on_disconnect()
        elif action == 'error':
            self.status = 'error'; self.error = '安卓设备蓝牙未开启、未授权，或不支持 BLE 广播'; self.controller.on_disconnect(); self.controller.stop(); self.timer.cancel(); release(self)


def reserve():
    global _reserved, _current
    with _guard:
        if _reserved or (_current and _current.status == 'waiting'):
            raise ValueError('已有配对正在进行')
        _current = None
        _reserved = True


def release(bridge=None):
    global _reserved
    with _guard:
        if bridge is None or bridge is _current:
            _reserved = False
            from core.agent import muse_runtime
            muse_runtime.changed()


def pairing_active():
    with _guard:
        return _reserved


def start(conn, state_dir, replace=False, reserved=False):
    global _current
    with _guard:
        if _current and _current.status == 'waiting': raise ValueError('已有配对正在进行')
        if (state_dir / 'pairing.json').exists() and not replace: raise ValueError('已配对，请选择重新配对')
        if not reserved:
            reserve()
        try:
            bridge = Bridge(conn, state_dir)
        except BaseException:
            release()
            raise
        _current = bridge
        try:
            bridge.send('start', name=bridge.ident.ble_name)
        except Exception:
            bridge.close()
            raise
        return bridge


def current():
    with _guard: return _current


def receive(conn, msg):
    b = current()
    if b: b.receive(conn, msg)


def disconnected(conn):
    b = current()
    if b and b.conn is conn: b.close()
