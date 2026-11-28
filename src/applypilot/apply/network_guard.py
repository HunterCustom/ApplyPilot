"""Launcher-owned browser network and submission boundary (Phase 3b).

One ``NetworkGuard`` per job. It runs in the launcher process (a private asyncio
thread), never inside the agent, and owns two independent enforcement layers:

1. ``EgressProxy`` -- Chrome's only route to the network (see egress_proxy).
2. ``BrowserController`` -- the launcher's own CDP connection. It auto-attaches to
   every target (pages, popups, out-of-process iframes, dedicated/shared/service
   workers) *paused before they run*, enables ``Fetch`` interception on each, and
   only then lets them start. Every HTTP(S) request, including each redirect hop,
   is held until :func:`network_policy.decide_request` allows it:

   * forbidden destination (private/loopback/link-local/IP literal, off-allowlist,
     bad scheme or port) -> failed before it is sent;
   * dry run: any state-changing request (non GET/HEAD/OPTIONS, or a GET form
     submission) -> failed before it is sent, and the session is stopped as
     DRY_RUN. This does not depend on button labels;
   * real run: each state-changing request is held until a human grants that exact
     request (single-use, expiring, bound to job + session + request digest).

Fail closed: the agent is never started unless the guard attached and a self-test
proved both layers sit in the request path; losing the CDP connection, failing to
intercept a new target, or a WebSocket/WebTransport (which Fetch cannot hold) marks
the guard failed, records a terminal violation (the policy hook then refuses every
tool) and kills the agent through ``on_failure``.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from applypilot.apply import approval
from applypilot.apply import network_policy as NP
from applypilot.apply import policy as P
from applypilot.apply.cdp import CDPConnection, CDPError, browser_ws_url
from applypilot.apply.egress_proxy import EgressProxy
from applypilot.apply.redact import safe_url

logger = logging.getLogger(__name__)
NETWORK_LOG = "network.jsonl"
FORM_NAV_WAIT_S = 0.3   # how long a held navigation waits for a form-submission signal
FORM_NAV_TTL_S = 10.0
_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
_FETCH_ALL = {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}
_DOC_TARGETS = ("page", "iframe")
_WORKER_TARGETS = ("worker", "shared_worker", "service_worker")
_ALLOWED_NAV_SCHEMES = ("http", "https", "about", "data", "blob", "chrome-error")
_NEW_TAB_URLS = ("chrome://newtab", "chrome://new-tab-page", "chrome-search://")  # browser-owned blank tab

# Installed in every document before its scripts run (defence in depth, audit H4/M2): never hand a
# link to the operating system, and remove transports the Fetch layer cannot hold. The launcher's
# enforcement does not depend on this script (a page may find a way around it); the process jail
# (proc_jail) and this guard remain the boundaries.
PAGE_GUARD_SCRIPT = r"""
(() => {
  const ok = (u) => { try { const s = new URL(u, document.baseURI).protocol;
    return ["http:", "https:", "about:", "blob:", "data:", "javascript:"].includes(s); } catch (e) { return false; } };
  const stop = (ev) => { const a = ev.target && ev.target.closest && ev.target.closest("a[href],area[href]");
    if (a && !ok(a.getAttribute("href"))) { ev.preventDefault(); ev.stopImmediatePropagation(); } };
  for (const t of ["click", "auxclick", "keydown"]) window.addEventListener(t, stop, true);
  const open = window.open;
  window.open = function (u, ...r) { if (u !== undefined && u !== "" && !ok(String(u))) return null;
    return open.call(window, u, ...r); };
  for (const k of ["WebSocket", "WebTransport", "RTCPeerConnection", "webkitRTCPeerConnection", "RTCDataChannel"]) {
    try { Object.defineProperty(window, k, {value: undefined, configurable: false, writable: false}); } catch (e) {}
  }
})();
"""


class NetworkGuardError(RuntimeError):
    pass


def _strip(url: str) -> str:
    """URL safe for logs: no userinfo, query or fragment; token-like path segments masked."""
    return safe_url(url)


def _nofrag(url: str) -> str:
    return url.split("#", 1)[0]


class BrowserController:
    def __init__(self, guard: NetworkGuard) -> None:
        self.guard = guard
        self.conn = CDPConnection()
        self.sessions: dict[str, dict] = {}       # sessionId -> targetInfo
        self.intercepted: set[str] = set()        # sessions with Fetch interception enabled
        self.form_navs: dict[tuple[str, str], tuple[float, str]] = {}  # (frameId, url) -> (t, reason)
        self.paused_urls: list[str] = []          # recorded only during the self-test
        self.selftest_urls: set[str] = set()

    # --- lifecycle ---------------------------------------------------------------
    async def start(self, cdp_port: int) -> None:
        ws = await asyncio.get_running_loop().run_in_executor(None, browser_ws_url, cdp_port)
        await self.conn.connect(ws)
        self.conn.on_event = self._on_event
        self.conn.on_close = lambda: self.guard.fail("lost the CDP connection to the browser")
        await self.conn.send("Browser.setDownloadBehavior", {"behavior": "deny"})
        await self.conn.send("Target.setAutoAttach", _ATTACH)
        # Targets that existed before auto-attach (the initial tab) are attached explicitly.
        targets = (await self.conn.send("Target.getTargets")).get("targetInfos", [])
        attached = {i.get("targetId") for i in self.sessions.values()}
        for t in targets:
            if t.get("type") in _DOC_TARGETS + _WORKER_TARGETS and t["targetId"] not in attached and not t.get("attached"):
                await self.conn.send("Target.attachToTarget", {"targetId": t["targetId"], "flatten": True})

    async def stop(self) -> None:
        await self.conn.close()

    # --- events ------------------------------------------------------------------
    async def _on_event(self, method: str, params: dict, sid: str | None) -> None:
        try:
            if method == "Target.attachedToTarget":
                await self._on_attached(params, sid)
            elif method == "Target.detachedFromTarget":
                info = self.sessions.pop(params.get("sessionId", ""), None)
                self.intercepted.discard(params.get("sessionId", ""))
                if info:
                    await self._check_detach(info)
            elif method == "Fetch.requestPaused":
                await self._on_paused(params, sid)
            elif method == "Page.frameRequestedNavigation":
                self._on_frame_nav(params)
            elif method in ("Network.webSocketCreated", "Network.webTransportCreated"):
                kind = "WebSocket" if "Socket" in method else "WebTransport"
                self.guard.fail(f"{kind} opened to {_strip(params.get('url', ''))}; the launcher cannot "
                                "inspect its traffic, so the session was stopped")
            elif method == "Page.frameNavigated":
                frame = params.get("frame", {})
                furl = frame.get("url", "")
                scheme = urlsplit(furl).scheme.lower()
                if scheme and scheme not in _ALLOWED_NAV_SCHEMES and not furl.startswith(_NEW_TAB_URLS):
                    self.guard.violation(f"browser navigated to a '{scheme}:' URL", terminal=True)
        except Exception as e:  # noqa: BLE001 -- an unhandled error is an enforcement failure
            self.guard.fail(f"controller error in {method}: {type(e).__name__}")

    async def _on_attached(self, params: dict, parent_sid: str | None) -> None:
        sid = params["sessionId"]
        info = params.get("targetInfo", {})
        ttype = info.get("type", "")
        self.sessions[sid] = info
        try:
            if ttype == "worker" and parent_sid in self.intercepted:
                # Dedicated workers have no Fetch domain; Chrome routes their requests through the
                # parent document's interception (verified by tests/test_network_boundary.py).
                pass
            else:
                await self.conn.send("Fetch.enable", _FETCH_ALL, sid)
                self.intercepted.add(sid)
            if ttype in _DOC_TARGETS:
                await self.conn.send("Page.enable", {}, sid)
                await self.conn.send("Page.addScriptToEvaluateOnNewDocument",
                                     {"source": PAGE_GUARD_SCRIPT, "runImmediately": True}, sid)
                await self.conn.send("Network.enable", {}, sid)
                await self.conn.send("Target.setAutoAttach", _ATTACH, sid)
            elif ttype in _WORKER_TARGETS and params.get("waitingForDebugger"):
                # A paused worker answers worker-side commands only once it runs, but it handles its
                # queued inspector messages *before* executing script. So: Fetch is already active
                # (browser-side, awaited above); queue the rest, then resume, then await them.
                queued = [asyncio.ensure_future(self.conn.send(m, p, sid)) for m, p in
                          (("Network.enable", {}), ("Target.setAutoAttach", _ATTACH))]
                await self.conn.send("Runtime.runIfWaitingForDebugger", {}, sid)
                await asyncio.gather(*queued)
                self.guard.log({"layer": "cdp", "kind": "attach", "target": ttype, "decision": "intercepting"})
                return
            elif ttype in _WORKER_TARGETS:
                await self.conn.send("Network.enable", {}, sid)
                await self.conn.send("Target.setAutoAttach", _ATTACH, sid)
        except CDPError as e:
            # A target destroyed while we attached ("Session ... not found") can no longer run.
            if "not found" in str(e).lower() and not await self._target_exists(info.get("targetId", "")):
                self.guard.log({"layer": "cdp", "kind": "attach", "target": ttype, "decision": "gone"})
                return
            # Interception could not be established: the target must never run.
            self.guard.log({"layer": "cdp", "kind": "attach", "target": ttype, "decision": "intercept_failed",
                            "reason": str(e)[:120]})
            if ttype != "iframe":  # closing an iframe target id would close its whole page
                try:
                    await self.conn.send("Target.closeTarget", {"targetId": info.get("targetId", "")})
                except CDPError:
                    pass
            if ttype in _DOC_TARGETS + _WORKER_TARGETS:
                self.guard.fail(f"could not intercept requests of a new {ttype or 'unknown'} target")
            return
        self.guard.log({"layer": "cdp", "kind": "attach", "target": ttype, "decision": "intercepting"})
        if params.get("waitingForDebugger"):
            await self.conn.send("Runtime.runIfWaitingForDebugger", {}, sid)

    async def _target_exists(self, tid: str) -> bool:
        """True unless Chrome confirms the target is gone (an unanswerable query counts as alive)."""
        try:
            targets = (await self.conn.send("Target.getTargets", {}, timeout=5)).get("targetInfos", [])
        except CDPError:
            return True
        return any(t.get("targetId") == tid for t in targets)

    async def _check_detach(self, info: dict) -> None:
        """A target that detached but still exists runs without interception: enforcement lost."""
        tid = info.get("targetId", "")
        for _ in range(4):  # destruction can trail the detach notification briefly
            try:
                targets = (await self.conn.send("Target.getTargets", {}, timeout=5)).get("targetInfos", [])
            except CDPError:
                if self.conn.on_close is None:
                    return  # deliberate shutdown
                self.guard.fail("could not confirm a detached target was destroyed")
                return
            if not any(t.get("targetId") == tid for t in targets):
                return
            await asyncio.sleep(0.25)
        self.guard.fail(f"a live {info.get('type') or 'unknown'} target detached from interception")

    def _on_frame_nav(self, params: dict) -> None:
        url, reason = params.get("url", ""), params.get("reason", "")
        now = time.monotonic()
        self.form_navs = {k: v for k, v in self.form_navs.items() if now - v[0] < FORM_NAV_TTL_S}
        self.form_navs[(params.get("frameId", ""), _nofrag(url))] = (now, reason)
        scheme = urlsplit(url).scheme.lower()
        if scheme in ("mailto", "tel", "sms") or (scheme and scheme not in _ALLOWED_NAV_SCHEMES + ("javascript",)):
            # External handlers are disabled in the worker profile; an email application needs a human.
            self.guard.violation(f"page requested a '{scheme}:' navigation (external application / email)",
                                 terminal=True)

    async def _form_reason(self, url: str, frame_id: str) -> str | None:
        key = (frame_id, _nofrag(url))
        deadline = time.monotonic() + FORM_NAV_WAIT_S
        while True:
            hit = self.form_navs.pop(key, None)  # consumed: one signal decides one request
            if hit:
                return hit[1]
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.02)

    async def _on_paused(self, params: dict, sid: str | None) -> None:
        req = params.get("request", {})
        rid = params["requestId"]
        url, method = req.get("url", ""), req.get("method", "GET")
        rtype = params.get("resourceType", "Other")
        info = self.sessions.get(sid or "", {})
        is_main = info.get("type") == "page" and params.get("frameId") == info.get("targetId")
        if self.selftest_urls:
            self.paused_urls.append(url)
            if url in self.selftest_urls:
                await self._continue(rid, sid)
                return
        form = None
        if rtype == "Document" and method.upper() == "GET":
            form = await self._form_reason(url, params.get("frameId", ""))
        decision = NP.decide_request(self.guard.policy_or_deny(), url=url, method=method, resource_type=rtype,
                                     is_main_frame=is_main,
                                     form_submission=bool(form and form.startswith("formSubmission")),
                                     headers=req.get("headers") or {}, has_body=bool(req.get("hasPostData")),
                                     sensitive_tokens=self.guard.sensitive_tokens)
        self.guard.log({"layer": "cdp", "kind": "request", "method": method, "type": rtype, "url": _strip(url),
                        "main": is_main, "decision": decision.action, "reason": decision.reason[:160]})
        if decision.action == "allow":
            await self._continue(rid, sid)
        elif decision.action == "approve":
            # Close the proxy write gate in this same loop step: if CDP is lost while the request
            # waits, Chrome releases it, but its bytes stop at the gate and the tunnel is revoked.
            self.guard.proxy.hold()
            task = asyncio.get_running_loop().create_task(self._await_approval(rid, sid, params, decision.reason))
            task.add_done_callback(self._approval_done)
        else:
            if not await self._fail(rid, sid) and method.upper() not in NP.SAFE_METHODS:
                self.guard.fail("a blocked state-changing request could not be confirmed as failed")
            if decision.boundary:
                self.guard.dry_run_boundary(method, rtype, url, decision.reason)
            elif decision.terminal:
                self.guard.violation(f"blocked request: {decision.reason}", terminal=True)

    def _approval_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            self.guard.fail(f"approval handling error: {type(task.exception()).__name__}")

    async def _await_approval(self, rid: str, sid: str | None, params: dict, reason: str) -> None:
        """Hold one state-changing request for a human. The proxy write gate is held on entry."""
        released = False
        try:
            req = params.get("request", {})
            body, complete = request_body(req)
            info = self.sessions.get(sid or "", {})
            action = {"method": req.get("method"), "url": req.get("url"), "resource_type": params.get("resourceType"),
                      "request_id": rid, "target_id": info.get("targetId", ""), "frame_id": params.get("frameId", ""),
                      "header_names": sorted({str(k).lower() for k in (req.get("headers") or {})}),
                      "body_sha256": hashlib.sha256(body).hexdigest(), "body_len": len(body),
                      "body_complete": complete}
            if not complete:
                # Chrome did not expose every byte (e.g. multipart file parts): an exact approval of
                # this payload is impossible, so it is refused and left for manual handling.
                self.guard.violation("state-changing request body could not be fully inspected; manual "
                                     "application required", terminal=False)
                await self._fail(rid, sid)
                return
            ok = await self.guard.request_approval(action, reason)
            if ok and not self.guard.failed.is_set() and sid in self.sessions:
                self.guard.proxy.release()
                released = True
                ok_sent = await self._continue(rid, sid)
                self.guard.record_release(action, "released" if ok_sent else "release_unconfirmed")
            else:
                await self._fail(rid, sid)
        finally:
            if not released:
                self.guard.proxy.release()

    async def _continue(self, rid: str, sid: str | None) -> bool:
        try:
            await self.conn.send("Fetch.continueRequest", {"requestId": rid}, sid)
            return True
        except CDPError:
            return False  # request already gone (tab closed / navigation cancelled) or unknown

    async def _fail(self, rid: str, sid: str | None) -> bool:
        """True when Chrome confirmed the failure or the request no longer exists."""
        try:
            await self.conn.send("Fetch.failRequest", {"requestId": rid, "errorReason": "BlockedByClient"}, sid)
            return True
        except CDPError as e:
            # "Invalid InterceptionId": the request is already gone (tab closed), nothing was sent.
            return "interception" in str(e).lower() and not self.guard.failed.is_set()

    # --- self-test -----------------------------------------------------------------
    async def selftest(self, proxy: EgressProxy) -> str | None:
        """Prove interception and proxy routing are in the request path. None = OK."""
        token = secrets.token_hex(12)
        proxy.selftest_token = token
        urls = {"loopback": f"http://127.0.0.1:{proxy.port}/applypilot-selftest/{token}",
                "name": f"http://{NP.SELFTEST_HOST}/applypilot-selftest/{token}"}
        self.selftest_urls = set(urls.values())
        pages = [s for s, i in self.sessions.items() if i.get("type") == "page"]
        if not pages:
            created = await self.conn.send("Target.createTarget", {"url": "about:blank"})
            for _ in range(100):
                pages = [s for s, i in self.sessions.items() if i.get("targetId") == created.get("targetId")]
                if pages:
                    break
                await asyncio.sleep(0.05)
        if not pages:
            return "no page target could be intercepted"
        sid = pages[0]
        try:
            for kind, url in urls.items():
                try:
                    await self.conn.send("Page.navigate", {"url": url}, sid, timeout=10)
                except CDPError:
                    pass  # 204 aborts the navigation; only what the layers observed matters
                for _ in range(100):
                    if url in self.paused_urls and (kind in proxy.selftest_seen or "direct" in proxy.selftest_seen):
                        break
                    await asyncio.sleep(0.05)
            await self.conn.send("Page.navigate", {"url": "about:blank"}, sid)
        finally:
            self.selftest_urls = set()
            proxy.selftest_token = None
        missing = [k for k, u in urls.items() if u not in self.paused_urls]
        if missing:
            return f"request interception not active ({', '.join(missing)})"
        if proxy.selftest_seen.get("direct") is not None or not all(proxy.selftest_seen.get(k) for k in urls):
            return f"browser traffic is not routed through the egress proxy ({proxy.selftest_seen})"
        return None


class NetworkGuard:
    """Owns the proxy + controller for one job. Thread-safe facade for the launcher."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="network-guard", daemon=True)
        self._thread.start()
        self.policy: P.SessionPolicy | None = None
        self.failed = threading.Event()
        self.failure_reason = ""
        self.on_failure: Callable[[], None] | None = None
        self.kill_browser: Callable[[], None] | None = None  # set by the worker: stop Chrome now
        self.sensitive_tokens: list[str] = []  # in memory only: applicant values that must not leave in URLs
        self.on_approval_request: Callable[[dict], None] | None = None
        self._lock = threading.Lock()
        self.proxy = EgressProxy(get_policy=lambda: None if self.failed.is_set() else self.policy,
                                 on_event=self.log)
        self.controller: BrowserController | None = None
        self.proxy_port = self._run(self.proxy.start(), 10)

    def _run(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def policy_or_deny(self) -> P.SessionPolicy:
        if self.policy is None or self.failed.is_set():
            # No policy (between jobs / after failure): an empty allowlist blocks everything.
            return P.SessionPolicy(session_dir="", dry_run=True, allowed_hosts=[], upload_paths=[])
        return self.policy

    # --- session state files ---------------------------------------------------------
    def log(self, rec: dict) -> None:
        pol = self.policy
        if pol is None:
            return
        with self._lock, open(pol.dir / NETWORK_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), **rec}) + "\n")

    def violation(self, reason: str, terminal: bool) -> None:
        if self.policy is not None:
            P.record_violation(self.policy, "network_guard", reason, terminal)

    def dry_run_boundary(self, method: str, rtype: str, url: str, reason: str) -> None:
        pol = self.policy
        if pol is None:
            return
        path = pol.dir / P.DRY_RUN_BOUNDARY_FILE
        if not path.exists():
            path.write_text(json.dumps({"t": time.time(), "method": method, "type": rtype,
                                        "url": _strip(url), "reason": reason}), encoding="utf-8")

    def fail(self, reason: str) -> None:
        if self.failed.is_set():
            return
        self.failure_reason = reason
        self.failed.set()
        # Containment first, independent of CDP: cut every tunnel, then stop the browser and agent.
        if threading.current_thread() is self._thread:
            self.proxy.revoke()
        else:
            self.loop.call_soon_threadsafe(self.proxy.revoke)
        if self.policy is not None:
            (self.policy.dir / P.GUARD_FAILED_FILE).write_text(reason, encoding="utf-8")
            self.violation(f"network guard failure: {reason}", terminal=True)
        for cb in (self.kill_browser, self.on_failure):
            if cb:
                try:
                    cb()
                except Exception:
                    logger.exception("network guard failure callback failed")

    def record_release(self, action: dict, outcome: str) -> None:
        """Launcher-side evidence that a human-approved write was handed back to Chrome."""
        pol = self.policy
        if pol is None:
            return
        P.record_audit(pol, {"phase": "network", "net_release": outcome, "t": time.time(),
                             "request_id": action.get("request_id"), "target_id": action.get("target_id"),
                             "frame_id": action.get("frame_id"), "method": action.get("method"),
                             "host": P.normalize_host(urlsplit(action.get("url") or "").hostname)})

    async def request_approval(self, action: dict, reason: str) -> bool:
        pol = self.policy
        if pol is None or self.failed.is_set():
            return False
        req = approval.create_request(pol.dir, kind="network", job_url=pol.job_url, action=action,
                                      ttl_s=pol.approval_timeout_s)
        if self.on_approval_request:
            try:
                self.on_approval_request(req)
            except Exception:
                logger.exception("approval notification failed")
        try:
            while time.time() <= req["expires"] and not self.failed.is_set():
                if approval.consume(pol.dir, req["nonce"], job_url=req["job_url"], digest=req["digest"]):
                    P.record_audit(pol, {"phase": "network", "decision": "granted", "net_approved": True,
                                         "nonce": req["nonce"], "t": time.time()})
                    return True
                await asyncio.sleep(0.5)
        finally:
            approval.withdraw(pol.dir, req["nonce"])
        self.violation(f"{reason}: not approved by a human ({action.get('method')} {_strip(action.get('url') or '')})",
                       terminal=False)
        return False

    # --- launcher API -------------------------------------------------------------
    def activate(self, policy: P.SessionPolicy, cdp_port: int, timeout: float = 30.0) -> None:
        """Attach to the browser and verify enforcement. Raises NetworkGuardError (fail closed)."""
        self.policy = policy
        status = policy.dir / P.GUARD_STATUS_FILE
        status.unlink(missing_ok=True)
        try:
            self.controller = BrowserController(self)
            self._run(self.controller.start(cdp_port), timeout)
            problem = self._run(self.controller.selftest(self.proxy), timeout)
        except NetworkGuardError:
            raise
        except Exception as e:  # noqa: BLE001 -- any attach/self-test error fails closed
            problem = f"could not attach to the browser ({type(e).__name__}: {str(e)[:80]})"
        if problem is None and self.failed.is_set():
            problem = self.failure_reason
        if problem:
            self.fail(problem)
            raise NetworkGuardError(problem)
        status.write_text(json.dumps({"active": True, "selftest": "ok", "proxy_port": self.proxy_port,
                                      "t": time.time()}), encoding="utf-8")

    def seal(self) -> None:
        """End of the agent session: cut all egress for good before the outcome is recorded."""
        try:
            self._run(self._seal(), 5)
        except Exception:
            logger.debug("network guard seal error", exc_info=True)

    async def _seal(self) -> None:
        self.proxy.revoke()

    def stop(self) -> None:
        try:
            if self.controller is not None:

                self._run(self.controller.stop(), 5)
            self._run(self.proxy.stop(), 5)
        except Exception:
            logger.debug("network guard shutdown error", exc_info=True)
        finally:
            self.policy = None
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout=5)


def request_body(req: dict) -> tuple[bytes, bool]:
    """(decoded request body, complete?). Incomplete when Chrome omitted any part of it."""
    entries = req.get("postDataEntries")
    if entries:
        out = b""
        for e in entries:
            if "bytes" not in e:
                return out, False  # e.g. a file element Chrome does not expose
            try:
                out += base64.b64decode(e["bytes"], validate=True)
            except (binascii.Error, ValueError):
                return out, False
        return out, True
    if req.get("postData") is not None:
        return str(req["postData"]).encode("utf-8"), True
    return b"", not req.get("hasPostData")


def chrome_network_args(proxy_port: int) -> list[str]:

    """Chrome flags that make the egress proxy the browser's only network route."""
    return [
        f"--proxy-server=http://127.0.0.1:{proxy_port}",
        "--proxy-bypass-list=<-loopback>",          # loopback goes through the proxy too
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",  # Chrome's own DNS always fails
        "--disable-quic",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--force-webrtc-ip-handling-policy",
        "--disable-background-networking",
    ]


def disable_external_protocols(profile_dir: Path) -> None:
    """Worker profile: never hand URLs (mailto:, tel:, ...) to external applications."""
    prefs_file = profile_dir / "Default" / "Preferences"
    prefs_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8")) if prefs_file.exists() else {}
    except ValueError:
        prefs = {}
    excluded = prefs.setdefault("protocol_handler", {}).setdefault("excluded_schemes", {})
    for scheme in ("mailto", "tel", "sms", "callto", "ms-outlook", "outlook", "skype", "msteams",
                   "zoommtg", "slack", "file", "ms-settings", "search-ms"):
        excluded[scheme] = True
    prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
