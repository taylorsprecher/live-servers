"""Minimal publish-only MQTT 3.1.1 client over stdlib ssl/socket.

Written for the USB-SOVEREIGN live IoT sink: mutual-TLS to AWS IoT Core
(port 8883), CONNECT with the bridge's client ID, PUBLISH at QoS 1 with
PUBACK tracking, PINGREQ keepalive, clean DISCONNECT. No third-party
MQTT SDK is required (neither paho-mqtt nor AWSIoTPythonSDK is installed
in the deployment image, which is standard-library only).

Scope limits (deliberate): publish-only, no subscriptions, no QoS 2, no
retained/will messages beyond the CONNECT flags the caller sets. That is
all the telemetry path needs; anything richer belongs in a real SDK.
"""

import socket
import ssl
import struct
import threading
import time

MQTT_PORT = 8883
_PROTOCOL_NAME = b"MQTT"
_PROTOCOL_LEVEL = 4  # MQTT 3.1.1


class MqttError(RuntimeError):
    pass


def _encode_remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        digit = n % 128
        n //= 128
        if n > 0:
            digit |= 0x80
        out.append(digit)
        if n == 0:
            return bytes(out)


def _encode_utf8(s: str) -> bytes:
    data = s.encode("utf-8")
    return struct.pack("!H", len(data)) + data


def build_connect_packet(client_id: str, keepalive_s: int = 60,
                         clean_session: bool = True) -> bytes:
    flags = 0x02 if clean_session else 0x00
    variable = struct.pack("!H", len(_PROTOCOL_NAME)) + _PROTOCOL_NAME
    variable += bytes([_PROTOCOL_LEVEL, flags])
    variable += struct.pack("!H", keepalive_s)
    payload = _encode_utf8(client_id)
    body = variable + payload
    return b"\x10" + _encode_remaining_length(len(body)) + body


def build_publish_packet(topic: str, payload: bytes, packet_id: int,
                         qos: int = 1) -> bytes:
    variable = _encode_utf8(topic)
    if qos > 0:
        variable += struct.pack("!H", packet_id)
    body = variable + payload
    header = 0x30 | (qos << 1)
    return bytes([header]) + _encode_remaining_length(len(body)) + body


def build_pingreq_packet() -> bytes:
    return b"\xc0\x00"


def build_disconnect_packet() -> bytes:
    return b"\xe0\x00"


class _PacketReader:
    """Reads whole MQTT packets from a socket-like recv callable."""

    def __init__(self, recv):
        self._recv = recv
        self._buf = bytearray()

    def _fill(self, n: int):
        while len(self._buf) < n:
            chunk = self._recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise MqttError("connection closed by broker")
            self._buf.extend(chunk)

    def read_packet(self):
        """Returns (packet_type, flags, body_bytes)."""
        self._fill(1)
        first = self._buf[0]
        del self._buf[0]
        multiplier = 1
        remaining = 0
        while True:
            self._fill(1)
            digit = self._buf[0]
            del self._buf[0]
            remaining += (digit & 0x7F) * multiplier
            multiplier *= 128
            if not digit & 0x80:
                break
        self._fill(remaining)
        body = bytes(self._buf[:remaining])
        del self._buf[:remaining]
        return first >> 4, first & 0x0F, body


class MqttTlsClient:
    """Blocking publish-only MQTT client with a background reader thread.

    The reader thread exists for exactly two jobs: matching PUBACKs to
    in-flight publishes and answering broker PINGRESP bookkeeping. On an
    unexpected disconnect it flips ``connected`` to False and invokes the
    optional ``on_disconnect`` callback; publish() then raises MqttError
    instead of silently dropping telemetry.
    """

    def __init__(self, endpoint: str, client_id: str, cert_path: str,
                 key_path: str, ca_path: str, port: int = MQTT_PORT,
                 keepalive_s: int = 60, on_disconnect=None,
                 socket_factory=None):
        self.endpoint = endpoint
        self.client_id = client_id
        self.cert_path = cert_path
        self.key_path = key_path
        self.ca_path = ca_path
        self.port = port
        self.keepalive_s = keepalive_s
        self.on_disconnect = on_disconnect
        self._socket_factory = socket_factory  # tests inject a fake
        self._sock = None
        self._reader = None
        self._next_packet_id = 0
        self._pending = {}          # packet_id -> threading.Event
        self._lock = threading.Lock()
        self.connected = False
        self._last_io = 0.0
        self._stop = threading.Event()

    # -- transport ------------------------------------------------------
    def _open_socket(self):
        if self._socket_factory is not None:
            return self._socket_factory(self.endpoint, self.port)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_verify_locations(cafile=self.ca_path)
        ctx.load_cert_chain(certfile=self.cert_path, keyfile=self.key_path)
        # AWS IoT Core data endpoints use the ATS CA; SNI/ALPN are not
        # required for the classic 8883 MQTT listener.
        raw = socket.create_connection((self.endpoint, self.port),
                                       timeout=15)
        return ctx.wrap_socket(raw, server_hostname=self.endpoint)

    # -- lifecycle ------------------------------------------------------
    def connect(self, timeout: float = 15.0):
        if self.connected:
            return
        self._stop.clear()
        self._sock = self._open_socket()
        self._sock.settimeout(timeout)
        self._reader = _PacketReader(self._sock.recv)
        self._sock.sendall(build_connect_packet(
            self.client_id, keepalive_s=self.keepalive_s))
        ptype, _flags, body = self._reader.read_packet()
        if ptype != 2:  # CONNACK
            raise MqttError(f"expected CONNACK, got packet type {ptype}")
        if len(body) != 2:
            raise MqttError("malformed CONNACK")
        rc = body[1]
        if rc != 0:
            raise MqttError(f"CONNACK refused, return code {rc}")
        self.connected = True
        self._last_io = time.time()
        self._sock.settimeout(1.0)
        t = threading.Thread(target=self._reader_loop, daemon=True,
                             name="mqtt-reader")
        t.start()
        self._reader_thread = t

    def disconnect(self):
        self._stop.set()
        if self._sock is not None and self.connected:
            try:
                self._sock.sendall(build_disconnect_packet())
            except Exception:
                pass
        self.connected = False
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    # -- reader thread ----------------------------------------------------
    def _reader_loop(self):
        try:
            while not self._stop.is_set():
                try:
                    ptype, _flags, body = self._reader.read_packet()
                except socket.timeout:
                    self._maybe_ping()
                    continue
                self._last_io = time.time()
                if ptype == 4:  # PUBACK
                    if len(body) >= 2:
                        pid = struct.unpack("!H", body[:2])[0]
                        with self._lock:
                            ev = self._pending.pop(pid, None)
                        if ev is not None:
                            ev.set()
                elif ptype == 13:  # PINGRESP — nothing to do
                    pass
                # Anything else is ignored: publish-only client.
        except (MqttError, OSError, ssl.SSLError):
            pass
        finally:
            was = self.connected
            self.connected = False
            with self._lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for ev in pending:
                ev.set()  # wake publishers so they can raise
            if was and self.on_disconnect is not None:
                try:
                    self.on_disconnect()
                except Exception:
                    pass

    def _maybe_ping(self):
        if time.time() - self._last_io >= self.keepalive_s:
            try:
                self._sock.sendall(build_pingreq_packet())
                self._last_io = time.time()
            except OSError:
                pass

    # -- publish ----------------------------------------------------------
    def publish(self, topic: str, payload: bytes, qos: int = 1,
                timeout: float = 10.0):
        if not self.connected:
            raise MqttError("publish before connect / after disconnect")
        if qos == 0:
            self._sock.sendall(build_publish_packet(topic, payload, 0, 0))
            self._last_io = time.time()
            return
        with self._lock:
            self._next_packet_id = (self._next_packet_id % 0xFFFF) + 1
            pid = self._next_packet_id
            ev = threading.Event()
            self._pending[pid] = ev
        try:
            self._sock.sendall(build_publish_packet(topic, payload, pid, 1))
        except OSError as exc:
            with self._lock:
                self._pending.pop(pid, None)
            raise MqttError(f"publish send failed: {exc}") from exc
        self._last_io = time.time()
        if not ev.wait(timeout):
            with self._lock:
                self._pending.pop(pid, None)
            raise MqttError(f"PUBACK timeout for packet {pid}")
        if not self.connected:
            raise MqttError("connection lost awaiting PUBACK")
