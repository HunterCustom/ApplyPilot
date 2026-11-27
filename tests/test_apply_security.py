"""Security boundary tests for the apply agent (S1-S4, Task E).

All synthetic: temporary directories and databases, a fake ``claude`` process,
a fake Gmail backend and synthetic malicious URLs / tool events. No browser,
LLM, network, real employer site or user data is touched.
"""
import atexit
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from applypilot import config
from applypilot.apply import approval, chrome, launcher, policy_hook
from applypilot.apply import policy as P
from applypilot.apply import prompt as prompt_mod
from applypilot.apply import verification_mcp as V
from applypilot.apply.redact import Redactor

atexit.unregister(chrome.cleanup_on_exit)

PW = "mcp__playwright__"
JOB = {"url": "https://www.indeed.com/viewjob?jk=1", "application_url": "https://careers.acme-corp.com/jobs/42",
       "title": "IT Support Specialist", "site": "Acme"}
PROFILE = {
    "personal": {"full_name": "Testy McTestface", "preferred_name": "Testy", "email": "testy.applicant@example.org",
                 "phone": "(919) 555-0142", "city": "Raleigh", "province_state": "NC", "country": "US",
                 "postal_code": "27601", "address": "12 Synthetic Lane", "password": "Sup3r-S3cret!pw",
                 "linkedin_url": "https://linkedin.com/in/testy-synthetic"},
    "work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": False},
    "compensation": {"salary_expectation": "60000"},
    "eeo_voluntary": {"gender": "Decline to answer"},
}


def mark_guard_verified(session_dir):
    """What NetworkGuard.activate writes after its self-test (see test_network_boundary)."""
    (Path(session_dir) / P.GUARD_STATUS_FILE).write_text(json.dumps({"active": True, "selftest": "ok"}))


def make_policy(tmp, dry_run=True, guard_verified=True, **kw):
    kw.setdefault("job_url", JOB["url"])
    pol = P.SessionPolicy(session_dir=str(tmp), dry_run=dry_run, allowed_hosts=P.session_allowed_hosts(JOB),
                          upload_paths=[str(Path(tmp) / "uploads" / "Testy_Resume.pdf")], **kw)
    pol.write()
    if guard_verified:
        mark_guard_verified(tmp)
    return pol


class FakeGuard:
    """Stands in for NetworkGuard in run_job tests (the real one is tested with Chrome)."""

    def __init__(self, fail_activate=None):
        self.failed = threading.Event()
        self.failure_reason = ""
        self.on_failure = self.on_approval_request = None
        self.fail_activate = fail_activate
        self.activated = False

    def activate(self, policy, port):
        if self.fail_activate:
            raise launcher.NetworkGuardError(self.fail_activate)
        self.activated = True
        mark_guard_verified(policy.dir)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


# --- S1: tool surface -----------------------------------------------------------------

class AgentCommandTests(unittest.TestCase):
    def cmd(self, verify=False):
        return launcher.build_agent_command("sonnet", Path("m.json"), True, Path("s.json"), verify)

    def arg(self, cmd, flag):
        return cmd[cmd.index(flag) + 1]

    def test_no_builtin_tools_no_user_settings_dont_ask(self):
        cmd = self.cmd()
        self.assertEqual(self.arg(cmd, "--tools"), "")
        self.assertEqual(self.arg(cmd, "--setting-sources"), "")
        self.assertEqual(self.arg(cmd, "--permission-mode"), "dontAsk")
        self.assertNotIn("bypassPermissions", cmd)
        for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
            self.assertIn(flag, cmd)

    def test_allowed_tools_are_exactly_the_browser_allowlist(self):
        allowed = set(self.arg(self.cmd(), "--allowedTools").split(","))
        self.assertEqual(allowed, {PW + t for t in P.ALLOWED_BROWSER_TOOLS})
        self.assertIn(P.VERIFY_TOOL, self.arg(self.cmd(verify=True), "--allowedTools").split(","))
        denied = set(self.arg(self.cmd(), "--disallowedTools").split(","))
        for t in ("Bash", "Read", "Write", "WebFetch", PW + "browser_evaluate", PW + "browser_run_code_unsafe"):
            self.assertIn(t, denied)
            self.assertNotIn(t, allowed)

    def test_mcp_servers_pinned_and_no_gmail(self):
        cfg = launcher._make_mcp_config(9300)
        self.assertEqual(set(cfg["mcpServers"]), {"playwright"})
        args = cfg["mcpServers"]["playwright"]["args"]
        self.assertIn(launcher.PLAYWRIGHT_MCP_PACKAGE, args)
        self.assertNotIn("@latest", " ".join(args))
        self.assertRegex(launcher.PLAYWRIGHT_MCP_PACKAGE, r"@\d+\.\d+\.\d+$")
        cfg = launcher._make_mcp_config(9300, Path("p.json"), verify_enabled=True)
        self.assertEqual(set(cfg["mcpServers"]), {"playwright", "applypilot_verify"})

    def test_cli_capability_check_fails_closed(self):
        full = ("--tools --setting-sources --strict-mcp-config --allowedTools --disallowedTools "
                "--settings dontAsk --disable-slash-commands")
        self.assertIsNone(launcher.check_agent_cli(full))
        self.assertIn("--tools", launcher.check_agent_cli(full.replace("--tools", "")))
        self.assertIsNotNone(launcher.check_agent_cli(""))

    def test_policy_settings_hook_every_tool(self):
        hooks = launcher.policy_hook_settings(Path("C:/x/policy.json"))["hooks"]
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], ".*")
        self.assertIn("policy_hook pre", hooks["PreToolUse"][0]["hooks"][0]["command"])
        self.assertIn("policy_hook post", hooks["PostToolUse"][0]["hooks"][0]["command"])
        self.assertIn("policy_hook start", hooks["SessionStart"][0]["hooks"][0]["command"])


class InitEventTests(unittest.TestCase):
    GOOD = types.MappingProxyType({"type": "system", "subtype": "init", "permissionMode": "dontAsk",
            "tools": [PW + "browser_click", PW + "browser_evaluate"],
            "mcp_servers": [{"name": "playwright", "status": "connected"}],
            "plugins": [{"name": "cc-plugin-privacy", "source": "cc-plugin-privacy@builtin"}], "skills": []})

    def check(self, **changes):
        return launcher.verify_init_event({**self.GOOD, **changes}, verify_enabled=False)

    def test_good_surface_passes(self):
        self.assertIsNone(self.check())

    def test_deviations_fail(self):
        self.assertIn("Bash", self.check(tools=self.GOOD["tools"] + ["Bash"]))
        self.assertIsNotNone(self.check(permissionMode="bypassPermissions"))
        self.assertIsNotNone(self.check(mcp_servers=self.GOOD["mcp_servers"] + [{"name": "gmail", "status": "connected"}]))
        self.assertIsNotNone(self.check(mcp_servers=[{"name": "playwright", "status": "failed"}]))
        self.assertIsNotNone(self.check(plugins=[{"name": "context-mode", "source": "context-mode@marketplace"}]))
        self.assertIsNotNone(self.check(skills=["deploy"]))
        self.assertIsNotNone(launcher.verify_init_event(dict(self.GOOD), verify_enabled=True))  # helper missing


# --- S2: navigation / upload / value policy ----------------------------------------------

class NavigationPolicyTests(unittest.TestCase):
    HOSTS = tuple(P.session_allowed_hosts(JOB))

    def test_allowed(self):
        for url in ("https://careers.acme-corp.com/jobs/42", "https://apply.acme-corp.com/x",
                    "https://acme.wd5.myworkdayjobs.com/en-US/External/job/1",
                    "https://job-boards.greenhouse.io/acme/jobs/1", "https://www.indeed.com/viewjob?jk=1",
                    "about:blank"):
            self.assertIsNone(P.url_block_reason(url, self.HOSTS), url)

    def test_malicious_and_local_targets_blocked(self):
        for url in (
            "http://localhost:9222/json", "http://127.0.0.1/", "http://127.1/", "http://2130706433/",
            "http://0x7f000001/", "http://[::1]/", "http://10.0.0.5/admin", "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/", "http://router.local/", "http://intranet/",
            "file:///C:/Users/x/profile.json", "chrome://settings", "javascript:alert(1)",
            "data:text/html,<script>1</script>", "view-source:https://acme-corp.com",
            "https://careers.acme-corp.com@evil.example/", "https://user:pw@careers.acme-corp.com/",
            "https://acme-corp.com.evil.example/", "https://evilgreenhouse.io/", "https://greenhouse.io.evil.example/",
            "https://evil.example/apply", "https://www.linkedin.com.evil.example/", "https://mail.google.com/",
            "https://careers.acme-corp.com:8443/", "https://linkedin.com/feed",
        ):
            self.assertIsNotNone(P.url_block_reason(url, self.HOSTS), url)

    def test_job_board_domain_is_exact_host_only(self):
        self.assertIn("www.indeed.com", self.HOSTS)
        self.assertNotIn("*.indeed.com", self.HOSTS)
        self.assertIn("*.acme-corp.com", self.HOSTS)

    def test_trusted_local_infra_only_when_explicit(self):
        self.assertIsNone(P.url_block_reason("http://127.0.0.1:8765/x", [], ["127.0.0.1:8765"]))
        self.assertIsNotNone(P.url_block_reason("http://127.0.0.1:8766/x", [], ["127.0.0.1:8765"]))

    def test_sensitive_values(self):
        self.assertIsNotNone(P.sensitive_value_reason("123-45-6789", []))
        self.assertIsNotNone(P.sensitive_value_reason("4111 1111 1111 1111", []))
        self.assertIsNone(P.sensitive_value_reason("(919) 555-0142", []))
        self.assertIsNone(P.sensitive_value_reason("123-45-6789", ["123-45-6789"]))


class PolicyHookTests(TempDirCase):
    def pre(self, pol, tool, inp):
        return policy_hook.decide_pre(pol, tool, inp)

    def test_disallowed_tools_denied_terminally(self):
        pol = make_policy(self.tmp)
        for tool in ("Bash", "Read", PW + "browser_evaluate", PW + "browser_run_code_unsafe", "mcp__gmail__search_emails"):
            allow, _, terminal = self.pre(pol, tool, {})
            self.assertFalse(allow, tool)
            self.assertTrue(terminal, tool)

    def test_navigation_upload_and_values(self):
        pol = make_policy(self.tmp)
        self.assertTrue(self.pre(pol, PW + "browser_navigate", {"url": JOB["application_url"]})[0])
        self.assertFalse(self.pre(pol, PW + "browser_navigate", {"url": "http://169.254.169.254/"})[0])
        self.assertFalse(self.pre(pol, PW + "browser_tabs", {"action": "new", "url": "https://evil.example"})[0])
        self.assertTrue(self.pre(pol, PW + "browser_file_upload", {"paths": [pol.upload_paths[0]]})[0])
        self.assertFalse(self.pre(pol, PW + "browser_file_upload", {"paths": [str(Path.home() / ".ssh" / "id_rsa")]})[0])
        self.assertFalse(self.pre(pol, PW + "browser_fill_form", {"fields": [{"name": "SSN", "value": "123-45-6789"}]})[0])

    def test_dry_run_guard_still_applies(self):
        pol = make_policy(self.tmp)
        allow, reason, _ = self.pre(pol, PW + "browser_click", {"element": "Submit application"})
        self.assertFalse(allow)
        self.assertTrue(reason.startswith(policy_hook.GUARD_PREFIX))

    def test_violation_halts_the_whole_session(self):
        pol = make_policy(self.tmp)
        event = {"tool_name": PW + "browser_navigate", "tool_input": {"url": "https://evil.example/"}}
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(policy_hook.handle_pre(pol, event), 2)
        self.assertTrue(P.session_halted(pol))
        allow, reason, _ = self.pre(pol, PW + "browser_snapshot", {})
        self.assertFalse(allow)
        self.assertIn("halted", reason)

    def test_post_hook_detects_redirect_to_disallowed_origin(self):
        pol = make_policy(self.tmp)
        resp = [{"type": "text", "text": "### Page state\n- Page URL: http://127.0.0.1:8080/steal?x=1\n- Page Title: x"}]
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            policy_hook.handle_post(pol, {"tool_name": PW + "browser_click", "tool_response": resp})
        self.assertEqual(json.loads(out.getvalue())["decision"], "block")
        self.assertTrue(P.session_halted(pol))

    def test_post_hook_allows_approved_page(self):
        pol = make_policy(self.tmp)
        resp = "- Page URL: https://acme.wd5.myworkdayjobs.com/External/apply"
        with mock.patch("sys.stdout", io.StringIO()):
            policy_hook.handle_post(pol, {"tool_name": PW + "browser_click", "tool_response": resp})
        self.assertFalse(P.session_halted(pol))

    def test_hook_entry_point_fails_closed(self):
        pol = make_policy(self.tmp)
        policy_path = str(Path(pol.session_dir) / "policy.json")
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(policy_hook.main(["pre"]), 2)  # missing policy path
            self.assertEqual(policy_hook.main(["pre", str(self.tmp / "missing.json")]), 2)
            with mock.patch("sys.stdin", io.StringIO("{not json")):
                self.assertEqual(policy_hook.main(["pre", policy_path]), 2)

    def test_hook_runs_as_subprocess_and_audits(self):
        pol = make_policy(self.tmp)
        policy_path = str(Path(pol.session_dir) / "policy.json")

        def run(phase, event):
            return subprocess.run([sys.executable, "-m", "applypilot.apply.policy_hook", phase, policy_path],
                                  input=json.dumps(event), capture_output=True, text=True, timeout=60, check=False)

        self.assertEqual(run("start", {}).returncode, 0)
        self.assertTrue((self.tmp / "hook_alive").exists())
        ok = run("pre", {"tool_name": PW + "browser_snapshot", "tool_input": {}})
        self.assertEqual(ok.returncode, 0)
        bad = run("pre", {"tool_name": "Bash", "tool_input": {"command": "whoami"}})
        self.assertEqual(bad.returncode, 2)
        self.assertIn("APPLYPILOT POLICY", bad.stderr)
        audit = P.read_jsonl(self.tmp / "audit.jsonl")
        self.assertEqual([r["decision"] for r in audit if r["phase"] == "pre"], ["allow", "deny"])
        self.assertNotIn("whoami", (self.tmp / "audit.jsonl").read_text())


class SubmitApprovalTests(TempDirCase):
    def test_real_run_submit_denied_without_human_grant(self):
        pol = make_policy(self.tmp, dry_run=False, approval_timeout_s=1)
        event = {"tool_name": PW + "browser_click", "tool_input": {"element": "Submit application"}}
        with mock.patch("sys.stderr", io.StringIO()), mock.patch.object(policy_hook.time, "sleep", lambda s: None):
            self.assertEqual(policy_hook.handle_pre(pol, event), 2)
        self.assertEqual(launcher.submission_evidence(self.tmp), (False, False))

    def test_real_run_submit_allowed_after_out_of_band_grant(self):
        pol = make_policy(self.tmp, dry_run=False, approval_timeout_s=20)
        event = {"tool_name": PW + "browser_click", "tool_input": {"element": "Submit application"}}

        def approve():
            for _ in range(200):
                reqs = list((self.tmp / approval.APPROVAL_DIR).glob("req-*.json"))
                if reqs:
                    req = json.loads(reqs[0].read_text())
                    self.assertEqual((req["kind"], req["job_url"]), ("click", JOB["url"]))
                    self.assertFalse(P.approve_submission(self.tmp, "0" * 32))
                    self.assertTrue(P.approve_submission(self.tmp, req["nonce"]))
                    return
                time.sleep(0.05)

        t = threading.Thread(target=approve)
        t.start()
        with mock.patch("sys.stderr", io.StringIO()):
            rc = policy_hook.handle_pre(pol, event)
        t.join()
        self.assertEqual(rc, 0)
        with mock.patch("sys.stdout", io.StringIO()):
            policy_hook.handle_post(pol, {"tool_name": PW + "browser_click",
                                          "tool_response": "Thank you for applying! - Page URL: https://careers.acme-corp.com/done"})
        # the click approval alone is not evidence: the network guard must also have released the request
        self.assertEqual(launcher.submission_evidence(self.tmp), (False, True))
        P.record_audit(pol, {"phase": "network", "decision": "granted", "net_approved": True})
        self.assertEqual(launcher.submission_evidence(self.tmp), (True, True))


class CliSubmissionGateTests(TempDirCase):
    def invoke(self, args):
        from typer.testing import CliRunner

        from applypilot import cli
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = [1]
        profile = self.tmp / "profile.json"
        profile.write_text("{}")
        with mock.patch.object(cli, "_bootstrap"), mock.patch.object(config, "check_tier"), \
                mock.patch.object(config, "PROFILE_PATH", profile), \
                mock.patch("applypilot.database.get_connection", return_value=conn), \
                mock.patch.object(launcher, "main") as main:
            result = CliRunner().invoke(cli.app, ["apply", *args])
        return result, main

    def test_real_run_refused_without_explicit_flag(self):
        result, main = self.invoke([])
        self.assertEqual(result.exit_code, 1)
        main.assert_not_called()

    def test_dry_run_and_flagged_real_run_reach_launcher(self):
        self.assertTrue(self.invoke(["--dry-run"])[1].called)
        self.assertTrue(self.invoke(["--allow-real-submissions"])[1].called)

    def test_approve_requires_matching_nonce(self):
        req = approval.create_request(self.tmp, kind="network", job_url=JOB["url"],
                                      action={"method": "POST"}, ttl_s=60)
        self.assertEqual(self.invoke(["--approve", str(self.tmp), "--nonce", "f" * 32])[0].exit_code, 1)
        result = self.invoke(["--approve", str(self.tmp), "--nonce", req["nonce"]])[0]
        self.assertEqual(result.exit_code, 0)
        self.assertIn("POST", result.output)  # the human sees the exact action being approved
        self.assertTrue((self.tmp / approval.APPROVAL_DIR / f"grant-{req['nonce']}.json").exists())
        # single use: a second grant of the same nonce is refused
        self.assertEqual(self.invoke(["--approve", str(self.tmp), "--nonce", req["nonce"]])[0].exit_code, 1)


# --- Classification / lifecycle (Task A + E) -----------------------------------------

class ClassificationTests(TempDirCase):
    def classify(self, text, **kw):
        base = {"final_is_error": False, "dry_run": False, "submit_attempts": 0, "unblocked_submits": 0}
        return launcher.classify_session(text, **{**base, **kw})

    def test_applied_requires_independent_evidence(self):
        self.assertEqual(self.classify("RESULT:APPLIED"), "uncertain:applied_without_evidence")
        self.assertEqual(self.classify("RESULT:APPLIED", submit_attempts=1), "uncertain:applied_without_evidence")
        self.assertEqual(self.classify("RESULT:APPLIED", submit_attempts=1, submit_approved=True),
                         "uncertain:applied_without_evidence")
        self.assertEqual(self.classify("RESULT:APPLIED", submit_attempts=1, submit_approved=True,
                                       confirmation_seen=True), "applied")

    def test_security_and_audit_results_override_agent_claims(self):
        self.assertEqual(self.classify("RESULT:DRY_RUN", dry_run=True, security_failure="x"),
                         "failed:security_config_unverified")
        self.assertEqual(self.classify("RESULT:DRY_RUN", dry_run=True, security_failure="x", tool_uses=1),
                         "uncertain:security_config_unverified")
        self.assertEqual(self.classify("RESULT:APPLIED", audit_result="uncertain:policy_violation"),
                         "uncertain:policy_violation")

    def test_audit_session(self):
        self.assertEqual(launcher.audit_session(self.tmp, 1), "uncertain:policy_hook_not_active")
        (self.tmp / "hook_alive").write_text("1")
        self.assertEqual(launcher.audit_session(self.tmp, 1), "uncertain:policy_hook_not_enforced")
        pol = make_policy(self.tmp)
        P.record_audit(pol, {"phase": "pre", "tool": "x", "decision": "allow"})
        self.assertIsNone(launcher.audit_session(self.tmp, 1))
        P.record_violation(pol, "x", "blocked", terminal=False)
        self.assertEqual(launcher.audit_session(self.tmp, 1), "uncertain:policy_violation")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        from applypilot import database
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Path(self._tmp.name) / "t.db"
        p = mock.patch.object(database, "DB_PATH", db)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(database.close_connection, db)
        database.init_db(db)
        self.conn = database.get_connection()
        self.conn.execute("INSERT INTO jobs (url, title, apply_status, agent_id) VALUES ('u', 't', 'in_progress', 'me')")
        self.conn.commit()

    def status(self):
        return self.conn.execute("SELECT apply_status, apply_error FROM jobs WHERE url='u'").fetchone()

    def test_interrupted_dry_run_after_agent_start_is_not_requeued(self):
        with launcher._claude_lock:
            launcher._agent_started.add(7)
        self.addCleanup(launcher._agent_started.discard, 7)
        launcher._release_interrupted("u", 7, dry_run=True, reason="uncertain:interrupted", owner="me")
        self.assertEqual(self.status()[0], "needs_review")

    def test_interrupted_before_agent_start_is_released(self):
        launcher._agent_started.discard(8)
        launcher._release_interrupted("u", 8, dry_run=True, reason="uncertain:interrupted", owner="me")
        self.assertIsNone(self.status()[0])

    def test_uncertain_dry_run_goes_to_review(self):
        self.assertEqual(launcher.record_outcome("u", "uncertain:no_result_line", dry_run=True, owner="me"),
                         "needs_review")

    def test_human_action_failures_go_to_review_in_real_runs(self):
        self.assertEqual(launcher.record_outcome("u", "failed:account_required", dry_run=False, owner="me"),
                         "needs_review")
        self.assertEqual(self.status()[1], "human:account_required")


# --- S4: secrets / logs / browser isolation -------------------------------------------

class RedactionTests(TempDirCase):
    def test_synthetic_transcript_has_no_profile_values(self):
        secrets = self.tmp / "secrets.txt"
        secrets.write_text("483920\nhttps://acme-corp.com/verify?token=abcDEF123\n")
        red = Redactor(PROFILE, secrets_file=secrets)
        transcript = (
            "Filling email testy.applicant@example.org, phone (919) 555-0142 / 9195550142, "
            "address 12 Synthetic Lane 27601. Password Sup3r-S3cret!pw. Gender: decline to answer. "
            "Code 483920 entered; link https://acme-corp.com/verify?token=abcDEF123; other@example.com; "
            "name Testy McTestface; https://linkedin.com/in/testy-synthetic; ssn 123-45-6789; code 771204"
        )
        out = red(transcript)
        for leaked in ("testy.applicant", "555-0142", "9195550142", "Synthetic Lane", "27601", "Sup3r-S3cret",
                       "Decline to answer", "483920", "abcDEF123", "other@example.com", "Testy McTestface",
                       "testy-synthetic", "123-45-6789", "771204"):
            self.assertNotIn(leaked.lower(), out.lower(), leaked)

    def test_agent_env_drops_secrets(self):
        env = {"GEMINI_API_KEY": "g", "OPENAI_API_KEY": "o", "GITHUB_TOKEN": "t", "SOME_SECRET": "s",
               "ANTHROPIC_API_KEY": "a", "PATH": "p", "CLAUDECODE": "1", "APPLYPILOT_GMAIL_CREDENTIALS": "c"}
        with mock.patch.dict(os.environ, env, clear=True):
            out = launcher.agent_env()
        self.assertEqual(set(out), {"ANTHROPIC_API_KEY", "PATH"})


class BrowserIsolationTests(TempDirCase):
    def test_profile_is_created_empty_and_never_cloned(self):
        with mock.patch.object(config, "CHROME_WORKER_DIR", self.tmp), \
                mock.patch.object(config, "get_chrome_user_data", side_effect=AssertionError("cloned")):
            d = chrome.setup_worker_profile(3)
            self.assertEqual(sorted(p.name for p in d.iterdir()), [chrome.PROFILE_MARKER])
            legacy = self.tmp / "worker-4"
            (legacy / "Default").mkdir(parents=True)
            with self.assertRaises(RuntimeError):
                chrome.setup_worker_profile(4)

    def test_second_launcher_cannot_take_the_same_worker_slot(self):
        code = ("import sys\nfrom applypilot.apply import chrome\n"
                "try:\n    chrome.acquire_worker_slot(0)\nexcept RuntimeError:\n    sys.exit(3)\n")
        env = {**os.environ, "APPLYPILOT_DIR": str(self.tmp)}
        with mock.patch.object(config, "CHROME_WORKER_DIR", self.tmp / "chrome-workers"):
            chrome.acquire_worker_slot(0)
            try:
                other = subprocess.run([sys.executable, "-c", code], env=env, timeout=60, check=False)
                self.assertEqual(other.returncode, 3)  # refused: our launcher holds slot 0
            finally:
                chrome.release_worker_slots()
            again = subprocess.run([sys.executable, "-c", code], env=env, timeout=60, check=False)
            self.assertEqual(again.returncode, 0)


class PromptTests(TempDirCase):
    def build(self, **kw):
        pdf = self.tmp / "r.pdf"
        pdf.write_bytes(b"%PDF-1.4 synthetic")
        job = {**JOB, "tailored_resume_path": str(self.tmp / "r.txt"), "fit_score": 8}
        with mock.patch.object(config, "load_profile", return_value=PROFILE), \
                mock.patch.object(config, "load_search_config", return_value={}), \
                mock.patch.object(config, "load_blocked_sso", return_value=["accounts.google.com"]):
            return prompt_mod.build_prompt(job, "resume text", dry_run=True, upload_dir=self.tmp / "up", **kw)

    def test_prompt_has_no_password_and_no_email_tools(self):
        text = self.build()
        self.assertNotIn("Sup3r-S3cret!pw", text)
        for gone in ("search_emails", "read_email", "send_email", "whatever it takes", "You are autonomous"):
            self.assertNotIn(gone, text)
        self.assertIn("UNTRUSTED DATA", text)
        self.assertIn("RESULT:FAILED:account_required", text)
        self.assertTrue((self.tmp / "up" / "Testy_McTestface_Resume.pdf").exists())

    def test_verification_tool_mentioned_only_when_enabled(self):
        self.assertNotIn("get_verification_code", self.build())
        self.assertIn("get_verification_code", self.build(verify_enabled=True))


# --- S3: verification helper ------------------------------------------------------------

def _b64(s):
    import base64
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def gmail_msg(sender, body, age_s=10, mime="text/plain"):
    return {"internalDate": str(int((time.time() - age_s) * 1000)),
            "payload": {"headers": [{"name": "From", "value": sender}], "mimeType": mime,
                        "body": {"data": _b64(body)}}}


class FakeClient:
    def __init__(self, msgs):
        self.msgs, self.queries = msgs, []

    def search(self, q):
        self.queries.append(q)
        return self.msgs


class VerificationHelperTests(TempDirCase):
    def policy(self):
        return make_policy(self.tmp, verify_enabled=True, verify_sender_domains=["acme-corp.com", "myworkday.com"])

    def test_query_is_session_scoped_and_not_agent_controlled(self):
        pol = self.policy()
        client = FakeClient([])
        server = V.Server(pol, lambda: client)
        resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": V.TOOL_NAME, "arguments": {"query": "in:anywhere bank"}}})
        self.assertEqual(json.loads(resp["result"]["content"][0]["text"]), {"status": "not_found"})
        self.assertEqual(client.queries, [f"from:(acme-corp.com OR myworkday.com) after:{int(pol.started_at) - 120}"])

    def test_returns_only_code_and_filters_senders_and_age(self):
        pol = self.policy()
        msgs = [gmail_msg("Bank <alerts@bank.example>", "Your verification code is 111111"),
                gmail_msg("Workday <noreply@myworkday.com>", "Your code: 222222", age_s=3600),
                gmail_msg("Acme Careers <jobs@careers.acme-corp.com>", "Hi! Your verification code is 333333. Secret stuff.")]
        out = V.lookup(pol, FakeClient(msgs))
        self.assertEqual(out, {"code": "333333"})
        self.assertIn("333333", (self.tmp / "secrets.txt").read_text())
        self.assertIsNone(V.extract("Your verification code request was received by Secretary", pol))
        self.assertEqual(V.extract("Use code: AB12CD to continue", pol), {"code": "AB12CD"})

    def test_links_must_pass_navigation_policy(self):
        pol = self.policy()
        evil = gmail_msg("jobs@acme-corp.com", "Confirm: https://evil.example/verify?t=1")
        self.assertEqual(V.lookup(pol, FakeClient([evil])), {"status": "not_found"})
        good = gmail_msg("jobs@acme-corp.com", "Confirm: https://careers.acme-corp.com/verify?t=1")
        self.assertEqual(V.lookup(pol, FakeClient([good])), {"link": "https://careers.acme-corp.com/verify?t=1"})

    def test_mcp_handshake_exposes_one_tool_and_call_limit(self):
        server = V.Server(self.policy(), lambda: FakeClient([]))
        init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "applypilot_verify")
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], [V.TOOL_NAME])
        self.assertEqual(tools[0]["inputSchema"]["properties"], {})
        for i in range(V.MAX_CALLS + 1):
            r = server.handle({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": V.TOOL_NAME}})
        self.assertIn("limit_reached", r["result"]["content"][0]["text"])
        bad = server.handle({"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": {"name": "read_email"}})
        self.assertTrue(bad["result"]["isError"])

    def test_token_scope_must_be_exactly_readonly(self):
        class Resp:
            def __init__(self, data):
                self.status_code, self._d = 200, data

            def json(self):
                return self._d

        class Http:
            def __init__(self, scope):
                self.scope = scope

            def post(self, url, data):
                return Resp({"access_token": "x", "scope": self.scope})

        creds = {"client_id": "c", "client_secret": "s", "refresh_token": "r"}
        V.GmailReadonly(creds, Http(V.READONLY_SCOPE))
        with self.assertRaises(V.VerificationError):
            V.GmailReadonly(creds, Http("https://mail.google.com/"))
        with self.assertRaises(V.VerificationError):
            V.GmailReadonly(creds, Http(V.READONLY_SCOPE + " https://www.googleapis.com/auth/gmail.send"))


# --- run_job end to end with a fake claude process ---------------------------------------

class FakeProc:
    def __init__(self, lines):
        self.stdin = io.StringIO()
        self.stdout = iter(json.dumps(m) + "\n" for m in lines)
        self.returncode, self.pid = 0, 424242

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


class RunJobTests(TempDirCase):

    def run_job(self, lines, setup=None, guard=None):
        app = self.tmp / "app"
        logs = app / "logs"
        logs.mkdir(parents=True)
        killed = []

        def popen(cmd, **kw):
            session = Path(cmd[cmd.index("--settings") + 1]).parent
            if setup:
                setup(session)
            return FakeProc(lines)

        patches = [
            mock.patch.object(config, "APP_DIR", app), mock.patch.object(config, "LOG_DIR", logs),
            mock.patch.object(config, "load_profile", return_value=PROFILE),
            mock.patch.object(launcher, "verification_enabled", return_value=False),
            mock.patch.object(launcher.prompt_mod, "build_prompt", return_value="PROMPT"),
            mock.patch.object(launcher, "reset_worker_dir", return_value=self.tmp),
            mock.patch.object(launcher, "_kill_process_tree", side_effect=killed.append),
            mock.patch.object(launcher.subprocess, "Popen", side_effect=popen),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.popen_calls = popen
        self.guard = guard or FakeGuard()
        result, _ = launcher.run_job(dict(JOB), port=9999, worker_id=5, dry_run=True,
                                     guard=self.guard)  # pyright: ignore[reportArgumentType]
        log_text = "".join(f.read_text(encoding="utf-8") for f in logs.iterdir())
        return result, killed, log_text

    def test_no_guard_never_starts_agent(self):
        with mock.patch.object(launcher.subprocess, "Popen") as popen:
            result, _ = launcher.run_job(dict(JOB), port=9999, worker_id=5, dry_run=True)
        self.assertEqual(result, "failed:network_guard_unavailable")
        popen.assert_not_called()

    def test_guard_verification_failure_never_starts_agent(self):
        started = []
        result, _killed, _ = self.run_job([dict(InitEventTests.GOOD)], setup=started.append,
                                         guard=FakeGuard(fail_activate="interception not active"))
        self.assertEqual(result, "failed:network_guard_unavailable")
        self.assertEqual(started, [])  # Popen (and so the agent) was never reached

    def test_guard_failure_mid_session_kills_agent_and_needs_review(self):
        guard = FakeGuard()

        def setup(session):
            (session / "hook_alive").write_text("1")
            pol = P.SessionPolicy.load(session / "policy.json")
            P.record_audit(pol, {"phase": "pre", "tool": PW + "browser_navigate", "decision": "allow"})
            # the guard loses enforcement while the agent runs
            (session / P.GUARD_FAILED_FILE).write_text("lost CDP")
            guard.failed.set()

        tool = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": PW + "browser_navigate", "input": {"url": JOB["application_url"]}}]}}
        result, killed, _ = self.run_job([dict(InitEventTests.GOOD), tool,
                                          {"type": "result", "result": "RESULT:DRY_RUN"}], setup, guard)
        self.assertEqual(result, "uncertain:network_guard_failed")
        self.assertIn(424242, killed)

    def test_unexpected_tool_surface_kills_session(self):
        bad = {**InitEventTests.GOOD, "tools": InitEventTests.GOOD["tools"] + ["Bash"]}
        result, killed, _ = self.run_job([bad, {"type": "result", "result": "RESULT:DRY_RUN"}])
        self.assertEqual(result, "failed:security_config_unverified")
        self.assertEqual(killed, [424242])

    def test_inactive_hooks_kill_session_at_first_tool_call(self):
        tool = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": PW + "browser_navigate", "input": {"url": JOB["application_url"]}}]}}
        result, killed, _ = self.run_job([dict(InitEventTests.GOOD), tool, {"type": "result", "result": "RESULT:DRY_RUN"}])
        self.assertEqual(result, "uncertain:security_config_unverified")
        self.assertEqual(killed, [424242])

    def test_clean_session_logs_are_redacted(self):
        def setup(session):
            (session / "hook_alive").write_text("1")
            pol = P.SessionPolicy.load(session / "policy.json")
            P.record_audit(pol, {"phase": "pre", "tool": PW + "browser_fill_form", "decision": "allow"})

        fill = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Typing testy.applicant@example.org and (919) 555-0142, pw Sup3r-S3cret!pw"},
            {"type": "tool_use", "id": "t1", "name": PW + "browser_navigate",
             "input": {"url": "https://careers.acme-corp.com/apply?token=SECRETTOKEN1"}}]}}
        final = {"type": "result", "result": "Done.\nRESULT:DRY_RUN"}
        result, killed, logs = self.run_job([dict(InitEventTests.GOOD), fill, final], setup)
        self.assertEqual(result, "dry_run")
        self.assertEqual(killed, [])
        for leaked in ("testy.applicant", "555-0142", "Sup3r-S3cret", "SECRETTOKEN1"):
            self.assertNotIn(leaked, logs)


if __name__ == "__main__":
    unittest.main()
