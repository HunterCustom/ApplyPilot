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
import hashlib
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from applypilot.apply import approval
from applypilot.apply import network_policy as NP
from applypilot.apply import policy as P
from applypilot.apply.cdp import CDPConnection, CDPError, browser_ws_url
from applypilot.apply.egress_proxy import EgressProxy

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


class NetworkGuardError(RuntimeError):
    pass


def _strip(url: str) -> str:
    """URL without query/fragment for logs (queries can carry personal data)."""
    try:
        p = urlsplit(url)
        return urlunsplit((p.scheme, p.netloc, p.path, "", ""))[:200]
    except ValueError:
        return "(unparseable)"


def _nofrag(url: str) -> str:
    return url.split("#", 1)[0]


class BrowserController:
    def __init__(self, guard: NetworkGuard) -> None:
        self.guard = guard
        self.conn = CDPConnection()
        self.sessions: dict[str, dict] = {}       # sessionId -> targetInfo
        self.intercepted: set[str] = set()        # sessions with Fetch interception enabled
        self.form_navs: dict[str, tuple[float, str]] = {}  # url(no fragment) -> (t, reason)
        self.paused_urls: list[str] = []
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
                self.sessions.pop(params.get("sessionId", ""), None)
                self.intercepted.discard(params.get("sessionId", ""))
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
            # Interception could not be established: the target must never run.
            self.guard.log({"layer": "cdp", "kind": "attach", "target": ttype, "decision": "intercept_failed",
                            "reason": str(e)[:120]})
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

    def _on_frame_nav(self, params: dict) -> None:
        url, reason = params.get("url", ""), params.get("reason", "")
        now = time.monotonic()
        self.form_navs = {u: v for u, v in self.form_navs.items() if now - v[0] < FORM_NAV_TTL_S}
        self.form_navs[_nofrag(url)] = (now, reason)
        scheme = urlsplit(url).scheme.lower()
        if scheme in ("mailto", "tel", "sms") or (scheme and scheme not in _ALLOWED_NAV_SCHEMES + ("javascript",)):
            # External handlers are disabled in the worker profile; an email application needs a human.
            self.guard.violation(f"page requested a '{scheme}:' navigation (external application / email)",
                                 terminal=True)

    async def _form_reason(self, url: str) -> str | None:
        key = _nofrag(url)
        deadline = time.monotonic() + FORM_NAV_WAIT_S
        while True:
            hit = self.form_navs.get(key)
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
        self.paused_urls.append(url)
        if url in self.selftest_urls:
            await self._continue(rid, sid)
            return
        form = None
        if rtype == "Document" and method.upper() == "GET":
            form = await self._form_reason(url)
        decision = NP.decide_request(self.guard.policy_or_deny(), url=url, method=method, resource_type=rtype,
                                     is_main_frame=is_main, form_submission=bool(form and form.startswith("formSubmission")))
        self.guard.log({"layer": "cdp", "kind": "request", "method": method, "type": rtype, "url": _strip(url),
                        "main": is_main, "decision": decision.action, "reason": decision.reason[:160]})
        if decision.action == "allow":
            await self._continue(rid, sid)
        elif decision.action == "approve":
            asyncio.get_running_loop().create_task(self._await_approval(rid, sid, params, decision.reason))
        else:
            await self._fail(rid, sid)
            if decision.boundary:
                self.guard.dry_run_boundary(method, rtype, url, decision.reason)
            elif decision.terminal:
                self.guard.violation(f"blocked request: {decision.reason}", terminal=True)

    async def _await_approval(self, rid: str, sid: str | None, params: dict, reason: str) -> None:
        req = params.get("request", {})
        body = "".join(e.get("bytes", "") for e in req.get("postDataEntries") or []) or req.get("postData") or ""
        action = {"method": req.get("method"), "url": req.get("url"), "resource_type": params.get("resourceType"),
                  "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "body_len": len(body)}
        ok = await self.guard.request_approval(action, reason)
        if ok:
            await self._continue(rid, sid)
        else:
            await self._fail(rid, sid)

    async def _continue(self, rid: str, sid: str | None) -> None:
        try:
            await self.conn.send("Fetch.continueRequest", {"requestId": rid}, sid)
        except CDPError:
            pass  # request already gone (tab closed / navigation cancelled)

    async def _fail(self, rid: str, sid: str | None) -> None:
        try:
            await self.conn.send("Fetch.failRequest", {"requestId": rid, "errorReason": "BlockedByClient"}, sid)
        except CDPError:
            pass

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
        if self.policy is not None:
            (self.policy.dir / P.GUARD_FAILED_FILE).write_text(reason, encoding="utf-8")
            self.violation(f"network guard failure: {reason}", terminal=True)
        cb = self.on_failure
        if cb:
            try:
                cb()
            except Exception:
                logger.exception("network guard on_failure callback failed")

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
                                         "nonce": req["nonce"]})
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
