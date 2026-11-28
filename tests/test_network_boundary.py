"""Phase 3b: launcher-owned network and submission boundary.

Unit tests (pure decisions, approvals, proxy over local sockets) plus integration
tests that drive a real headless Chrome *directly over CDP* -- no model, no agent --
so the enforcement is tested independently of whether a model follows instructions.
Everything is synthetic and local: two 127.0.0.1 servers, one playing the
"trusted application site" (listed in ``trusted_hosts``) and one the forbidden
internal target that must receive zero requests. Temp profiles and session dirs only.

Browser tests are skipped when Chrome is not installed or APPLYPILOT_SKIP_BROWSER_TESTS=1.
"""
import asyncio
import atexit
import http.server
import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import types
import unittest
from contextlib import suppress
from pathlib import Path
from unittest import mock

from applypilot.apply import approval, chrome, launcher, policy_hook, proc_jail
from applypilot.apply import network_policy as NP
from applypilot.apply import policy as P
from applypilot.apply.cdp import CDPConnection, browser_ws_url
from applypilot.apply.egress_proxy import EgressProxy
from applypilot.apply.network_guard import NetworkGuard, NetworkGuardError, disable_external_protocols

atexit.unregister(chrome.cleanup_on_exit)

JOB_URL = "https://careers.acme-corp.com/jobs/42"


def make_policy(tmp, dry_run=True, **kw):
    kw.setdefault("allowed_hosts", ["*.acme-corp.com", "*.greenhouse.io"])
    kw.setdefault("asset_hosts", list(NP.STATIC_ASSET_HOSTS))
    pol = P.SessionPolicy(session_dir=str(tmp), dry_run=dry_run, upload_paths=[], job_url=JOB_URL, **kw)
    pol.write()
    return pol


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name) / "session-test"
        self.tmp.mkdir()
        self.addCleanup(self._tmp.cleanup)


# --- Pure decisions ----------------------------------------------------------------

class IpBlockTests(unittest.TestCase):
    def test_non_public_addresses_blocked(self):
        for ip in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
                   "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fc00::1", "::", "::ffff:127.0.0.1",
                   "::ffff:169.254.169.254", "64:ff9b::7f00:1", "2002:7f00:1::1", "::127.0.0.1", "[::1]"):
            with self.subTest(ip=ip):
                self.assertIsNotNone(NP.ip_block_reason(ip))

    def test_public_addresses_allowed(self):
        for ip in ("8.8.8.8", "140.82.112.3", "2606:4700:4700::1111"):
            with self.subTest(ip=ip):
                self.assertIsNone(NP.ip_block_reason(ip))


class DecideRequestTests(TempDirCase):
    def d(self, url, method="GET", rtype="Document", main=True, form=False, **kw):
        return NP.decide_request(make_policy(self.tmp, **kw), url=url, method=method, resource_type=rtype,
                                 is_main_frame=main, form_submission=form)

    def test_private_and_alternate_ip_forms_blocked_terminally(self):
        for url in ("http://127.0.0.1/", "http://2130706433/", "http://0x7f.1/", "http://127.1/", "http://[::1]/",
                    "http://[::ffff:7f00:1]/", "http://169.254.169.254/latest/meta-data/", "http://localhost:8080/",
                    "https://intranet.local/", "ws://127.0.0.1:9222/devtools/browser"):
            for rtype, main in (("Document", True), ("Image", False), ("XHR", False), ("Script", False)):
                with self.subTest(url=url, rtype=rtype):
                    dec = self.d(url, rtype=rtype, main=main)
                    self.assertEqual(dec.action, "block")
                    self.assertTrue(dec.terminal)

    def test_forbidden_schemes_blocked(self):
        for url in ("file:///C:/Windows/win.ini", "chrome://settings", "javascript:alert(1)", "ftp://x.acme-corp.com/"):
            with self.subTest(url=url):
                self.assertEqual(self.d(url).action, "block")

    def test_off_allowlist_navigation_blocked_subresource_blocked_quietly(self):
        nav = self.d("https://evil.example/")
        self.assertEqual((nav.action, nav.terminal), ("block", True))
        sub = self.d("https://tracker.example/pixel.gif", rtype="Image", main=False)
        self.assertEqual((sub.action, sub.terminal), ("block", False))

    def test_asset_hosts_only_for_get_subresources(self):
        self.assertEqual(self.d("https://fonts.gstatic.com/a.woff2", rtype="Font", main=False).action, "allow")
        self.assertEqual(self.d("https://cdnjs.cloudflare.com/x.js", rtype="Script", main=False).action, "allow")
        for rtype, method in (("Document", "GET"), ("XHR", "GET"), ("Fetch", "POST"), ("Script", "POST")):
            with self.subTest(rtype=rtype, method=method):
                self.assertEqual(self.d("https://cdnjs.cloudflare.com/x", method=method, rtype=rtype,
                                        main=rtype == "Document").action, "block")

    def test_page_references_never_widen_the_allowlist(self):
        # An employer page referencing a domain does not make it reachable: only the policy decides.
        self.assertEqual(self.d("https://attacker-cdn.example/lib.js", rtype="Script", main=False).action, "block")

    def test_dry_run_state_changing_requests_hit_boundary(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "post"):
            for rtype in ("Document", "XHR", "Fetch", "Ping", "Other"):
                with self.subTest(method=method, rtype=rtype):
                    dec = self.d("https://careers.acme-corp.com/apply", method=method, rtype=rtype)
                    self.assertEqual((dec.action, dec.boundary), ("block", True))
        dec = self.d("https://careers.acme-corp.com/apply?name=x", form=True)
        self.assertEqual((dec.action, dec.boundary), ("block", True))

    def test_dry_run_reads_allowed(self):
        self.assertEqual(self.d("https://careers.acme-corp.com/jobs/42").action, "allow")
        pre = NP.decide_request(make_policy(self.tmp), url="https://boards.greenhouse.io/x", method="OPTIONS",
                                resource_type="Preflight", headers={"Access-Control-Request-Method": "POST"})
        self.assertEqual(pre.action, "allow")
        # OPTIONS without the preflight header (script-made) or with a body is state-changing (audit H2)
        self.assertEqual(self.d("https://boards.greenhouse.io/x", method="OPTIONS", rtype="Fetch", main=False).action,
                         "block")

    def test_real_run_state_changing_requests_need_approval(self):
        dec = self.d("https://careers.acme-corp.com/apply", method="POST", rtype="XHR", dry_run=False)
        self.assertEqual(dec.action, "approve")
        self.assertEqual(self.d("https://careers.acme-corp.com/apply", form=True, dry_run=False).action, "approve")
        # still blocked outright when the destination is forbidden
        self.assertEqual(self.d("https://evil.example/", method="POST", rtype="XHR", dry_run=False).action, "block")

    def test_proxy_host_rule(self):
        pol = make_policy(self.tmp)
        self.assertIsNone(NP.proxy_host_reason(pol, "boards.greenhouse.io", 443))
        self.assertIsNone(NP.proxy_host_reason(pol, "fonts.gstatic.com", 443))
        for host, port in (("boards.greenhouse.io", 80), ("boards.greenhouse.io", 8443), ("evil.example", 443),
                           ("127.0.0.1", 443), ("2130706433", 443), ("localhost", 443)):
            with self.subTest(host=host, port=port):
                self.assertIsNotNone(NP.proxy_host_reason(pol, host, port))
        self.assertIsNotNone(NP.proxy_host_reason(None, "boards.greenhouse.io", 443))


# --- Approvals ---------------------------------------------------------------------

class ApprovalTests(TempDirCase):
    ACTION = types.MappingProxyType({"method": "POST", "url": "https://careers.acme-corp.com/apply",
                                     "body_sha256": "ab"})

    def req(self, ttl=60, now=1000.0, **kw):
        return approval.create_request(self.tmp, kind="network", job_url=JOB_URL,
                                       action=kw.get("action", dict(self.ACTION)), ttl_s=ttl, now=now)

    def test_missing_approval_rejected(self):
        r = self.req()
        self.assertFalse(approval.consume(self.tmp, r["nonce"], job_url=JOB_URL, digest=r["digest"], now=1001))

    def test_valid_grant_consumed_exactly_once(self):
        r = self.req()
        self.assertTrue(approval.grant(self.tmp, r["nonce"], now=1001))
        self.assertTrue(approval.consume(self.tmp, r["nonce"], job_url=JOB_URL, digest=r["digest"], now=1002))
        self.assertFalse(approval.consume(self.tmp, r["nonce"], job_url=JOB_URL, digest=r["digest"], now=1003))

    def test_replayed_grant_rejected(self):
        r = self.req()
        approval.grant(self.tmp, r["nonce"], now=1001)
        saved = {p.name: p.read_text() for p in (self.tmp / approval.APPROVAL_DIR).iterdir()}
        self.assertTrue(approval.consume(self.tmp, r["nonce"], job_url=JOB_URL, digest=r["digest"], now=1002))
        for name, text in saved.items():  # attacker restores the old request + grant files
            (self.tmp / approval.APPROVAL_DIR / name).write_text(text)
        self.assertFalse(approval.consume(self.tmp, r["nonce"], job_url=JOB_URL, digest=r["digest"], now=1003))
        self.assertFalse(approval.grant(self.tmp, r["nonce"], now=1003))

    def test_expired_request_cannot_be_granted_or_consumed(self):
        r = self.req(ttl=10)
        self.assertFalse(approval.grant(self.tmp, r["nonce"], now=1011))
        r2 = self.req(ttl=10)
        self.assertTrue(approval.grant(self.tmp, r2["nonce"], now=1005))
        self.assertFalse(approval.consume(self.tmp, r2["nonce"], job_url=JOB_URL, digest=r2["digest"], now=1011))

    def test_grant_bound_to_action_job_and_session(self):
        r = self.req()
        approval.grant(self.tmp, r["nonce"], now=1001)
        other = approval.action_digest({**dict(self.ACTION), "url": "https://careers.acme-corp.com/other"})
        self.assertFalse(approval.consume(self.tmp, r["nonce"], job_url=JOB_URL, digest=other, now=1002))
        self.assertFalse(approval.consume(self.tmp, r["nonce"], job_url="https://other.example/job", digest=r["digest"], now=1002))
        other_session = self.tmp.parent / "session-other"
        shutil.copytree(self.tmp, other_session)
        self.assertFalse(approval.consume(other_session, r["nonce"], job_url=JOB_URL, digest=r["digest"], now=1002))
        # tampered request digest on disk: no longer matches the enforcing caller's in-memory action
        path = self.tmp / approval.APPROVAL_DIR / f"req-{r['nonce']}.json"
        data = json.loads(path.read_text())
        self.assertNotIn("action", data)  # only a sanitized display copy is persisted (audit M4)
        data["digest"] = other
        path.write_text(json.dumps(data))
        self.assertFalse(approval.consume(self.tmp, r["nonce"], job_url=JOB_URL, digest=r["digest"], now=1002))
        # a grant for a request the human was not shown is refused
        r2 = self.req()
        self.assertFalse(approval.grant(self.tmp, r2["nonce"], now=1001, expected_digest=other))

    def test_malformed_nonces_rejected(self):
        for nonce in ("", "abc", "../../etc", "Z" * 32, None):
            with self.subTest(nonce=nonce):
                self.assertFalse(approval.grant(self.tmp, nonce))  # type: ignore[arg-type]

    def test_wait_for_grant_times_out_and_withdraws(self):
        clock = iter(range(1000, 2000, 5))
        r = self.req(ttl=20)
        self.assertFalse(approval.wait_for_grant(self.tmp, r, sleep=lambda s: None, clock=lambda: next(clock)))
        self.assertIsNone(approval.pending_request(self.tmp, r["nonce"]))


# --- Policy hook integration ---------------------------------------------------------

class HookGuardTests(TempDirCase):
    PW = "mcp__playwright__"

    def pre(self, pol, tool="browser_snapshot", inp=None):
        with mock.patch("sys.stderr", io.StringIO()) as err:
            rc = policy_hook.handle_pre(pol, {"tool_name": self.PW + tool, "tool_input": inp or {}})
        return rc, err.getvalue()

    def test_every_tool_denied_without_verified_guard(self):
        pol = make_policy(self.tmp)
        rc, err = self.pre(pol)
        self.assertEqual(rc, 2)
        self.assertIn("network guard is not active", err)
        (self.tmp / P.GUARD_STATUS_FILE).write_text(json.dumps({"active": True, "selftest": "failed"}))
        self.assertEqual(self.pre(pol)[0], 2)

    def test_guard_failure_halts_tools(self):
        pol = make_policy(self.tmp)
        (self.tmp / P.GUARD_STATUS_FILE).write_text(json.dumps({"active": True, "selftest": "ok"}))
        self.assertEqual(self.pre(pol)[0], 0)
        (self.tmp / P.GUARD_FAILED_FILE).write_text("lost CDP")
        self.assertEqual(self.pre(pol)[0], 2)

    def test_dry_run_boundary_ends_session_as_dry_run(self):
        pol = make_policy(self.tmp)
        (self.tmp / P.GUARD_STATUS_FILE).write_text(json.dumps({"active": True, "selftest": "ok"}))
        (self.tmp / P.DRY_RUN_BOUNDARY_FILE).write_text("{}")
        rc, err = self.pre(pol, "browser_click", {"element": "Continue"})
        self.assertEqual(rc, 2)
        self.assertIn(policy_hook.GUARD_PREFIX, err)
        self.assertIn("RESULT:DRY_RUN", err)
        self.assertFalse((self.tmp / "violations.jsonl").exists())  # a boundary is not a violation

    def test_error_page_after_guard_block_is_not_a_second_violation(self):
        resp = {"tool_name": self.PW + "browser_click",
                "tool_response": "### Page\n- Page URL: chrome-error://chromewebdata/\n"}
        pol = make_policy(self.tmp)
        with mock.patch("sys.stdout", io.StringIO()) as out:  # no verified guard: tripwire still fires
            policy_hook.handle_post(pol, resp)
        self.assertIn("block", out.getvalue())
        (self.tmp / "violations.jsonl").unlink()
        (self.tmp / P.GUARD_STATUS_FILE).write_text(json.dumps({"active": True, "selftest": "ok"}))
        with mock.patch("sys.stdout", io.StringIO()) as out:
            policy_hook.handle_post(pol, resp)
        self.assertEqual(out.getvalue(), "")
        self.assertFalse((self.tmp / "violations.jsonl").exists())
        # a real off-site page URL still halts the session
        with mock.patch("sys.stdout", io.StringIO()) as out:
            policy_hook.handle_post(pol, {"tool_name": self.PW + "browser_click",
                                          "tool_response": "- Page URL: https://evil.example/x"})
        self.assertIn("block", out.getvalue())

    def test_launcher_audit_requires_verified_guard(self):
        pol = make_policy(self.tmp)
        (self.tmp / "hook_alive").write_text("1")
        P.record_audit(pol, {"phase": "pre", "tool": "x", "decision": "allow"})
        self.assertEqual(launcher.audit_session(self.tmp, 1), "uncertain:network_guard_not_verified")
        (self.tmp / P.GUARD_STATUS_FILE).write_text(json.dumps({"active": True, "selftest": "ok"}))
        self.assertIsNone(launcher.audit_session(self.tmp, 1))
        (self.tmp / P.GUARD_FAILED_FILE).write_text("x")
        self.assertEqual(launcher.audit_session(self.tmp, 1), "uncertain:network_guard_failed")

    def test_real_run_evidence_needs_network_level_release(self):
        pol = make_policy(self.tmp, dry_run=False)
        P.record_audit(pol, {"phase": "pre", "tool": "x", "decision": "allow", "submit_approved": True})
        P.record_audit(pol, {"phase": "post", "tool": "x", "evidence": "confirmation_text"})
        ev = launcher.submission_evidence(self.tmp)
        self.assertEqual((ev["approved"], ev["confirmed"], ev["correlated"]), (False, True, False))
        # a grant alone is not a release, and a confirmation seen *before* the release proves nothing
        P.record_audit(pol, {"phase": "network", "decision": "granted", "net_approved": True})
        P.record_audit(pol, {"phase": "network", "net_release": "released"})
        ev = launcher.submission_evidence(self.tmp)
        self.assertEqual((ev["approved"], ev["released"], ev["correlated"]), (True, 1, False))
        time.sleep(0.01)
        P.record_audit(pol, {"phase": "post", "tool": "x", "evidence": "confirmation_text"})
        self.assertTrue(launcher.submission_evidence(self.tmp)["correlated"])



# --- Synthetic servers ---------------------------------------------------------------

class Hits:
    def __init__(self):
        self.lock = threading.Lock()
        self.items: list[tuple[str, str]] = []

    def add(self, method, path):
        with self.lock:
            self.items.append((method, path))

    def count(self, method=None, prefix=""):
        with self.lock:
            return sum(1 for m, p in self.items if (method is None or m == method) and p.startswith(prefix))


def serve(handler_cls):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def forbidden_server(hits: Hits):
    class H(http.server.BaseHTTPRequestHandler):
        def _any(self):
            hits.add(self.command, self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>internal secret</h1>")
        do_GET = do_POST = do_PUT = do_HEAD = _any

        def log_message(self, *a):
            pass
    return serve(H)


def app_server(hits: Hits, forbidden_url: str):
    """The synthetic 'application site'. Pages try every way to leave or to submit."""
    F = forbidden_url
    pages = {
        "/": "<h1>Synthetic job</h1>",
        "/redirect": None,
        "/subresources": f"""<h1>subresources</h1><img src="{F}/img.png"><script src="{F}/x.js"></script>
            <link rel=stylesheet href="{F}/s.css"><iframe src="{F}/frame"></iframe>
            <script>fetch("{F}/api").catch(()=>{{}}); navigator.sendBeacon("{F}/beacon","x");
            var x=new XMLHttpRequest(); x.open("GET","{F}/xhr"); x.send();</script>""",
        "/continue-post": """<form method=post action="/apply"><input name=full_name value="Synthetic Name">
            <button id=b type=submit>Continue</button></form>""",
        "/next-get": """<form method=get action="/apply-get"><input name=full_name value="Synthetic Name">
            <button id=b>Next</button></form>""",
        "/js-submit": """<button id=b onclick="fetch('/apply-js',{method:'POST',body:'name=synthetic'});
            var x=new XMLHttpRequest();x.open('PUT','/apply-xhr');x.send('x');
            navigator.sendBeacon('/apply-beacon','x');">Finish</button>""",
        "/keyboard": """<form method=post action="/apply-key"><input id=f name=full_name value="Synthetic"></form>""",
        "/auto": """<form id=f method=post action="/apply-auto"><input name=a value=1></form>
            <script>setTimeout(()=>document.getElementById('f').submit(),100)</script>""",
        "/worker": """<script>var w=new Worker('/worker.js');</script>""",
        "/worker.js": "fetch('/apply-worker',{method:'POST',body:'x'});",
        "/sw-page": """<script>navigator.serviceWorker.register('/sw.js')</script>""",
        "/sw.js": "self.addEventListener('install',e=>e.waitUntil(fetch('/apply-sw',{method:'POST',body:'x'})"
                  ".catch(()=>0)));",
        "/popup": f"""<button id=b onclick="window.open('{F}/popup')">Apply</button>""",
        "/ws": """<script>try{new WebSocket('ws://'+location.host+'/socket')}catch(e){}</script>""",
        "/ws-worker": """<script>var w=new Worker('/ws-worker.js');</script>""",
        "/ws-worker.js": "try{new WebSocket('ws://'+location.host+'/socket-w')}catch(e){}",
        # No mailto:/tel: or other external-scheme fixture on purpose: activating one can hand the URL
        # to the operating system's default handler (audit H4 -- it started a personal browser).
    }

    class H(http.server.BaseHTTPRequestHandler):
        def _any(self):
            hits.add(self.command, self.path)
            path = self.path.split("?")[0]
            if path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"{F}/landing")
                self.end_headers()
                return
            body = pages.get(path, "<p>ok</p>") or ""
            self.send_response(200)
            ctype = "text/javascript" if path.endswith(".js") else "text/html"
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body.encode())
        do_GET = do_POST = do_PUT = do_HEAD = _any

        def log_message(self, *a):
            pass
    return serve(H)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- Proxy (no browser) ----------------------------------------------------------------

class ProxyTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.hits = Hits()
        self.target = forbidden_server(self.hits)
        self.addCleanup(self.target.shutdown)
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def proxy_request(self, raw: bytes, resolver=None, policy=None) -> bytes:
        pol = policy or make_policy(self.tmp)
        kw = {"resolver": resolver} if resolver else {}
        proxy = EgressProxy(get_policy=lambda: pol, **kw)

        async def go():
            port = await proxy.start()
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(raw)
            await w.drain()
            try:
                data = await asyncio.wait_for(r.read(4096), 5)
            finally:
                w.close()
                await proxy.stop()
            return data
        return self.loop.run_until_complete(go())

    def test_private_targets_refused_zero_hits(self):
        port = self.target.server_address[1]
        for raw in (f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n", f"GET http://127.0.0.1:{port}/x HTTP/1.1\r\n\r\n",
                    f"GET http://2130706433:{port}/x HTTP/1.1\r\n\r\n", f"CONNECT [::1]:{port} HTTP/1.1\r\n\r\n",
                    f"POST http://localhost:{port}/x HTTP/1.1\r\nContent-Length: 1\r\n\r\nx"):
            with self.subTest(raw=raw.split()[1]):
                self.assertIn(b"403", self.proxy_request(raw.encode()).split(b"\r\n")[0])
        self.assertEqual(self.hits.count(), 0)

    def test_dns_rebinding_to_private_address_refused(self):
        port = self.target.server_address[1]
        calls = []

        async def rebinding(host, p):
            calls.append(host)
            return ["93.184.216.34", "127.0.0.1"] if len(calls) == 1 else ["127.0.0.1"]

        pol = make_policy(self.tmp, trusted_hosts=[])
        out = self.proxy_request(b"CONNECT careers.acme-corp.com:443 HTTP/1.1\r\n\r\n", rebinding, pol)
        self.assertIn(b"403", out.split(b"\r\n")[0])

        async def private(host, p):
            return ["10.0.0.5"]
        out = self.proxy_request(b"CONNECT careers.acme-corp.com:443 HTTP/1.1\r\n\r\n", private, pol)
        self.assertIn(b"403", out.split(b"\r\n")[0])
        self.assertEqual(self.hits.count(), 0)
        del port

    def test_vetted_address_is_the_one_connected(self):
        proxy = EgressProxy(get_policy=lambda: make_policy(self.tmp), resolver=self._public)
        addr, why = self.loop.run_until_complete(proxy.vet("careers.acme-corp.com", 443))
        self.assertEqual((addr, why), ("93.184.216.34", None))

    @staticmethod
    async def _public(host, port):
        return ["93.184.216.34"]

    def test_plain_http_never_forwarded_upstream(self):
        out = self.proxy_request(b"GET http://careers.acme-corp.com/jobs/42 HTTP/1.1\r\n\r\n")
        self.assertIn(b"307", out.split(b"\r\n")[0])
        self.assertIn(b"Location: https://careers.acme-corp.com/jobs/42", out)
        out = self.proxy_request(b"POST http://careers.acme-corp.com/apply HTTP/1.1\r\nContent-Length: 1\r\n\r\nx")
        self.assertIn(b"403", out.split(b"\r\n")[0])

    def test_no_policy_denies_everything(self):
        proxy = EgressProxy(get_policy=lambda: None)
        addr, why = self.loop.run_until_complete(proxy.vet("boards.greenhouse.io", 443))
        self.assertIsNone(addr)
        self.assertIn("no active session policy", why or "")


# --- Real browser, driven directly over CDP (no model) ------------------------------------

# Browser tests use exactly this executable -- never CHROME_PATH, never another browser binary.
INSTALLED_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def _chrome_exe():
    if os.environ.get("APPLYPILOT_SKIP_BROWSER_TESTS") == "1" or not proc_jail.jail_supported():
        return None
    if not os.path.isfile(INSTALLED_CHROME):
        return None  # browser tests skip
    return chrome.require_chrome_binary(INSTALLED_CHROME)


CHROME = _chrome_exe()


class Driver:
    """Plays the agent's browser: its own CDP client, like Playwright MCP would be."""

    def __init__(self, loop, cdp_port):
        self.loop, self.port = loop, cdp_port
        self.conn = CDPConnection()
        self.sid = None

    def run(self, coro, timeout=20):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self):
        async def go():
            await self.conn.connect(browser_ws_url(self.port))
            page = None
            for _ in range(50):  # the initial tab can be replaced during start-up (audit L2)
                targets = (await self.conn.send("Target.getTargets"))["targetInfos"]
                page = next((t for t in targets if t["type"] == "page"), None)
                if page:
                    break
                await asyncio.sleep(0.1)
            assert page is not None, "no page target"
            self.sid = (await self.conn.send("Target.attachToTarget",
                                             {"targetId": page["targetId"], "flatten": True}))["sessionId"]
            await self.conn.send("Page.enable", {}, self.sid)
            await self.conn.send("Runtime.enable", {}, self.sid)
        self.run(go())

    def navigate(self, url, settle=1.2):
        async def go():
            with suppress(Exception):  # blocked navigations raise; the server counts matter
                await self.conn.send("Page.navigate", {"url": url}, self.sid, timeout=10)
            await asyncio.sleep(settle)
        self.run(go())

    def js(self, expr, settle=1.2):
        async def go():
            with suppress(Exception):
                await self.conn.send("Runtime.evaluate", {"expression": expr, "userGesture": True}, self.sid)
            await asyncio.sleep(settle)
        self.run(go())

    def eval(self, expr):
        async def go():
            r = await self.conn.send("Runtime.evaluate", {"expression": expr, "returnByValue": True}, self.sid)
            return r.get("result", {}).get("value")
        return self.run(go())

    def press_enter(self, settle=1.2):
        async def go():
            for t in ("keyDown", "keyUp"):
                await self.conn.send("Input.dispatchKeyEvent", {"type": t, "key": "Enter", "code": "Enter",
                                                                "windowsVirtualKeyCode": 13, "text": "\r"}, self.sid)
            await asyncio.sleep(settle)
        self.run(go())

    def stop(self):
        with suppress(Exception):
            self.run(self.conn.close(), 5)


@unittest.skipUnless(CHROME, "Chrome not available (or APPLYPILOT_SKIP_BROWSER_TESTS=1)")
class BrowserBoundaryTests(TempDirCase):
    @classmethod
    def setUpClass(cls):
        cls.forbidden_hits, cls.app_hits = Hits(), Hits()
        cls.forbidden = forbidden_server(cls.forbidden_hits)
        cls.F = f"http://127.0.0.1:{cls.forbidden.server_address[1]}"
        cls.app = app_server(cls.app_hits, cls.F)
        cls.A_HOSTPORT = f"127.0.0.1:{cls.app.server_address[1]}"
        cls.A = f"http://{cls.A_HOSTPORT}"
        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.forbidden.shutdown()
        cls.app.shutdown()
        cls.loop.call_soon_threadsafe(cls.loop.stop)

    def launch(self, dry_run=True, with_proxy=True, **kw):
        self.forbidden_hits.items.clear()
        self.app_hits.items.clear()
        self.guard = NetworkGuard()
        self.addCleanup(self.guard.stop)
        profile = Path(self._tmp.name) / "profile"
        disable_external_protocols(profile)
        port = free_port()
        assert CHROME is not None
        cmd = chrome.chrome_command(CHROME, port, profile, True, self.guard.proxy_port if with_proxy else None)
        # Same containment as the launcher: Chrome runs in a process jail; any other program it
        # starts is terminated and fails the guard (audit H4).
        self.escapes = []
        self.jail = proc_jail.ProcessJail([CHROME], lambda why: (self.escapes.append(why), self.guard.fail(why)))
        self.addCleanup(self.jail.close)
        self.proc = self.jail.spawn(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._kill_chrome)
        for _ in range(100):
            try:
                browser_ws_url(port, timeout=0.5)
                break
            except Exception:  # noqa: BLE001 -- Chrome still starting
                time.sleep(0.1)
        self.policy = make_policy(self.tmp, dry_run=dry_run, trusted_hosts=[self.A_HOSTPORT], **kw)
        self.guard.activate(self.policy, port)  # raises when enforcement cannot be verified
        self.driver = Driver(self.loop, port)
        self.driver.start()
        self.addCleanup(self.driver.stop)
        return port

    def _kill_chrome(self):
        self.proc.kill()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            pass

    def violations(self):
        return P.read_jsonl(self.tmp / "violations.jsonl")

    def boundary(self):
        return (self.tmp / P.DRY_RUN_BOUNDARY_FILE).exists()

    def held(self, fragment):
        """(method, decision) of every request the controller held whose URL contains fragment."""
        return [(r["method"], r["decision"]) for r in P.read_jsonl(self.tmp / "network.jsonl")
                if r.get("kind") == "request" and fragment in r.get("url", "")]

    # -- network boundary --
    def test_guard_verified_before_agent_could_start(self):
        self.launch()
        status = json.loads((self.tmp / P.GUARD_STATUS_FILE).read_text())
        self.assertEqual((status["active"], status["selftest"]), (True, "ok"))
        self.assertIsNone(P.network_guard_problem(self.policy))

    def test_forbidden_redirect_zero_requests_at_target(self):
        self.launch()
        self.driver.navigate(self.A + "/redirect", settle=1.5)
        self.assertEqual(self.app_hits.count("GET", "/redirect"), 1)
        self.assertEqual(self.forbidden_hits.count(), 0)
        self.assertEqual(self.held("/landing"), [("GET", "block")])  # the redirect hop itself was held
        self.assertTrue(any(v["terminal"] for v in self.violations()))

    def test_forbidden_subresources_zero_requests_at_target(self):
        self.launch()
        self.driver.navigate(self.A + "/subresources", settle=2.0)
        self.assertEqual(self.app_hits.count("GET", "/subresources"), 1)
        self.assertEqual(self.forbidden_hits.count(), 0)

    def test_private_network_navigation_and_popup_blocked(self):
        self.launch()
        self.driver.navigate(self.F + "/secret")
        self.driver.navigate(f"http://2130706433:{self.forbidden.server_address[1]}/alt")
        self.driver.navigate(self.A + "/popup")
        self.driver.js("document.getElementById('b').click()", settle=1.5)
        self.assertEqual(self.forbidden_hits.count(), 0)

    def test_file_scheme_not_reachable(self):
        self.launch()
        # Even a CDP-level navigation (not via the agent's hooked tool) is held by Fetch and failed.
        self.driver.navigate("file:///C:/Windows/win.ini")
        self.assertTrue(any("scheme 'file'" in v["reason"] and v["terminal"] for v in self.violations()))
        net = P.read_jsonl(self.tmp / "network.jsonl")
        self.assertTrue(any(r.get("url", "").startswith("file:") and r["decision"] == "block" for r in net))
        self.assertTrue(P.session_halted(self.policy))
        # Prevented, not just logged: the page never received the file's contents.
        body = self.driver.eval("document.documentElement ? document.documentElement.innerText : ''") or ""
        self.assertNotIn("16-bit", body)
        self.assertFalse(str(self.driver.eval("location.href")).startswith("file:"))

    # -- dry-run submission boundary (label-free) --
    def test_continue_button_cannot_transmit_in_dry_run(self):
        self.launch()
        self.driver.navigate(self.A + "/continue-post")
        self.driver.js("document.getElementById('b').click()")
        self.assertEqual(self.app_hits.count("POST"), 0)
        self.assertTrue(self.boundary())

    def test_next_button_get_form_cannot_transmit_in_dry_run(self):
        self.launch()
        self.driver.navigate(self.A + "/next-get")
        self.driver.js("document.getElementById('b').click()")
        self.assertEqual(self.app_hits.count("GET", "/apply-get"), 0)
        self.assertTrue(self.boundary())

    def test_javascript_submissions_blocked(self):
        self.launch()
        self.driver.navigate(self.A + "/js-submit")
        self.driver.js("document.getElementById('b').click()", settle=1.5)
        self.assertEqual(self.app_hits.count("POST") + self.app_hits.count("PUT"), 0)
        self.assertTrue(self.boundary())

    def test_keyboard_and_automatic_submissions_blocked(self):
        self.launch()
        self.driver.navigate(self.A + "/keyboard")
        self.driver.js("document.getElementById('f').focus()", settle=0.2)
        self.driver.press_enter()
        self.driver.navigate(self.A + "/auto", settle=1.5)
        self.assertEqual(self.app_hits.count("POST"), 0)
        self.assertEqual(self.held("apply-key"), [("POST", "block")])
        self.assertEqual(self.held("apply-auto"), [("POST", "block")])
        self.assertTrue(self.boundary())

    def test_worker_requests_intercepted(self):
        self.launch()
        self.driver.navigate(self.A + "/worker", settle=2.0)
        self.assertEqual(self.app_hits.count("GET", "/worker.js"), 1)  # the worker really ran
        self.assertEqual(self.app_hits.count("POST"), 0)
        self.assertEqual(self.held("apply-worker"), [("POST", "block")])
        self.assertTrue(self.boundary())
        self.assertFalse(self.guard.failed.is_set())

    def test_service_worker_requests_intercepted(self):
        self.launch()
        self.driver.navigate(self.A + "/sw-page", settle=3.0)
        self.assertEqual(self.app_hits.count("GET", "/sw.js"), 1)  # the service worker really installed
        self.assertEqual(self.app_hits.count("POST"), 0)
        self.assertEqual(self.held("apply-sw"), [("POST", "block")])
        self.assertFalse(self.guard.failed.is_set())

    def test_page_websocket_and_webrtc_removed(self):
        self.launch()
        self.driver.navigate(self.A + "/ws", settle=1.0)
        self.assertEqual(self.driver.eval("[typeof WebSocket, typeof RTCPeerConnection, typeof WebTransport]"
                                          ".join()"), "undefined,undefined,undefined")
        self.assertEqual(self.app_hits.count("GET", "/socket"), 0)
        self.assertFalse(self.guard.failed.is_set())

    def test_worker_websocket_stops_the_session(self):
        self.launch()
        self.driver.navigate(self.A + "/ws-worker", settle=2.0)
        self.assertTrue(self.guard.failed.is_set())
        self.assertIsNotNone(P.network_guard_problem(self.policy))

    def test_browser_runs_inside_the_process_jail(self):
        self.launch()
        self.assertTrue(self.jail.contains(self.proc.pid))
        for page in ("/", "/popup", "/worker", "/sw-page"):
            self.driver.navigate(self.A + page, settle=0.8)
        self.assertEqual(self.escapes, [])
        # Prevention, not detection: every process that exists in the browser's job is that Chrome.
        images = {os.path.normcase(os.path.realpath(p)) for p in self.jail.images()}
        self.assertGreater(len(self.jail.pids()), 1)  # renderers etc. really are inside the job
        self.assertEqual(images, {os.path.normcase(os.path.realpath(CHROME))})


    # -- real-run approval at the network layer --
    def test_real_run_post_needs_single_use_grant(self):
        self.launch(dry_run=False, approval_timeout_s=4)
        self.driver.navigate(self.A + "/js-submit")
        granted = []

        def approve_first():
            for _ in range(80):
                reqs = list((self.tmp / approval.APPROVAL_DIR).glob("req-*.json")) \
                    if (self.tmp / approval.APPROVAL_DIR).exists() else []
                if reqs:
                    nonce = json.loads(reqs[0].read_text())["nonce"]
                    granted.append(approval.grant(self.tmp, nonce))
                    return
                time.sleep(0.05)
        t = threading.Thread(target=approve_first)
        t.start()
        self.driver.js("fetch('/apply-js',{method:'POST',body:'one'})", settle=2.0)
        t.join()
        self.assertEqual(granted, [True])
        self.assertEqual(self.app_hits.count("POST", "/apply-js"), 1)
        # no grant for the next one: it expires and is never sent
        self.driver.js("fetch('/apply-js',{method:'POST',body:'two'})", settle=5.0)
        self.assertEqual(self.app_hits.count("POST", "/apply-js"), 1)
        self.assertTrue(any("not approved" in v["reason"] for v in self.violations()))

    # -- fail closed --
    def test_guard_refuses_browser_that_bypasses_the_proxy(self):
        with self.assertRaises(NetworkGuardError) as cm:
            self.launch(with_proxy=False)
        self.assertIn("proxy", str(cm.exception))
        self.assertIsNotNone(P.network_guard_problem(self.policy))

    def test_held_write_never_sent_when_controller_is_lost(self):
        """Audit H1 regression, receiver side: a POST held for approval when CDP drops never arrives."""
        self.launch(dry_run=False, approval_timeout_s=20)
        self.driver.navigate(self.A + "/js-submit")
        self.driver.js("fetch('/apply-js',{method:'POST',body:'held'}).catch(()=>0)", settle=0.2)
        adir = self.tmp / approval.APPROVAL_DIR
        for _ in range(100):
            if adir.exists() and list(adir.glob("req-*.json")):
                break
            time.sleep(0.05)
        self.assertTrue(list(adir.glob("req-*.json")), "the POST was not held for approval")
        asyncio.run_coroutine_threadsafe(self._drop(), self.guard.loop).result(5)
        time.sleep(2.5)  # Chrome releases its held requests when the controller disappears
        self.assertTrue(self.guard.failed.is_set())
        self.assertEqual(self.app_hits.count("POST", "/apply-js"), 0)
        self.assertTrue(self.guard.proxy.revoked)

    def test_released_write_is_recorded_as_evidence(self):
        self.launch(dry_run=False, approval_timeout_s=10)
        self.driver.navigate(self.A + "/js-submit")

        def approve():
            adir = self.tmp / approval.APPROVAL_DIR
            for _ in range(100):
                reqs = list(adir.glob("req-*.json")) if adir.exists() else []
                if reqs:
                    approval.grant(self.tmp, json.loads(reqs[0].read_text())["nonce"])
                    return
                time.sleep(0.05)
        t = threading.Thread(target=approve)
        t.start()
        self.driver.js("fetch('/apply-js',{method:'POST',body:'one'}).catch(()=>0)", settle=2.0)
        t.join()
        ev = launcher.submission_evidence(self.tmp)
        self.assertEqual((ev["released"], ev["ambiguous"]), (1, 0))
        self.assertEqual(self.app_hits.count("POST", "/apply-js"), 1)

    def test_lost_controller_connection_fails_closed(self):

        self.launch()
        killed = threading.Event()
        self.guard.on_failure = killed.set
        asyncio.run_coroutine_threadsafe(self._drop(), self.guard.loop).result(5)
        self.assertTrue(killed.wait(5))
        self.assertIsNotNone(P.network_guard_problem(self.policy))
        self.assertTrue(P.session_halted(self.policy))

    async def _drop(self):
        controller = self.guard.controller
        assert controller is not None and controller.conn._writer is not None
        controller.conn._writer.close()


if __name__ == "__main__":
    unittest.main()
