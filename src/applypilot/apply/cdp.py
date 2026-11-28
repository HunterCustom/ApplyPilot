"""Minimal asyncio Chrome DevTools Protocol client for the launcher's network guard.

Local ``ws://127.0.0.1`` only (Chrome's remote-debugging endpoint), RFC 6455
client framing, flattened target sessions. Kept dependency-free on purpose: the
security boundary should not depend on a third-party websocket package.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
import urllib.request
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 256 * 1024 * 1024   # per assembled message (all fragments together)
MAX_PENDING_EVENTS = 5000          # outstanding event handler tasks before the connection is failed


class CDPError(RuntimeError):
    pass


def browser_ws_url(cdp_port: int, timeout: float = 5.0) -> str:
    """Browser-level websocket URL from /json/version (never through an env proxy)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{cdp_port}/json/version", timeout=timeout) as r:
        url = json.loads(r.read().decode())["webSocketDebuggerUrl"]
    host = urlsplit(url).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise CDPError(f"unexpected CDP host {host!r}")
    return url


EventHandler = Callable[[str, dict, "str | None"], Awaitable[None] | None]


class CDPConnection:
    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.closed = asyncio.Event()
        self.on_event: EventHandler | None = None
        self.on_close: Callable[[], None] | None = None
        self._event_tasks: set[asyncio.Task] = set()

    async def connect(self, ws_url: str, timeout: float = 10.0) -> None:
        parts = urlsplit(ws_url)
        if parts.scheme != "ws" or parts.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise CDPError("only local ws:// CDP endpoints are supported")
        host, port = parts.hostname, parts.port or 80
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=2 ** 20), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        self._writer.write((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                            "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        await self._writer.drain()
        head = await asyncio.wait_for(self._reader.readuntil(b"\r\n\r\n"), timeout)
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise CDPError(f"websocket upgrade refused: {lines[0]!r}")
        accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
        if headers.get("sec-websocket-accept") != accept:
            raise CDPError("bad Sec-WebSocket-Accept")
        self._task = asyncio.get_running_loop().create_task(self._read_loop())

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        assert self._writer is not None
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) if n < 4096 else _mask(payload, mask)
        async with self._send_lock:
            self._writer.write(header + mask + masked)
            await self._writer.drain()

    async def _read_frame(self) -> tuple[bool, int, bytes]:
        assert self._reader is not None
        b1, b2 = await self._reader.readexactly(2)
        fin, opcode, masked, n = bool(b1 & 0x80), b1 & 0x0F, bool(b2 & 0x80), b2 & 0x7F
        # RFC 6455 invariants (audit M5): servers never mask, no extensions are negotiated,
        # reserved opcodes are errors, control frames are short and never fragmented.
        if masked or b1 & 0x70 or opcode not in (0x0, 0x1, 0x2, 0x8, 0x9, 0xA):
            raise CDPError("malformed CDP websocket frame")
        if opcode >= 0x8 and (not fin or n > 125):
            raise CDPError("malformed CDP control frame")
        if n == 126:
            (n,) = struct.unpack("!H", await self._reader.readexactly(2))
        elif n == 127:
            (n,) = struct.unpack("!Q", await self._reader.readexactly(8))
        if n > MAX_MESSAGE:
            raise CDPError("CDP message too large")
        mask = await self._reader.readexactly(4) if masked else b""
        data = await self._reader.readexactly(n)
        return fin, opcode, (_mask(data, mask) if masked else data)

    async def _read_loop(self) -> None:
        buf, buf_op = bytearray(), 0
        try:
            while True:
                fin, opcode, data = await self._read_frame()
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    await self._send_frame(0xA, data)
                    continue
                if opcode == 0xA:
                    continue
                if opcode in (0x1, 0x2):
                    if buf_op:
                        raise CDPError("new message inside a fragmented one")
                    buf, buf_op = bytearray(data), opcode
                elif opcode == 0x0:
                    if not buf_op:
                        raise CDPError("continuation without a message")
                    buf += data
                if len(buf) > MAX_MESSAGE:
                    raise CDPError("CDP message too large")
                if not fin:
                    continue
                if buf_op == 0x1:
                    self._dispatch(json.loads(bytes(buf).decode("utf-8")))
                buf, buf_op = bytearray(), 0
        except (asyncio.IncompleteReadError, ConnectionError, OSError, CDPError, ValueError):
            pass
        finally:
            self._shutdown()

    def _dispatch(self, msg: dict) -> None:
        if "id" in msg:
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(CDPError(str(msg["error"].get("message", msg["error"]))))
                else:
                    fut.set_result(msg.get("result", {}))
            return
        if self.on_event:
            res = self.on_event(msg.get("method", ""), msg.get("params", {}), msg.get("sessionId"))
            if asyncio.iscoroutine(res):
                if len(self._event_tasks) >= MAX_PENDING_EVENTS:
                    res.close()
                    raise CDPError("too many outstanding CDP events")
                task = asyncio.get_running_loop().create_task(res)
                self._event_tasks.add(task)
                task.add_done_callback(self._event_tasks.discard)


    def _shutdown(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CDPError("CDP connection closed"))
        self._pending.clear()
        if self._writer is not None:
            self._writer.close()
        if self.on_close:
            self.on_close()

    async def send(self, method: str, params: dict | None = None, session_id: str | None = None,
                   timeout: float = 15.0) -> dict:
        if self.closed.is_set() or self._writer is None:
            raise CDPError("CDP connection closed")
        self._next_id += 1
        msg_id = self._next_id
        msg: dict = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._send_frame(0x1, json.dumps(msg).encode())
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(msg_id, None)

    async def close(self) -> None:
        self.on_close = None  # a deliberate close is not a failure
        if self._writer is not None and not self.closed.is_set():
            try:
                await self._send_frame(0x8, b"")
            except (ConnectionError, OSError):
                pass
        self._shutdown()
        if self._task:
            self._task.cancel()


def _mask(data: bytes, mask: bytes) -> bytes:
    n = len(data)
    key = int.from_bytes((mask * (n // 4 + 1))[:n], "big") if n else 0
    return (int.from_bytes(data, "big") ^ key).to_bytes(n, "big") if n else b""
