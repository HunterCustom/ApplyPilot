"""Phase 0 apply-safety regression tests.

Every test runs against a fresh temporary SQLite database. No browser, LLM,
network, or user data is touched.
"""
import atexit
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from applypilot import database, eligibility
from applypilot.apply import chrome, launcher

# Importing the launcher registers a Chrome/CDP-port cleanup hook; never run it from tests.
atexit.unregister(chrome.cleanup_on_exit)

ATS = "https://boards.example-ats.test/acme/jobs"
APPLIED_AT = "2026-01-01T00:00:00+00:00"


class ApplySafetyTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "test.db"
        patcher = mock.patch.object(database, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(database.close_connection, self.db_path)
        database.init_db(self.db_path)
        conn = database.get_connection()
        # Guard: never operate on anything except the temporary database.
        db_file = conn.execute("PRAGMA database_list").fetchone()[2]
        self.assertTrue(Path(db_file).resolve().is_relative_to(Path(self._tmp.name).resolve()))
        self.conn = conn

    def add_job(self, url, score=8, app_url=None, status=None, attempts=None,
                applied_at=None, agent_id=None, last_attempted_at=None,
                elig="eligible_nc", elig_version=eligibility.CLASSIFIER_VERSION, tier="high",
                readiness="ready", review="approved"):
        self.conn.execute(
            """INSERT INTO jobs (url, title, site, application_url, tailored_resume_path,
                                 fit_score, apply_status, apply_attempts, applied_at,
                                 agent_id, last_attempted_at, discovered_at,
                                 eligibility_status, eligibility_version, eligibility_tier,
                                 eligibility_reason, readiness, review_status)
               VALUES (?, 'IT Support Specialist', 'Acme', ?, '/tmp/resume.pdf', ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, 'test', ?, ?)""",
            (url, app_url or url, score, status, attempts, applied_at, agent_id,
             last_attempted_at, APPLIED_AT, elig, elig_version, tier, readiness, review),
        )
        self.conn.commit()

    def row(self, url):
        return dict(self.conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone())


class DryRunSafetyTests(ApplySafetyTestCase):
    def test_dry_run_agent_claiming_applied_goes_to_review(self):
        self.add_job(f"{ATS}/1")
        job = launcher.acquire_job(dry_run=True)
        self.assertEqual(self.row(job["url"])["agent_id"], f"{launcher.SESSION_ID}:worker-0:dry_run")
        self.assertEqual(job["agent_id"], f"{launcher.SESSION_ID}:worker-0:dry_run")

        outcome = launcher.record_outcome(job["url"], "applied", dry_run=True)

        # A dry-run agent claiming APPLIED may really have submitted: review, never requeue.
        row = self.row(job["url"])
        self.assertEqual(outcome, "needs_review")
        self.assertEqual(row["apply_status"], "needs_review")
        self.assertEqual(row["apply_error"], "uncertain:applied_claimed_in_dry_run")
        self.assertIsNone(row["applied_at"])
        self.assertIsNone(row["apply_attempts"])

    def test_dry_run_failures_do_not_consume_retry_attempts(self):
        self.add_job(f"{ATS}/1", status="failed", attempts=1)
        job = launcher.acquire_job(dry_run=True)
        launcher.record_outcome(job["url"], "failed:stuck", dry_run=True)
        row = self.row(job["url"])
        self.assertEqual(row["apply_status"], "dry_run")
        self.assertEqual(row["apply_attempts"], 1)
        self.assertEqual(row["apply_error"], "dry_run:failed:stuck")

    def test_dry_run_never_modifies_applied_job(self):
        self.add_job(f"{ATS}/1", status="applied", applied_at=APPLIED_AT)
        for result in ("applied", "dry_run", "uncertain:timeout", "expired"):
            launcher.record_outcome(f"{ATS}/1", result, dry_run=True)
        launcher.mark_result(f"{ATS}/1", "failed", "stuck")
        launcher.mark_result(f"{ATS}/1", "needs_review", "uncertain:timeout")
        row = self.row(f"{ATS}/1")
        self.assertEqual(row["apply_status"], "applied")
        self.assertEqual(row["applied_at"], APPLIED_AT)

    def test_dry_run_job_remains_eligible_for_real_application(self):
        self.add_job(f"{ATS}/1")
        launcher.record_outcome(launcher.acquire_job(dry_run=True)["url"], "dry_run", dry_run=True)

        self.assertIsNone(launcher.acquire_job(dry_run=True), "dry-run batch must move on")
        self.assertEqual(launcher.acquire_job(dry_run=False)["url"], f"{ATS}/1")

    def test_real_run_reporting_dry_run_goes_to_review(self):
        self.add_job(f"{ATS}/1")
        url = launcher.acquire_job()["url"]
        self.assertEqual(launcher.record_outcome(url, "dry_run", dry_run=False), "needs_review")
        self.assertIsNone(self.row(url)["applied_at"])

    def test_real_applied_sets_applied_at(self):
        self.add_job(f"{ATS}/1")
        url = launcher.acquire_job()["url"]
        launcher.record_outcome(url, "applied", dry_run=False)
        row = self.row(url)
        self.assertEqual(row["apply_status"], "applied")
        self.assertIsNotNone(row["applied_at"])


class ResultParsingTests(unittest.TestCase):
    def test_standalone_codes(self):
        parse = launcher.parse_agent_result
        self.assertEqual(parse("done\nRESULT:DRY_RUN"), "dry_run")
        self.assertEqual(parse("**RESULT:APPLIED**"), "applied")
        self.assertEqual(parse("`RESULT:APPLIED` -- submitted successfully"), "applied")
        self.assertEqual(parse("RESULT:FAILED:not_eligible_location**"), "failed:not_eligible_location")
        self.assertEqual(parse("RESULT:FAILED: captcha"), "captcha")
        self.assertEqual(parse("RESULT:FAILED"), "failed:unknown")
        self.assertEqual(parse("RESULT:APPLIED\nRESULT:APPLIED"), "applied")

    def test_incidental_mentions_never_count(self):
        parse = launcher.parse_agent_result
        self.assertEqual(parse("I'll output RESULT:APPLIED once the form is sent."),
                         "uncertain:no_result_line")
        self.assertEqual(parse("The page said 'RESULT:APPLIED' in a banner\nRESULT:FAILED:stuck"),
                         "failed:stuck")
        self.assertEqual(parse("Status: RESULT:APPLIED (according to the page)"), "uncertain:no_result_line")

    def test_uncertain_outcomes(self):
        parse = launcher.parse_agent_result
        self.assertEqual(parse(None), "uncertain:no_final_result")
        self.assertEqual(parse(""), "uncertain:no_result_line")
        self.assertEqual(parse("clicked submit, page hung"), "uncertain:no_result_line")
        self.assertEqual(parse("RESULT:FAILED:stuck\nRESULT:EXPIRED"), "uncertain:conflicting_results")
        self.assertEqual(parse("RESULT:APPLIED\nRESULT:FAILED:stuck"), "uncertain:conflicting_results")
        self.assertEqual(parse("RESULT:MAYBE"), "uncertain:malformed_result")
        self.assertEqual(parse("RESULT:APPLIED: probably"), "uncertain:malformed_result")
        self.assertEqual(parse("RESULT:APPLIED, I think"), "uncertain:malformed_result")
        self.assertEqual(parse("RESULT:FAILED:captcha_after_submit"), "uncertain:captcha_after_submit")


class SessionClassificationTests(unittest.TestCase):
    def classify(self, text, **kw):
        base = {"final_is_error": False, "dry_run": False, "submit_attempts": 0, "unblocked_submits": 0}
        base.update(kw)
        return launcher.classify_session(text, **base)

    def test_failure_after_submit_click_needs_review(self):
        self.assertEqual(self.classify("RESULT:FAILED:page_error", submit_attempts=1),
                         "uncertain:after_submit_action:failed:page_error")
        self.assertEqual(self.classify("RESULT:APPLIED", submit_attempts=1, submit_approved=True,
                                       confirmation_seen=True, released_writes=1,
                                       evidence_correlated=True), "applied")
        self.assertEqual(self.classify("RESULT:FAILED:page_error"), "failed:page_error")

    def test_watchdog_and_agent_error_are_uncertain(self):
        self.assertEqual(self.classify("RESULT:APPLIED", watchdog_fired=True), "uncertain:watchdog_timeout")
        self.assertTrue(self.classify("RESULT:APPLIED", final_is_error=True).startswith("uncertain:"))
        self.assertEqual(self.classify(None, interrupted=True), "uncertain:interrupted")

    def test_dry_run_submit_not_blocked_by_guard(self):
        self.assertEqual(self.classify("RESULT:DRY_RUN", dry_run=True, submit_attempts=1,
                                       unblocked_submits=1), "uncertain:dry_run_submit_not_blocked")
        self.assertEqual(self.classify("RESULT:DRY_RUN", dry_run=True, submit_attempts=1,
                                       unblocked_submits=0), "dry_run")


class CommandAndGuardTests(unittest.TestCase):
    def test_every_command_requires_policy_settings(self):
        for dry in (True, False):
            cmd = launcher.build_agent_command("sonnet", Path("m.json"), dry, Path("s.json"))
            self.assertIn("--strict-mcp-config", cmd)
            self.assertEqual(cmd[cmd.index("--settings") + 1], "s.json")
            with self.assertRaises(ValueError):
                launcher.build_agent_command("sonnet", Path("m.json"), dry, None)

    def test_command_has_no_email_server_tools(self):
        cmd = launcher.build_agent_command("sonnet", Path("m.json"), False, Path("s.json"))
        self.assertNotIn("gmail", " ".join(cmd))
        self.assertNotIn("gmail", str(launcher._make_mcp_config(9222)))

    def test_guard_blocks_submit_like_actions(self):
        from applypilot.apply.dry_run_guard import blocked_reason
        pw = "mcp__playwright__"
        self.assertIsNotNone(blocked_reason(pw + "browser_click", {"element": "Submit Application button"}))
        self.assertIsNotNone(blocked_reason(pw + "browser_click", {"element": "Send application"}))
        self.assertIsNotNone(blocked_reason(pw + "browser_press_key", {"key": "Enter"}))
        self.assertIsNotNone(blocked_reason(pw + "browser_type", {"text": "x", "submit": True}))
        self.assertIsNotNone(blocked_reason(pw + "browser_evaluate", {"function": "() => form.requestSubmit()"}))
        self.assertIsNotNone(blocked_reason("mcp__gmail__send_email", {}))

    def test_guard_allows_navigation_and_filling(self):
        from applypilot.apply.dry_run_guard import blocked_reason
        pw = "mcp__playwright__"
        self.assertIsNone(blocked_reason(pw + "browser_click", {"element": "Apply now button"}))
        self.assertIsNone(blocked_reason(pw + "browser_click", {"element": "Next"}))
        self.assertIsNone(blocked_reason(pw + "browser_fill_form", {"fields": []}))
        self.assertIsNone(blocked_reason(pw + "browser_navigate", {"url": "https://x.test"}))
        self.assertIsNone(blocked_reason("mcp__gmail__search_emails", {}))

    def test_guard_hook_process_exit_codes(self):
        import json
        import subprocess
        import sys
        def run(payload):
            return subprocess.run([sys.executable, "-m", "applypilot.apply.dry_run_guard"],
                                  input=payload, capture_output=True, text=True, timeout=30, check=False)
        blocked = run(json.dumps({"tool_name": "mcp__playwright__browser_click",
                                  "tool_input": {"element": "Submit"}}))
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("DRY RUN GUARD", blocked.stderr)
        allowed = run(json.dumps({"tool_name": "mcp__playwright__browser_snapshot", "tool_input": {}}))
        self.assertEqual(allowed.returncode, 0)
        self.assertEqual(run("not json").returncode, 2)

    def test_submit_action_detection(self):
        self.assertTrue(launcher.is_submit_action("mcp__playwright__browser_click", {"element": "Submit"}))
        self.assertTrue(launcher.is_submit_action("mcp__gmail__send_email", {}))
        self.assertFalse(launcher.is_submit_action("mcp__playwright__browser_click", {"element": "Apply"}))

    def test_captcha_section_never_solves(self):
        from applypilot.apply.prompt import _build_captcha_section
        text = _build_captcha_section().lower()
        self.assertNotIn("capsolver", text)
        self.assertNotIn("createtask", text)
        self.assertIn("result:captcha", text)
        self.assertIn("do not attempt to solve", text)

    def test_import_installs_no_exit_hooks(self):
        self.assertFalse(launcher._exit_hooks_installed)


class ChromeOwnershipTests(unittest.TestCase):
    NETSTAT = (
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    127.0.0.1:9222         0.0.0.0:0              LISTENING       111\n"
        "  TCP    127.0.0.1:92220        0.0.0.0:0              LISTENING       222\n"
        "  TCP    127.0.0.1:50000        127.0.0.1:9222         ESTABLISHED     333\n"
        "  TCP    [::1]:9222             [::]:0                 LISTENING       444\n"
    )

    def test_netstat_parses_exact_local_port_only(self):
        self.assertEqual(chrome.parse_netstat_listeners(self.NETSTAT, 9222), [111, 444])

    def test_only_worker_chrome_is_recognised(self):
        worker = str(chrome.config.CHROME_WORKER_DIR / "worker-0")
        self.assertTrue(chrome.is_worker_chrome(
            f'"chrome.exe" --remote-debugging-port=9222 --user-data-dir={worker}'))
        self.assertFalse(chrome.is_worker_chrome(
            r'"chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\Users\x\AppData\Local\Google\Chrome'))
        self.assertFalse(chrome.is_worker_chrome("node.exe devserver.js --port 9222"))

    def test_foreign_listener_is_never_killed(self):
        with mock.patch.object(chrome, "_listening_pids", return_value=[111]), \
             mock.patch.object(chrome, "_process_command_line", return_value="node.exe server.js"), \
             mock.patch.object(chrome, "_kill_process_tree") as kill:
            self.assertFalse(chrome._kill_on_port(9222))
            kill.assert_not_called()

    def test_leftover_worker_chrome_is_killed(self):
        cmdline = f"chrome.exe --remote-debugging-port=9222 --user-data-dir={chrome.config.CHROME_WORKER_DIR}/worker-0"
        with mock.patch.object(chrome, "_listening_pids", return_value=[111]), \
             mock.patch.object(chrome, "_process_command_line", return_value=cmdline), \
             mock.patch.object(chrome, "_kill_process_tree") as kill:
            self.assertTrue(chrome._kill_on_port(9222))
            kill.assert_called_once_with(111)


class AcquisitionTests(ApplySafetyTestCase):
    def test_target_url_acquires_never_attempted_job(self):
        self.add_job(f"{ATS}/1", app_url=f"{ATS}/1/apply")
        self.assertEqual(launcher.acquire_job(target_url=f"{ATS}/1")["url"], f"{ATS}/1")

    def test_target_url_matches_application_url(self):
        self.add_job("https://indeed.test/viewjob?jk=1", app_url=f"{ATS}/1/apply")
        job = launcher.acquire_job(target_url=f"{ATS}/1/apply")
        self.assertEqual(job["url"], "https://indeed.test/viewjob?jk=1")

    def test_target_url_prefers_exact_match_over_substring(self):
        self.add_job(f"{ATS}/12", score=10)
        self.add_job(f"{ATS}/1", score=7)
        self.assertEqual(launcher.acquire_job(target_url=f"{ATS}/1")["url"], f"{ATS}/1")

    def test_not_ready_or_unapproved_jobs_are_never_claimed(self):
        self.add_job(f"{ATS}/blocked", readiness="blocked")
        self.add_job(f"{ATS}/unreviewed", review=None)
        self.assertIsNone(launcher.acquire_job(target_url=f"{ATS}/blocked", dry_run=True))
        self.assertIsNone(launcher.acquire_job(target_url=f"{ATS}/unreviewed"))   # real run needs approval
        job = launcher.acquire_job(target_url=f"{ATS}/unreviewed", dry_run=True)  # dry run: ready suffices
        self.assertEqual(job["url"], f"{ATS}/unreviewed")
        self.assertIsNone(launcher.acquire_job())


    def test_target_url_rejects_applied_in_progress_and_review(self):
        self.add_job(f"{ATS}/1", status="applied", applied_at=APPLIED_AT)
        self.add_job(f"{ATS}/2", status="in_progress")
        self.add_job(f"{ATS}/3", status="needs_review")
        for n in (1, 2, 3):
            self.assertIsNone(launcher.acquire_job(target_url=f"{ATS}/{n}"))
        self.assertEqual(self.row(f"{ATS}/1")["applied_at"], APPLIED_AT)

    def test_queue_never_returns_applied_or_review_jobs(self):
        self.add_job(f"{ATS}/1", status="applied", applied_at=APPLIED_AT)
        self.add_job(f"{ATS}/2", status="needs_review")
        self.add_job(f"{ATS}/3", status="in_progress")
        self.assertIsNone(launcher.acquire_job())
        self.assertIsNone(launcher.acquire_job(dry_run=True))

    def test_duplicate_application_url_blocked(self):
        shared = f"{ATS}/99/apply"
        self.add_job("https://indeed.test/viewjob?jk=99", app_url=shared,
                     status="applied", applied_at=APPLIED_AT)
        self.add_job(f"{ATS}/99", app_url=shared)
        self.assertIsNone(launcher.acquire_job())
        self.assertIsNone(launcher.acquire_job(target_url=f"{ATS}/99"))

    def test_manual_ats_is_skipped_and_queue_continues(self):
        self.add_job("https://ibegin.tcsapps.com/job/1", score=10)
        self.add_job(f"{ATS}/2", score=7)
        self.assertEqual(launcher.acquire_job()["url"], f"{ATS}/2")
        self.assertEqual(self.row("https://ibegin.tcsapps.com/job/1")["apply_status"], "manual")

    def test_concurrent_workers_never_claim_same_job(self):
        for n in range(6):
            self.add_job(f"{ATS}/{n}")
        claimed, errors = [], []
        barrier = threading.Barrier(8)

        def worker(wid):
            try:
                barrier.wait()
                while (job := launcher.acquire_job(worker_id=wid)) is not None:
                    claimed.append(job["url"])
            except Exception as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)
            finally:
                database.close_connection(self.db_path)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(claimed), sorted(f"{ATS}/{n}" for n in range(6)))


class EligibilityGateTests(ApplySafetyTestCase):
    def test_queue_only_returns_verified_eligible_jobs(self):
        self.add_job(f"{ATS}/inel", score=10, elig="ineligible")
        self.add_job(f"{ATS}/unk", score=10, elig="unknown")
        self.add_job(f"{ATS}/restr", score=10, elig="restricted_remote")
        self.add_job(f"{ATS}/none", score=10, elig=None, elig_version=None)
        self.add_job(f"{ATS}/stale", score=10, elig="eligible_nc", elig_version="0.0.1")
        self.assertIsNone(launcher.acquire_job())
        self.add_job(f"{ATS}/ok", score=7, elig="eligible_us_remote")
        self.assertEqual(launcher.acquire_job()["url"], f"{ATS}/ok")

    def test_explicit_url_cannot_bypass_eligibility(self):
        for status in ("ineligible", "unknown", "restricted_remote", "ineligible_manual"):
            url = f"{ATS}/{status}"
            self.add_job(url, elig=status)
            with self.subTest(status=status):
                self.assertIsNone(launcher.acquire_job(target_url=url))
                self.assertIn(status, launcher.explain_target(url))

    def test_manual_approval_allows_acquisition(self):
        self.add_job(f"{ATS}/1", elig="unknown")
        self.assertIsNone(launcher.acquire_job(target_url=f"{ATS}/1"))
        eligibility.set_manual_decision(f"{ATS}/1", approve=True, conn=self.conn)
        self.assertEqual(launcher.acquire_job(target_url=f"{ATS}/1")["url"], f"{ATS}/1")

    def test_queue_prefers_commute_tier_over_score(self):
        self.add_job(f"{ATS}/remote", score=9, elig="eligible_us_remote", tier="high")
        self.add_job(f"{ATS}/mebane", score=7, elig="eligible_nc", tier="highest")
        self.assertEqual(launcher.acquire_job()["url"], f"{ATS}/mebane")

    def test_stage_queries_gate_scoring_and_tailoring(self):
        for url, elig in ((f"{ATS}/e", "eligible_nc"), (f"{ATS}/u", "unknown"), (f"{ATS}/i", "ineligible")):
            self.conn.execute("INSERT INTO jobs (url, title, full_description, eligibility_status, "
                              "eligibility_version) VALUES (?, 't', 'd', ?, ?)",
                              (url, elig, eligibility.CLASSIFIER_VERSION))
        self.conn.commit()
        pending_score = {j["url"] for j in database.get_jobs_by_stage(self.conn, "pending_score", limit=0)}
        self.assertEqual(pending_score, {f"{ATS}/e", f"{ATS}/u"}, "ineligible jobs never reach the LLM")
        self.conn.execute("UPDATE jobs SET fit_score = 9")
        self.conn.commit()
        pending_tailor = {j["url"] for j in database.get_jobs_by_stage(self.conn, "pending_tailor",
                                                                      min_score=7, limit=0)}
        self.assertEqual(pending_tailor, set(), "documents only for verified-active postings")
        self.conn.execute("UPDATE jobs SET posting_status = 'active'")
        self.conn.commit()
        pending_tailor = {j["url"] for j in database.get_jobs_by_stage(self.conn, "pending_tailor",
                                                                      min_score=7, limit=0)}
        self.assertEqual(pending_tailor, {f"{ATS}/e"}, "only verified-eligible jobs are tailored")


class RetryAndRecoveryTests(ApplySafetyTestCase):
    def test_retry_eligibility_by_attempts(self):
        self.add_job(f"{ATS}/1", status="failed", attempts=2)
        self.add_job(f"{ATS}/2", status="failed", attempts=3)
        self.assertEqual(launcher.acquire_job()["url"], f"{ATS}/1")
        self.assertIsNone(launcher.acquire_job())

    def test_transient_failure_retryable_permanent_failure_not(self):
        self.add_job(f"{ATS}/1")
        url = launcher.acquire_job()["url"]
        launcher.record_outcome(url, "failed:page_error", dry_run=False)
        self.assertEqual(self.row(url)["apply_attempts"], 1)
        url = launcher.acquire_job()["url"]
        launcher.record_outcome(url, "expired", dry_run=False)
        self.assertEqual(self.row(url)["apply_attempts"], 99)
        self.assertIsNone(launcher.acquire_job())

    def test_uncertain_outcome_flags_review_without_retry(self):
        self.add_job(f"{ATS}/1")
        url = launcher.acquire_job()["url"]
        self.assertEqual(launcher.record_outcome(url, "uncertain:timeout", dry_run=False), "needs_review")
        row = self.row(url)
        self.assertEqual(row["apply_status"], "needs_review")
        self.assertIsNone(row["apply_attempts"])
        self.assertIsNone(launcher.acquire_job())

    def test_reset_failed_leaves_applied_and_review_untouched(self):
        self.add_job(f"{ATS}/1", status="failed", attempts=99)
        self.add_job(f"{ATS}/2", status="needs_review")
        self.add_job(f"{ATS}/3", status="applied", applied_at=APPLIED_AT)
        self.assertEqual(launcher.reset_failed(), 1)
        self.assertEqual(self.row(f"{ATS}/2")["apply_status"], "needs_review")
        self.assertEqual(self.row(f"{ATS}/3")["apply_status"], "applied")

    def test_stale_locks_recovered_by_mode(self):
        old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        recent = datetime.now(UTC).isoformat()
        self.add_job(f"{ATS}/real", status="in_progress", agent_id="worker-0", last_attempted_at=old)
        self.add_job(f"{ATS}/dry", status="in_progress", agent_id="worker-1:dry_run", last_attempted_at=old)
        self.add_job(f"{ATS}/live", status="in_progress", agent_id="worker-2", last_attempted_at=recent)

        self.assertEqual(launcher.recover_stale_locks(max_age_seconds=3600), 2)
        self.assertEqual(self.row(f"{ATS}/real")["apply_status"], "needs_review")
        # A stale dry-run claim may hide a submission the best-effort guard missed.
        self.assertEqual(self.row(f"{ATS}/dry")["apply_status"], "needs_review")
        self.assertEqual(self.row(f"{ATS}/live")["apply_status"], "in_progress")

    def test_late_result_cannot_overwrite_manual_resolution(self):
        self.add_job(f"{ATS}/1")
        job = launcher.acquire_job()
        launcher.mark_job(job["url"], "failed", reason="human: not interested")  # human decides
        outcome = launcher.record_outcome(job["url"], "applied", dry_run=False, owner=job["agent_id"])
        self.assertEqual(outcome, "ownership_lost")
        row = self.row(job["url"])
        self.assertEqual(row["apply_status"], "failed")
        self.assertIsNone(row["applied_at"])

    def test_late_result_cannot_overwrite_another_workers_claim(self):
        self.add_job(f"{ATS}/1")
        mine = launcher.acquire_job(worker_id=1)
        # Simulate the claim being taken over (e.g. human reset, then another worker claimed it).
        self.conn.execute("UPDATE jobs SET agent_id = 'other-session:worker-2' WHERE url = ?", (mine["url"],))
        self.conn.commit()
        self.assertEqual(launcher.record_outcome(mine["url"], "failed:stuck", dry_run=False,
                                                 owner=mine["agent_id"]), "ownership_lost")
        self.assertFalse(launcher.release_lock(mine["url"], owner=mine["agent_id"]))
        row = self.row(mine["url"])
        self.assertEqual((row["apply_status"], row["agent_id"]), ("in_progress", "other-session:worker-2"))

    def test_late_result_after_stale_recovery_is_noted_for_reviewer(self):
        self.add_job(f"{ATS}/1")
        job = launcher.acquire_job()
        self.conn.execute("UPDATE jobs SET apply_status = 'needs_review', agent_id = NULL WHERE url = ?",
                          (job["url"],))
        self.conn.commit()
        self.assertEqual(launcher.record_outcome(job["url"], "applied", dry_run=False, owner=job["agent_id"]),
                         "ownership_lost")
        row = self.row(job["url"])
        self.assertEqual(row["apply_status"], "needs_review")
        self.assertIn("late result: applied", row["apply_error"])

    def test_stale_recovery_skips_own_session_claims(self):
        old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        self.add_job(f"{ATS}/mine", status="in_progress", agent_id=f"{launcher.SESSION_ID}:worker-0",
                     last_attempted_at=old)
        self.assertEqual(launcher.recover_stale_locks(max_age_seconds=3600), 0)
        self.assertEqual(self.row(f"{ATS}/mine")["apply_status"], "in_progress")

    def test_dry_run_unblocked_submit_goes_to_review(self):
        self.add_job(f"{ATS}/1")
        job = launcher.acquire_job(dry_run=True)
        self.assertEqual(launcher.record_outcome(job["url"], "uncertain:dry_run_submit_not_blocked",
                                                 dry_run=True, owner=job["agent_id"]), "needs_review")
        self.assertIsNone(self.row(job["url"])["applied_at"])

    def test_interrupted_real_job_after_agent_start_needs_review(self):
        self.add_job(f"{ATS}/1")
        self.add_job(f"{ATS}/2")
        url1 = launcher.acquire_job(worker_id=5)["url"]
        launcher._agent_started.add(5)
        self.addCleanup(launcher._agent_started.discard, 5)
        launcher._release_interrupted(url1, 5, dry_run=False, reason="uncertain:interrupted")
        self.assertEqual(self.row(url1)["apply_status"], "needs_review")

        url2 = launcher.acquire_job(worker_id=6)["url"]
        launcher._release_interrupted(url2, 6, dry_run=False, reason="uncertain:interrupted")
        self.assertIsNone(self.row(url2)["apply_status"], "agent never started: safe to release")


if __name__ == "__main__":
    unittest.main()
