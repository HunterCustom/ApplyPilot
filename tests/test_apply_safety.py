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

from applypilot import database
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
                applied_at=None, agent_id=None, last_attempted_at=None):
        self.conn.execute(
            """INSERT INTO jobs (url, title, site, application_url, tailored_resume_path,
                                 fit_score, apply_status, apply_attempts, applied_at,
                                 agent_id, last_attempted_at, discovered_at)
               VALUES (?, 'IT Support Specialist', 'Acme', ?, '/tmp/resume.pdf', ?, ?, ?, ?, ?, ?, ?)""",
            (url, app_url or url, score, status, attempts, applied_at, agent_id,
             last_attempted_at, APPLIED_AT),
        )
        self.conn.commit()

    def row(self, url):
        return dict(self.conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone())


class DryRunSafetyTests(ApplySafetyTestCase):
    def test_dry_run_agent_claiming_applied_is_recorded_as_dry_run(self):
        self.add_job(f"{ATS}/1")
        job = launcher.acquire_job(dry_run=True)
        self.assertEqual(self.row(job["url"])["agent_id"], "worker-0:dry_run")

        outcome = launcher.record_outcome(job["url"], "applied", dry_run=True)

        row = self.row(job["url"])
        self.assertEqual(outcome, "dry_run")
        self.assertEqual(row["apply_status"], "dry_run")
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
    def test_codes(self):
        parse = launcher.parse_agent_result
        self.assertEqual(parse("done\nRESULT:DRY_RUN"), "dry_run")
        self.assertEqual(parse("**RESULT:APPLIED**"), "applied")
        self.assertEqual(parse("RESULT:FAILED:not_eligible_location**"), "failed:not_eligible_location")
        self.assertEqual(parse("RESULT:FAILED: captcha"), "captcha")
        self.assertEqual(parse("RESULT:FAILED"), "failed:unknown")
        self.assertEqual(parse("RESULT:FAILED:stuck\nRESULT:EXPIRED"), "expired")

    def test_uncertain_outcomes(self):
        parse = launcher.parse_agent_result
        self.assertEqual(parse(""), "uncertain:no_result_line")
        self.assertEqual(parse("clicked submit, page hung"), "uncertain:no_result_line")
        self.assertEqual(parse("I'll output RESULT:APPLIED later\nRESULT:FAILED:stuck"),
                         "uncertain:conflicting_results")
        self.assertTrue(parse("RESULT:MAYBE").startswith("uncertain:"))


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
        self.assertIsNone(self.row(f"{ATS}/dry")["apply_status"])
        self.assertEqual(self.row(f"{ATS}/live")["apply_status"], "in_progress")

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
