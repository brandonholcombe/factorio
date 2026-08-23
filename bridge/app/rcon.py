"""Minimal Source-RCON client (sync; callers wrap in asyncio.to_thread).

Factorio speaks standard Source RCON: 4-byte little-endian length prefix,
then id/type int32s, body, two NULs. One request per command is plenty for
a 10s poll loop, so no pipelining.
"""
import socket
import struct

SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0


class RconError(Exception):
    pass


class RconClient:
    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._next_id = 0

    def _send(self, ptype: int, body: str) -> int:
        self._next_id += 1
        payload = struct.pack("<ii", self._next_id, ptype) + body.encode() + b"\x00\x00"
        assert self._sock is not None
        self._sock.sendall(struct.pack("<i", len(payload)) + payload)
        return self._next_id

    def _recv_packet(self) -> tuple[int, int, bytes]:
        assert self._sock is not None
        raw = b""
        while len(raw) < 4:
            chunk = self._sock.recv(4 - len(raw))
            if not chunk:
                raise RconError("connection closed")
            raw += chunk
        (length,) = struct.unpack("<i", raw)
        data = b""
        while len(data) < length:
            chunk = self._sock.recv(length - len(data))
            if not chunk:
                raise RconError("connection closed mid-packet")
            data += chunk
        pid, ptype = struct.unpack("<ii", data[:8])
        return pid, ptype, data[8:-2]

    def connect(self) -> None:
        self.close()
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        req_id = self._send(SERVERDATA_AUTH, self.password)
        # Auth response may be preceded by an empty RESPONSE_VALUE.
        while True:
            pid, ptype, _ = self._recv_packet()
            if ptype == SERVERDATA_AUTH_RESPONSE:
                if pid == -1:
                    raise RconError("RCON auth failed (bad password)")
                if pid != req_id:
                    raise RconError("RCON auth id mismatch")
                return

    def command(self, cmd: str) -> str:
        if self._sock is None:
            self.connect()
        try:
            req_id = self._send(SERVERDATA_EXECCOMMAND, cmd)
            pid, _, body = self._recv_packet()
        except (OSError, RconError):
            # One reconnect attempt per command; the poller treats a second
            # failure as "server down".
            self.connect()
            req_id = self._send(SERVERDATA_EXECCOMMAND, cmd)
            pid, _, body = self._recv_packet()
        if pid != req_id:
            raise RconError(f"response id {pid} != request {req_id}")
        return body.decode(errors="replace")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
