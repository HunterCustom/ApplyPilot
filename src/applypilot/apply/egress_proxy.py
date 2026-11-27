"""Launcher-owned egress proxy: the worker Chrome's only route to the network.

Chrome is started with ``--proxy-server`` pointing here, ``--proxy-bypass-list=<-loopback>``
(so even 127.0.0.1 goes through the proxy) and ``--host-resolver-rules`` that make
its own DNS fail, so every connection -- documents, subresources, fetch/XHR,
WebSockets, service workers, WebRTC over TCP -- is decided here first.

Rules (``network_policy.proxy_host_reason``): HTTPS ``CONNECT`` to port 443 of an
allowlisted host only. The proxy resolves the name itself, refuses if *any*
resolved address is non-public (loopback, private, link-local, CGNAT, IPv6
equivalents, IPv4 embedded in IPv6) and connects to the vetted address it
checked -- so a DNS rebinding answer can never be used. Plain HTTP is never
forwarded: GET/HEAD to an allowlisted host get a local redirect to HTTPS,
everything else 403. Only ``trusted_hosts`` (synthetic test servers) are
forwarded as plain HTTP.

The proxy only sees hosts for HTTPS; methods, paths and bodies are decided by
the CDP controller (``network_guard``) before Chrome sends them.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from applypilot.apply import network_policy as NP
from applypilot.apply import policy as P

logger = logging.getLogger(__name__)
Resolver = Callable[[str, int], Awaitable[list[str]]]
_HEAD_LIMIT = 64 * 1024


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(i[4][0]) for i in infos]


def _split_hostport(target: str, default_port: int) -> tuple[str, int]:
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, _, port = target.rpartition(":") if target.count(":") == 1 else (target, "", "")
    return P.normalize_host(host), int(port) if port else default_port


class EgressProxy:
    def __init__(self, *, get_policy: Callable[[], P.SessionPolicy | None],
                 on_event: Callable[[dict], None] = lambda e: None,
                 resolver: Resolver = system_resolver, connect_timeout: float = 15.0) -> None:
        self.get_policy = get_policy
        self.on_event = on_event
        self.resolver = resolver
        self.connect_timeout = connect_timeout
        self.selftest_token: str | None = None
        self.selftest_seen: dict[str, bool] = {}  # kind -> came through the proxy (absolute-form)
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=_HEAD_LIMIT)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2)
            except (TimeoutError, RuntimeError):
                pass

    async def vet(self, host: str, port: int) -> tuple[str | None, str | None]:
        """(address to connect to, None) or (None, reason). DNS is resolved here, once."""
        policy = self.get_policy()
        why = NP.proxy_host_reason(policy, host, port)
        if why:
            return None, why
        if policy and f"{host}:{port}" in {P.normalize_host(t) for t in policy.trusted_hosts}:
            return host, None  # synthetic test servers (tests only)
        try:
            addrs = await asyncio.wait_for(self.resolver(host, port), self.connect_timeout)
        except (TimeoutError, OSError):
            return None, f"DNS resolution failed for {host}"
        if not addrs:
            return None, f"no addresses for {host}"
        for a in addrs:
            bad = NP.ip_block_reason(a)
            if bad:
                return None, f"{host} resolves to a {bad}"
        return addrs[0], None

    def _log(self, **rec) -> None:
        try:
            self.on_event({"layer": "proxy", **rec})
        except Exception:
            logger.debug("proxy event log failed", exc_info=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError):
            writer.close()
            return
        try:
            line, *header_lines = head.decode("latin-1").split("\r\n")
            method, target, _version = line.split(" ", 2)
            method = method.upper()
            if method == "CONNECT":
                await self._connect(target, reader, writer)
            else:
                await self._plain(method, target, header_lines, reader, writer)
        except Exception:  # noqa: BLE001 -- malformed input: refuse
            await self._reply(writer, 400, "Bad Request")

    async def _reply(self, writer: asyncio.StreamWriter, code: int, text: str, extra: str = "") -> None:
        try:
            writer.write(f"HTTP/1.1 {code} {text}\r\n{extra}Content-Length: 0\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        writer.close()

    async def _connect(self, target: str, reader, writer) -> None:
        host, port = _split_hostport(target, 443)
        addr, why = await self.vet(host, port)
        if why:
            self._log(kind="connect", host=host, port=port, decision="block", reason=why[:160])
            await self._reply(writer, 403, "Forbidden")
            return
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(addr, port), self.connect_timeout)
        except (TimeoutError, OSError):
            self._log(kind="connect", host=host, port=port, decision="error")
            await self._reply(writer, 502, "Bad Gateway")
            return
        self._log(kind="connect", host=host, port=port, decision="allow")
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await _pipe(reader, writer, up_r, up_w)

    async def _plain(self, method: str, target: str, header_lines: list[str], reader, writer) -> None:
        parts = urlsplit(target)
        token = self.selftest_token
        path = parts.path or "/"
        if token and path == f"/applypilot-selftest/{token}":
            # Absolute-form proves the request was routed through this proxy; origin-form means
            # Chrome bypassed the proxy and connected here directly.
            kind = "loopback" if parts.hostname in ("127.0.0.1", "localhost") else "name"
            if not parts.scheme:
                kind = "direct"
            self.selftest_seen[kind] = bool(parts.scheme)
            await self._reply(writer, 204, "No Content")
            return
        if parts.scheme != "http" or not parts.hostname:
            self._log(kind="http", decision="block", reason="not an absolute http:// proxy request")
            await self._reply(writer, 400, "Bad Request")
            return
        host = P.normalize_host(parts.hostname)
        port = parts.port or 80
        policy = self.get_policy()
        trusted = bool(policy) and f"{host}:{port}" in {P.normalize_host(t) for t in policy.trusted_hosts}
        if trusted:
            await self._forward_trusted(method, parts, header_lines, host, port, reader, writer)
            return
        why = NP.proxy_host_reason(policy, host, 443)
        if why is None and port == 80 and method in ("GET", "HEAD"):
            # Never send plain HTTP upstream; let the browser retry over HTTPS (re-decided there).
            location = parts._replace(scheme="https", netloc=host).geturl()
            self._log(kind="http", host=host, decision="upgrade")
            await self._reply(writer, 307, "Temporary Redirect", f"Location: {location}\r\n")
            return
        self._log(kind="http", host=host, port=port, decision="block",
                  reason=(why or f"plain-http {method} is not allowed")[:160])
        await self._reply(writer, 403, "Forbidden")

    async def _forward_trusted(self, method, parts, header_lines, host, port, reader, writer) -> None:
        headers = [h for h in header_lines if h and not h.lower().startswith(("proxy-", "connection:", "keep-alive:"))]
        length = 0
        for h in headers:
            k, _, v = h.partition(":")
            if k.strip().lower() == "content-length":
                length = int(v.strip())
            if k.strip().lower() == "transfer-encoding":
                await self._reply(writer, 411, "Length Required")
                return
        body = await reader.readexactly(length) if length else b""
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, port), self.connect_timeout)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        up_w.write((f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\nConnection: close\r\n\r\n").encode("latin-1") + body)
        await up_w.drain()
        self._log(kind="http", host=host, port=port, decision="allow_trusted", method=method)
        try:
            while chunk := await up_r.read(65536):
                writer.write(chunk)
                await writer.drain()
        finally:
            up_w.close()
            writer.close()


async def _pipe(r1, w1, r2, w2) -> None:
    async def copy(src, dst):
        try:
            while chunk := await src.read(65536):
                dst.write(chunk)
                await dst.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                dst.close()
            except (ConnectionError, OSError):
                pass

    await asyncio.gather(copy(r1, w2), copy(r2, w1))
