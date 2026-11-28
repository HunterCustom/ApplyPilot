"""Regression tests for the Phase 3b audit fixes (CODEX_PHASE3B_AUDIT.md H1-H4, M1-M6, L3).

Everything is local and synthetic: loopback sockets, temp session directories, and (for the
process jail) a child ``cmd.exe`` that only runs ``ping 127.0.0.1``. No browser is launched here
and no OS protocol handler is ever invoked.
"""
import asyncio
import atexit
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from applypilot.apply import approval, chrome, launcher, policy_hook, proc_jail
from applypilot.apply import network_policy as NP
from applypilot.apply import policy as P
from applypilot.apply.egress_proxy import EgressProxy
from applypilot.apply.network_guard import request_body

atexit.unregister(chrome.cleanup_on_exit)

JOB_URL = "https://careers.acme-corp.com/jobs/42"


def make_policy(tmp, dry_run=True, **kw):
    kw.setdefault("allowed_hosts", ["*.acme-corp.com"])
    kw.setdefault("asset_hosts", list(NP.STATIC_ASSET_HOSTS))
    pol = P.SessionPolicy(session_dir=str(tmp), dry_run=dry_run, upload_paths=[], job_url=JOB_URL, **kw)
    pol.write()
    return pol


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


# --- H1: proxy kill switch and write gate, observed at the receiver -----------------------------

class CountingServer:
    """Raw TCP sink: counts every byte the proxy forwards to it."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.received = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._read, args=(c,), daemon=True).start()

    def _read(self, c):
        with c:
            while True:
                try:
                    data = c.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                with self.lock:
                    self.received += len(data)

    def close(self):
        self.sock.close()


class ProxyGateTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.sink = CountingServer()
        self.addCleanup(self.sink.close)
        self.pol = make_policy(self.tmp, trusted_hosts=[f"127.0.0.1:{self.sink.port}"])
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.proxy = EgressProxy(get_policy=lambda: self.pol)

    async def _tunnel(self):
        port = await self.proxy.start()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"CONNECT 127.0.0.1:{self.sink.port} HTTP/1.1\r\n\r\n".encode())
        await w.drain()
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
        self.assertIn(b"200", head)
        return r, w

    def test_held_gate_stops_bytes_and_revoke_drops_them(self):
        async def go():
            r, w = await self._tunnel()
            self.proxy.hold()
            w.write(b"POST /apply HTTP/1.1\r\nContent-Length: 4\r\n\r\ndata")
            await w.drain()
            await asyncio.sleep(0.5)
            held = self.sink.received
            self.proxy.revoke()
            await asyncio.sleep(0.5)
            data = await asyncio.wait_for(r.read(), 5)  # the tunnel was cut
            w.close()
            await self.proxy.stop()
            return held, data
        held, data = self.loop.run_until_complete(go())
        self.assertEqual(held, 0)
        self.assertEqual(self.sink.received, 0)
        self.assertEqual(data, b"")

    def test_released_gate_forwards(self):
        async def go():
            _r, w = await self._tunnel()
            self.proxy.hold()
            w.write(b"abcd")
            await w.drain()
            await asyncio.sleep(0.3)
            self.proxy.release()
            await asyncio.sleep(0.5)
            w.close()
            await self.proxy.stop()
        self.loop.run_until_complete(go())
        self.assertEqual(self.sink.received, 4)

    def test_revoked_proxy_refuses_new_connections(self):
        async def go():
            port = await self.proxy.start()
            self.proxy.revoke()
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(f"CONNECT 127.0.0.1:{self.sink.port} HTTP/1.1\r\n\r\n".encode())
            try:
                await w.drain()
                data = await asyncio.wait_for(r.read(), 5)
            except (ConnectionError, OSError):
                data = b""
            w.close()
            await self.proxy.stop()
            return data
        self.assertNotIn(b"200", self.loop.run_until_complete(go()))
        self.assertEqual(self.sink.received, 0)


# --- M1: atomic approvals -----------------------------------------------------------------------

class AtomicApprovalTests(TempDirCase):
    ACTION = types.MappingProxyType({"method": "POST", "url": JOB_URL, "body_sha256": "ab", "request_id": "r1"})

    def req(self):
        return approval.create_request(self.tmp, kind="network", job_url=JOB_URL, action=dict(self.ACTION), ttl_s=60)

    def test_concurrent_consumers_exactly_one_wins(self):
        for _ in range(10):
            req = self.req()
            self.assertTrue(approval.grant(self.tmp, req["nonce"]))
            results, barrier = [], threading.Barrier(4)

            def consume(r=req, barrier=barrier, results=results):
                barrier.wait()
                results.append(approval.consume(self.tmp, r["nonce"], job_url=JOB_URL, digest=r["digest"]))
            ts = [threading.Thread(target=consume) for _ in range(4)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(sorted(results), [False, False, False, True])

    def test_withdrawn_request_can_never_be_granted_or_consumed(self):
        req = self.req()
        approval.withdraw(self.tmp, req["nonce"])
        self.assertFalse(approval.grant(self.tmp, req["nonce"]))
        req2 = self.req()
        self.assertTrue(approval.grant(self.tmp, req2["nonce"]))
        approval.withdraw(self.tmp, req2["nonce"])
        self.assertFalse(approval.consume(self.tmp, req2["nonce"], job_url=JOB_URL, digest=req2["digest"]))

    def test_double_grant_refused(self):
        req = self.req()
        self.assertTrue(approval.grant(self.tmp, req["nonce"]))
        self.assertFalse(approval.grant(self.tmp, req["nonce"]))

    def test_cancel_all_closes_open_requests(self):
        reqs = [self.req() for _ in range(3)]
        approval.grant(self.tmp, reqs[0]["nonce"])
        self.assertEqual(approval.cancel_all(self.tmp), 3)
        for r in reqs:
            self.assertFalse(approval.consume(self.tmp, r["nonce"], job_url=JOB_URL, digest=r["digest"]))
        self.assertEqual(list((self.tmp / approval.APPROVAL_DIR).glob("*.json")), [])


class RequestBodyTests(unittest.TestCase):
    def test_complete_and_incomplete_bodies(self):
        b = base64.b64encode(b"name=x").decode()
        self.assertEqual(request_body({"postDataEntries": [{"bytes": b}]}), (b"name=x", True))
        self.assertEqual(request_body({"postDataEntries": [{"bytes": b}, {}]})[1], False)  # file part missing
        self.assertEqual(request_body({"hasPostData": True})[1], False)
        self.assertEqual(request_body({"postData": "a=1"}), (b"a=1", True))
        self.assertEqual(request_body({}), (b"", True))


# --- H2: request decisions -------------------------------------------------------------------------

class RequestRuleTests(TempDirCase):
    def d(self, url, method="GET", rtype="XHR", main=False, dry_run=True, **kw):
        return NP.decide_request(make_policy(self.tmp, dry_run=dry_run), url=url, method=method,
                                 resource_type=rtype, is_main_frame=main, **kw)

    def test_data_bearing_get_is_state_changing(self):
        tokens = ["testy.applicant@example.org", "9195550142"]
        dec = self.d("https://careers.acme-corp.com/api?e=testy.applicant%40example.org", sensitive_tokens=tokens)
        self.assertEqual((dec.action, dec.boundary), ("block", True))
        dec = self.d("https://careers.acme-corp.com/api?p=919-555-0142", sensitive_tokens=tokens, dry_run=False)
        self.assertEqual(dec.action, "approve")
        self.assertEqual(self.d("https://careers.acme-corp.com/api?q=it", sensitive_tokens=tokens).action, "allow")

    def test_bodies_and_beacons_are_writes(self):
        self.assertTrue(self.d("https://careers.acme-corp.com/x", has_body=True).boundary)
        self.assertTrue(self.d("https://careers.acme-corp.com/x", rtype="Ping").boundary)
        self.assertTrue(self.d("https://careers.acme-corp.com/x", method="OPTIONS").boundary)

    def test_asset_exception_is_bounded(self):
        ok = self.d("https://fonts.gstatic.com/a.woff2", rtype="Font")
        self.assertEqual(ok.action, "allow")
        long_q = self.d("https://fonts.gstatic.com/a.woff2?" + "x" * 400, rtype="Font")
        self.assertEqual(long_q.action, "block")
        leak = self.d("https://fonts.gstatic.com/a.png?e=testy.applicant@example.org", rtype="Image",
                      sensitive_tokens=["testy.applicant@example.org"])
        self.assertEqual(leak.action, "block")

    def test_other_tenant_page_blocked(self):
        pol = make_policy(self.tmp, allowed_hosts=["*.myworkdayjobs.com"], tenant_hosts=["acme.wd5.myworkdayjobs.com"])
        own = NP.decide_request(pol, url="https://acme.wd5.myworkdayjobs.com/job", method="GET",
                                resource_type="Document", is_main_frame=True)
        other = NP.decide_request(pol, url="https://rival.wd1.myworkdayjobs.com/job", method="GET",
                                  resource_type="Document", is_main_frame=True)
        self.assertEqual(own.action, "allow")
        self.assertEqual((other.action, other.terminal), ("block", True))


# --- M3: tool surface / audit -----------------------------------------------------------------------

class ToolSurfaceTests(TempDirCase):
    PW = P.PLAYWRIGHT_PREFIX

    def test_screenshot_and_file_output_args_denied(self):
        pol = make_policy(self.tmp)
        (self.tmp / P.GUARD_STATUS_FILE).write_text(json.dumps({"active": True, "selftest": "ok"}))
        self.assertNotIn(self.PW + "browser_take_screenshot", P.allowed_tool_names(False))
        allow, why, terminal = policy_hook.decide_pre(pol, self.PW + "browser_snapshot", {"filename": "x.md"})
        self.assertFalse(allow)
        self.assertTrue(terminal)
        self.assertIn("filename", why)
        allow, _, _ = policy_hook.decide_pre(pol, self.PW + "browser_snapshot", {})
        self.assertTrue(allow)

    def test_audit_matches_tool_use_ids(self):
        pol = make_policy(self.tmp)
        (self.tmp / "hook_alive").write_text("1")
        (self.tmp / P.GUARD_STATUS_FILE).write_text(json.dumps({"active": True, "selftest": "ok"}))
        P.record_audit(pol, {"phase": "pre", "tool": "x", "decision": "allow", "id": "a"})
        self.assertIsNone(launcher.audit_session(self.tmp, 1, {"a"}))
        # one decision for a different call does not cover this one
        self.assertEqual(launcher.audit_session(self.tmp, 1, {"b"}), "uncertain:policy_hook_not_enforced")

    def test_agent_env_drops_playwright_overrides(self):
        env = {"PATH": "x", "PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS": "1", "PLAYWRIGHT_MCP_OUTPUT_DIR": "C:/",
               "NODE_OPTIONS": "--require x", "ANTHROPIC_API_KEY": "k"}
        with mock.patch.dict(os.environ, env, clear=True):
            out = launcher.agent_env()
        self.assertEqual(sorted(out), ["ANTHROPIC_API_KEY", "PATH"])

    def test_enter_and_type_submit_count_as_submit_actions(self):
        self.assertTrue(launcher.is_submit_action(self.PW + "browser_press_key", {"key": "Enter"}))
        self.assertTrue(launcher.is_submit_action(self.PW + "browser_type", {"text": "x", "submit": True}))

    def test_sensitive_tokens_from_profile(self):
        toks = launcher.sensitive_url_tokens({"personal": {"email": "a.b@example.org", "phone": "(919) 555-0142",
                                                           "full_name": "Testy Applicant"}})
        self.assertIn("9195550142", toks)
        self.assertIn("a.b@example.org", toks)


# --- M4 / M5 / L1 ---------------------------------------------------------------------------------------

class PersistenceAndProtocolTests(TempDirCase):
    def test_safe_url_drops_credentials_queries_and_tokens(self):
        from applypilot.apply.redact import safe_url
        url = ("https://user:pw@careers.acme-corp.com:8443/apply/eyJhbGciOiJIUzI1NiJ9abc123def456/"
               "testy@example.org/step?email=testy@example.org#frag")
        out = safe_url(url)
        self.assertEqual(out, "https://careers.acme-corp.com:8443/apply/[token]/[token]/step")

    def test_approval_file_holds_no_raw_values(self):
        action = {"tool_action": "typing with submit=true", "input": {"text": "Sup3r-S3cret!pw", "element": "Pw"},
                  "url": "https://careers.acme-corp.com/x?token=abc"}
        req = approval.create_request(self.tmp, kind="click", job_url=JOB_URL + "?ref=me@example.org",
                                      action=action, ttl_s=60)
        raw = (self.tmp / approval.APPROVAL_DIR / f"req-{req['nonce']}.json").read_text()
        for secret in ("Sup3r-S3cret!pw", "token=abc", "me@example.org"):
            self.assertNotIn(secret, raw)
        self.assertEqual(req["digest"], approval.action_digest(action))

    def test_cdp_rejects_malformed_frames(self):
        from applypilot.apply.cdp import CDPConnection, CDPError

        async def frame(data: bytes):
            conn = CDPConnection()
            conn._reader = asyncio.StreamReader()
            conn._reader.feed_data(data)
            conn._reader.feed_eof()
            return await conn._read_frame()
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        self.assertEqual(loop.run_until_complete(frame(b"\x81\x02hi")), (True, 1, b"hi"))
        for bad in (b"\x81\x82abcdhi", b"\xc1\x02hi", b"\x83\x02hi", b"\x09\x02hi", b"\x89\x7e\x00\x80" + b"x" * 128):
            with self.subTest(bad=bad[:2]), self.assertRaises(CDPError):
                loop.run_until_complete(frame(bad))

    def test_verification_helper_needs_explicit_opt_in(self):
        with mock.patch.object(launcher.verification_mcp, "credentials_path",
                               return_value=mock.Mock(exists=lambda: True)):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("APPLYPILOT_ENABLE_VERIFY_HELPER", None)
                self.assertFalse(launcher.verification_enabled())
            with mock.patch.dict(os.environ, {"APPLYPILOT_ENABLE_VERIFY_HELPER": "1"}):
                os.environ.pop("APPLYPILOT_GMAIL_CREDENTIALS", None)
                self.assertTrue(launcher.verification_enabled())
            with mock.patch.dict(os.environ, {"APPLYPILOT_ENABLE_VERIFY_HELPER": "1",
                                              "APPLYPILOT_GMAIL_CREDENTIALS": "x"}):
                self.assertFalse(launcher.verification_enabled())


# --- M6: explicit targets are exact ------------------------------------------------------------------

class TargetSelectionTests(TempDirCase):
    def test_fuzzy_target_never_matches(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT, site TEXT, application_url TEXT,
            tailored_resume_path TEXT, fit_score INT, location TEXT, full_description TEXT, cover_letter_path TEXT,
            eligibility_status TEXT, eligibility_reason TEXT, eligibility_version TEXT, apply_status TEXT,
            apply_attempts INT, eligibility_tier TEXT)""")
        from applypilot import eligibility
        conn.execute("INSERT INTO jobs (url, title, application_url, tailored_resume_path, eligibility_status,"
                     " eligibility_version) VALUES (?,?,?,?,?,?)",
                     ("https://ex.com/jobs/12345", "t", None, "r.pdf", min(eligibility.ELIGIBLE_STATUSES),
                      eligibility.CLASSIFIER_VERSION))
        self.assertIsNotNone(launcher._select_target(conn, "https://ex.com/jobs/12345"))
        self.assertIsNone(launcher._select_target(conn, "https://ex.com/jobs/1234"))
        self.assertIsNone(launcher._select_target(conn, "jobs/12345"))


# --- H4: process jail ----------------------------------------------------------------------------------

@unittest.skipUnless(proc_jail.jail_supported(), "process jail is Windows-only")
class ProcessJailTests(unittest.TestCase):
    def test_child_program_terminated_and_reported(self):
        escapes = []
        allowed = [sys.executable, getattr(sys, "_base_executable", sys.executable)]
        jail = proc_jail.ProcessJail(allowed, escapes.append)
        self.addCleanup(jail.close)
        # The jail usually terminates the child while CreateProcess is still running, so the parent
        # sees an OSError (the program never executed); otherwise the child dies with a non-zero code.
        code = ("import subprocess,time;t=time.time()\n"
                "try:\n rc=subprocess.run(['cmd.exe','/c','ping','-n','6','127.0.0.1'],capture_output=True).returncode\n"
                "except OSError:\n rc='oserror'\n"
                "print(rc, round(time.time()-t,1))")
        t0 = time.time()
        proc = jail.spawn([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.assertTrue(jail.contains(proc.pid))
        out, _ = proc.communicate(timeout=30)
        rc, elapsed = out.split()
        self.assertNotEqual(rc, "0")
        self.assertLess(float(elapsed), 4.0)  # killed long before ping's ~5 s
        self.assertLess(time.time() - t0, 20)
        self.assertTrue(any("cmd.exe" in e.lower() for e in escapes), escapes)

    def test_close_terminates_job(self):
        jail = proc_jail.ProcessJail([sys.executable, getattr(sys, "_base_executable", sys.executable)],
                                     lambda why: None)
        proc = jail.spawn([sys.executable, "-c", "import time; time.sleep(60)"])
        jail.close()
        self.assertIsNotNone(proc.wait(timeout=10))


if __name__ == "__main__":
    unittest.main()
