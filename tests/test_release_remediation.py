"""Regression tests for FINAL_RELEASE_AUDIT.md (R1-R12). Synthetic data, temp DBs, mocks only.

Each test reproduces the audited failure and asserts that the prohibited outcome is *prevented*
(no approved artifact, no claim, no request, no second owner), not merely logged.
"""
from __future__ import annotations

import atexit
import hashlib
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from test_preparation_pipeline import GOOD_JSON, PROFILE, FakeLLM, TailorTests, TempEnv

from applypilot import config, database, readiness
from applypilot.apply import chrome, launcher
from applypilot.apply import prompt as prompt_mod
from applypilot.scoring import cover_letter, tailor
from applypilot.scoring import pdf as pdf_mod

atexit.unregister(chrome.cleanup_on_exit)
ROOT = Path(__file__).resolve().parents[1]


def _json_with(**changes):
    data = json.loads(json.dumps(GOOD_JSON))
    for k, v in changes.items():
        data[k] = v
    return data


class RawJudgeLLM(FakeLLM):
    """FakeLLM whose judge returns an exact raw string."""

    def __init__(self, judge_raw: str, **kw):
        super().__init__(**kw)
        self.judge_raw = judge_raw

    def chat(self, messages, **kw):
        if "resume quality judge" in messages[0]["content"].lower():
            self.calls.append("judge")
            return self.judge_raw
        return super().chat(messages, **kw)


# --- R1: judge parser ---------------------------------------------------------------------------------

class JudgeParserTests(unittest.TestCase):
    def test_only_a_single_leading_pass_line_passes(self):
        cases = {
            "VERDICT: PASS\nISSUES: none": "PASS",
            "**VERDICT:** PASS\nISSUES: none": "PASS",
            "VERDICT: FAIL\nISSUES: it would be VERDICT: PASS without the invented degree": "FAIL",
            "VERDICT: FAIL\nISSUES: invented degree": "FAIL",
            "Thinking... VERDICT: PASS": "MALFORMED",
            "VERDICT: PASS\nVERDICT: FAIL": "FAIL",
            "ISSUES: none": "MALFORMED",
            "": "MALFORMED",
            "I would say VERDICT: PASS overall": "MALFORMED",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(tailor.parse_judge_verdict(raw)[0], want)


# --- R1: fabricated claims cannot be approved even with a passing judge --------------------------------

class GroundingTests(TailorTests):
    def run_strict(self, data, judge_raw="VERDICT: PASS\nISSUES: none"):
        url = self.job()
        self.llm(RawJudgeLLM(judge_raw, tailor=data))
        tailor.run_tailoring(min_score=7, validation_mode="strict")
        return self.row(url)

    def assert_not_approved(self, r):
        self.assertIsNone(r["tailored_resume_path"])
        self.assertIsNone(r["resume_sha"])
        self.assertNotEqual(r["resume_validation"], "passed")
        self.assertEqual(list((self.app / "tailored_resumes").glob("*.txt")), [])

    def test_invented_leadership_and_degree_rejected_with_passing_judge(self):
        """Audit reproduction: a fake passing judge let an invented leadership claim and degree through."""
        data = _json_with(education="Bachelor of Science in Computer Science, Synthetic Community College")
        data["experience"][0]["bullets"] = data["experience"][0]["bullets"] + [
            "Supervised a team of technicians responsible for clinical endpoints"]
        r = self.run_strict(data)
        self.assert_not_approved(r)
        reasons = " ".join(json.loads(r["resume_validation_reasons"]))
        self.assertIn("education", reasons)
        self.assertIn("leadership", reasons)

    def test_real_number_moved_to_another_context_is_rejected(self):
        data = json.loads(json.dumps(GOOD_JSON))
        data["experience"][1]["bullets"] = ["Imaged and deployed Windows 11 workstations for 250 servers"]
        self.assert_not_approved(self.run_strict(data))

    def test_invented_senior_title_is_rejected(self):
        data = json.loads(json.dumps(GOOD_JSON))
        data["experience"][0]["header"] = "Senior Help Desk Manager, Synthetic Health Co"
        self.assert_not_approved(self.run_strict(data))

    def test_judge_fail_quoting_pass_is_never_approved(self):
        r = self.run_strict(GOOD_JSON, "VERDICT: FAIL\nISSUES: would be VERDICT: PASS if the tool list were real")
        self.assert_not_approved(r)

    def test_malformed_judge_output_is_never_approved(self):
        self.assert_not_approved(self.run_strict(GOOD_JSON, "Looks fine to me."))


# --- R1: readiness and the queue bind to the exact artifacts -------------------------------------------

class ArtifactBindingTests(TailorTests):
    def prepared(self):
        (self.app / "pipeline.yaml").write_text("verify_delay_s: 0\nmin_score: 7\nrequire_cover_letter: true\n",
                                                encoding="utf-8")
        url = self.job()
        self.llm(FakeLLM())
        tailor.run_tailoring(min_score=7, validation_mode="strict")
        cover_letter.run_cover_letters(min_score=7, validation_mode="strict")
        readiness.run_prepare(self.conn)
        self.assertEqual(self.row(url)["readiness"], "ready", self.row(url)["readiness_reasons"])
        return url

    def reasons(self, url):
        ok, reasons, _ = readiness.evaluate(self.row(url), self.conn)
        self.assertFalse(ok)
        return " | ".join(reasons)

    def test_hashes_recorded_for_text_and_pdf(self):
        r = self.row(self.prepared())
        for path_c, sha_c in (("tailored_resume_path", "resume_sha"), ("resume_pdf_path", "resume_pdf_sha"),
                              ("cover_letter_path", "cover_sha"), ("cover_pdf_path", "cover_pdf_sha")):
            self.assertEqual(hashlib.sha256(Path(r[path_c]).read_bytes()).hexdigest(), r[sha_c])

    def test_replaced_approved_text_blocks_readiness(self):
        url = self.prepared()
        Path(self.row(url)["tailored_resume_path"]).write_text("Invented resume\n", encoding="utf-8")
        self.assertIn("text changed after validation", self.reasons(url))

    def test_replaced_pdf_blocks_readiness(self):
        url = self.prepared()
        Path(self.row(url)["resume_pdf_path"]).write_bytes(b"%PDF other")
        self.assertIn("PDF changed after rendering", self.reasons(url))

    def test_missing_pdf_blocks_readiness(self):
        url = self.prepared()
        Path(self.row(url)["resume_pdf_path"]).unlink()
        self.assertIn("PDF is missing", self.reasons(url))

    def test_legacy_unvalidated_cover_blocks_readiness_when_required(self):
        url = self.prepared()
        legacy = self.app / "legacy_CL.txt"
        legacy.write_text("Unvalidated letter", encoding="utf-8")
        self.conn.execute("UPDATE jobs SET cover_letter_path = ?, cover_validation = NULL WHERE url = ?",
                          (str(legacy), url))
        self.assertIn("no validated cover letter", self.reasons(url))

    def build_prompt(self, job):
        with mock.patch.object(config, "load_profile", return_value={**PROFILE, "personal": {
                **PROFILE["personal"], "full_name": "Testy Applicant"}}), \
                mock.patch.object(config, "load_search_config", return_value={}), \
                mock.patch.object(config, "load_blocked_sso", return_value=[]):
            return prompt_mod.build_prompt(job, "IGNORED", dry_run=True, upload_dir=self.app / "up")

    def test_unvalidated_cover_is_never_given_to_the_agent(self):
        url = self.prepared()
        legacy = self.app / "legacy_CL.txt"
        legacy.write_text("UNVALIDATED-LETTER-MARKER", encoding="utf-8")
        legacy.with_suffix(".pdf").write_bytes(b"%PDF legacy")
        self.conn.execute("UPDATE jobs SET cover_letter_path = ?, cover_validation = NULL WHERE url = ?",
                          (str(legacy), url))
        text = self.build_prompt(self.row(url))
        self.assertNotIn("UNVALIDATED-LETTER-MARKER", text)
        self.assertNotIn("Cover_Letter.pdf", text)
        self.assertIn("do not write one", text)
        self.assertFalse((self.app / "up" / "Testy_Applicant_Cover_Letter.pdf").exists())

    def test_agent_receives_exactly_the_validated_bytes(self):
        url = self.prepared()
        r = self.row(url)
        text = self.build_prompt(r)
        self.assertIn(Path(r["tailored_resume_path"]).read_text(encoding="utf-8").strip()[:60], text)
        self.assertNotIn("IGNORED", text)  # the caller's text is never used
        up = self.app / "up" / "Testy_Applicant_Resume.pdf"
        self.assertEqual(hashlib.sha256(up.read_bytes()).hexdigest(), r["resume_pdf_sha"])
        Path(r["resume_pdf_path"]).write_bytes(b"%PDF swapped")
        with self.assertRaises(ValueError):
            self.build_prompt(self.row(url))

    def test_failed_regeneration_leaves_no_stale_pdf(self):
        url = self.prepared()
        old_pdf = Path(self.row(url)["resume_pdf_path"])
        self.assertTrue(old_pdf.exists())
        (self.app / "resume.txt").write_text((self.app / "resume.txt").read_text(encoding="utf-8") + "\nX\n",
                                             encoding="utf-8")
        readiness.run_prepare(self.conn)  # stale -> cleared for regeneration
        with mock.patch("applypilot.scoring.pdf.convert_to_pdf", side_effect=RuntimeError("render failed")):
            tailor.run_tailoring(min_score=7, validation_mode="strict")
        r = self.row(url)
        self.assertEqual(r["resume_validation"], "passed")
        self.assertIsNone(r["resume_pdf_sha"])
        self.assertFalse(old_pdf.exists())  # the older rendering was removed with the new text
        self.assertIn("PDF has not been rendered", self.reasons(url))

    def test_approval_is_bound_to_reviewed_artifacts(self):
        url = self.prepared()
        ok, msg = readiness.review(url, "approve", "checked", self.conn)
        self.assertTrue(ok, msg)
        self.assertEqual(json.loads(self.row(url)["review_artifacts"])["resume"], self.row(url)["resume_sha"])
        # Re-tailoring (new text) silently replaces documents -> the approval no longer applies.
        self.conn.execute("UPDATE jobs SET resume_sha = 'x' WHERE url = ?", (url,))
        self.assertIn("changed since human approval", self.reasons(url))
        self.conn.execute("UPDATE jobs SET resume_sha = ? WHERE url = ?",
                          (hashlib.sha256(Path(self.row(url)["tailored_resume_path"]).read_bytes()).hexdigest(), url))
        Path(self.row(url)["cover_letter_path"]).write_text("changed", encoding="utf-8")
        readiness.run_prepare(self.conn)
        r = self.row(url)
        self.assertIsNone(r["review_status"])
        self.assertIn("approval withdrawn", r["review_note"])

    def test_claim_rechecks_artifacts_and_live_approval(self):
        """Audit R8 reproduction: a job deferred after the claim still passed _still_ready."""
        url = self.prepared()
        readiness.review(url, "approve", "checked", self.conn)
        job = launcher.acquire_job(target_url=url, dry_run=False)
        self.assertIsNotNone(job)
        readiness.review(url, "defer", "not now", self.conn)
        ok, reasons = launcher._still_ready(dict(job), dry_run=False)
        self.assertFalse(ok)
        self.assertTrue(any("approval no longer stands" in r for r in reasons), reasons)

    def test_claim_rechecks_ownership(self):
        url = self.prepared()
        readiness.review(url, "approve", "checked", self.conn)
        job = launcher.acquire_job(target_url=url, dry_run=False)
        self.conn.execute("UPDATE jobs SET agent_id = 'someone-else' WHERE url = ?", (url,))
        self.conn.commit()
        ok, reasons = launcher._still_ready(dict(job), dry_run=False)
        self.assertFalse(ok)
        self.assertIn("claim no longer held by this worker", reasons)

    def test_manual_submission_refused_for_inflight_claim(self):
        url = self.prepared()
        readiness.review(url, "approve", "checked", self.conn)
        launcher.acquire_job(target_url=url, dry_run=False)
        ok, msg = readiness.review(url, "manual_submitted", "", self.conn)
        self.assertFalse(ok)
        self.assertIn("worker currently holds", msg)
        self.assertEqual(self.row(url)["apply_status"], "in_progress")

    def test_review_history_is_append_only(self):
        url = self.prepared()
        readiness.review(url, "approve", "a", self.conn)
        readiness.review(url, "defer", "b", self.conn)
        events = [tuple(r) for r in self.conn.execute(
            "SELECT event, detail FROM application_events WHERE url = ? ORDER BY id", (url,))]
        self.assertEqual(events, [("review_approve", "a"), ("review_defer", "b")])


# --- R2: rendering boundary ---------------------------------------------------------------------------

class RenderEscapingTests(unittest.TestCase):
    MARK = '<img src="http://127.0.0.1:9/x"><script>document.title="pwned"</script><b>bold</b>'

    def test_resume_fields_are_escaped(self):
        text = (f"Testy Applicant\nIT Support {self.MARK}\nmail@example.org | {self.MARK}\n\nSUMMARY\n{self.MARK}\n\n"
                f"TECHNICAL SKILLS\nSystems: {self.MARK}\n\nEXPERIENCE\nRole {self.MARK}\n- did {self.MARK}\n\n"
                f"EDUCATION\n{self.MARK}\n")
        html = pdf_mod.build_html(pdf_mod.parse_resume(text))
        self.assertNotIn("<img", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("<b>bold", html)
        self.assertIn("&lt;script&gt;", html)

    def test_letter_template_keeps_every_paragraph_in_order(self):
        paras = [f"Paragraph {i} with <tag> & detail." for i in range(12)]
        html = pdf_mod.build_letter_html("\n\n".join(paras))
        self.assertNotIn("<tag>", html)
        positions = [html.index(f"Paragraph {i} with &lt;tag&gt; &amp; detail.") for i in range(12)]
        self.assertEqual(positions, sorted(positions))

    def test_cover_letter_uses_letter_template(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "Job_CL.txt"
            p.write_text("Dear Hiring Manager,\n\nFirst paragraph.\n\nSincerely,\nTesty", encoding="utf-8")
            out = pdf_mod.convert_to_pdf(p, html_only=True)
            html = out.read_text(encoding="utf-8")
        self.assertIn("<p>First paragraph.</p>", html)
        self.assertNotIn("section-title", html)


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    hits: ClassVar[list[str]] = []

    def do_GET(self):
        _CountingHandler.hits.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"x")

    def log_message(self, *a):
        pass


@unittest.skipIf(os.environ.get("APPLYPILOT_SKIP_BROWSER_TESTS") == "1", "browser tests disabled")
class RenderSafetyTests(unittest.TestCase):
    """Real Playwright rendering: offline, script-free, every request aborted (audit R2)."""

    def setUp(self):
        _CountingHandler.hits = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.tmp = Path(tempfile.mkdtemp())

    def test_active_markup_makes_no_request_and_runs_no_script(self):
        base = f"http://127.0.0.1:{self.port}"
        html = (f'<html><head><link rel="stylesheet" href="{base}/css"></head><body>'
                f'<img src="{base}/img"><iframe src="{base}/frame"></iframe>'
                f'<script>fetch("{base}/script"); document.body.innerText = "SCRIPT RAN";</script>'
                f'<p>static text</p></body></html>')
        guard = pdf_mod.RenderGuard()
        out = self.tmp / "x.pdf"
        pdf_mod.render_pdf(html, str(out), guard)
        self.assertTrue(out.exists() and out.stat().st_size > 500)
        self.assertEqual(_CountingHandler.hits, [])         # nothing reached the server
        self.assertTrue(guard.attempted)                     # ...because the attempts were aborted
        self.assertTrue(all(u.startswith(base) for u in guard.attempted))
        self.assertFalse((self.tmp / "x.pdf.partial").exists())

    def test_rendered_text_matches_the_reviewed_source(self):
        from playwright.sync_api import sync_playwright
        letter = "Dear Hiring Manager,\n\nI support <Windows 11> & Microsoft 365 users.\n\nSincerely,\nTesty"
        html = pdf_mod.build_letter_html(letter)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(java_script_enabled=False, offline=True)
            ctx.route("**/*", lambda r: r.abort())
            page = ctx.new_page()
            page.set_content(html, wait_until="load")
            rendered = page.inner_text("body")
            browser.close()
        norm = lambda s: " ".join(s.split())
        self.assertEqual(norm(rendered), norm(letter))



# --- R3: posting verification trust boundary ------------------------------------------------------------

from applypilot import eligibility, verify


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    hits: ClassVar[list[str]] = []
    routes: ClassVar[dict[str, tuple[int, dict, bytes]]] = {}

    def do_GET(self):
        _RedirectHandler.hits.append(self.path)
        status, headers, body = _RedirectHandler.routes.get(self.path, (404, {}, b"missing"))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _literal_resolver(host, port):
    return [host.strip("[]")]


def _public_resolver(host, port):
    return ["93.184.216.34"]


class DestinationPolicyTests(unittest.TestCase):
    def test_non_public_and_malformed_destinations_are_refused(self):
        cases = ["http://127.0.0.1/job", "http://10.0.0.5/job", "http://192.168.1.1/", "http://169.254.169.254/latest",
                 "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://user:pw@careers.example.org/",
                 "http://careers.example.org:8080/", "http://localhost/", "http://hr.corp.internal/", "ftp://x.org/",
                 "file:///C:/Windows/win.ini"]
        for url in cases:
            with self.subTest(url=url):
                resolver = _public_resolver if "example.org" in url else _literal_resolver
                with self.assertRaises(verify.UnsafeDestination):
                    verify.check_destination(url, resolve=resolver)
        with self.assertRaises(verify.UnsafeDestination):  # public name resolving to a private address
            verify.check_destination("https://careers.example.org/", resolve=lambda h, p: ["10.1.2.3"])
        with self.assertRaises(verify.UnsafeDestination):  # mixed answers: any private address refuses
            verify.check_destination("https://careers.example.org/", resolve=lambda h, p: ["93.184.216.34", "127.0.0.1"])
        self.assertEqual(verify.check_destination("https://careers.example.org/jobs/1", resolve=_public_resolver),
                         ["93.184.216.34"])


class SafeFetchTests(unittest.TestCase):
    """Real loopback server; the policy is told only 127.0.0.1 is 'public' so loopback can stand in."""

    def setUp(self):
        _RedirectHandler.hits = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        names = {"careers.synth.test": ["127.0.0.1"], "internal.synth.test": ["10.0.0.7"]}
        self.kw = {"resolve": lambda h, p: names[h], "is_public": lambda ip: ip == "127.0.0.1",
                   "ports": (self.port,)}

    def url(self, host, path):
        return f"http://{host}:{self.port}{path}"

    def test_connection_is_pinned_to_the_checked_address(self):
        _RedirectHandler.routes = {"/job": (200, {}, b"<h1>IT Support</h1>")}
        status, _final, _body = verify.safe_fetch(self.url("careers.synth.test", "/job"), 5, **self.kw)
        # careers.synth.test does not exist in real DNS: success proves no second lookup was made.
        self.assertEqual((status, _RedirectHandler.hits), (200, ["/job"]))

    def test_redirect_to_internal_address_is_refused_before_connecting(self):
        _RedirectHandler.routes = {"/job": (302, {"Location": self.url("internal.synth.test", "/secret")}, b"")}
        with self.assertRaises(verify.UnsafeDestination):
            verify.safe_fetch(self.url("careers.synth.test", "/job"), 5, **self.kw)
        self.assertEqual(_RedirectHandler.hits, ["/job"])

    def test_redirect_to_ip_literal_and_other_port_refused(self):
        for loc in (f"http://127.0.0.1:{self.port + 1}/x", "http://169.254.169.254/latest/meta-data"):
            with self.subTest(loc=loc):
                _RedirectHandler.hits = []
                _RedirectHandler.routes = {"/job": (301, {"Location": loc}, b"")}
                with self.assertRaises(verify.UnsafeDestination):
                    verify.safe_fetch(self.url("careers.synth.test", "/job"), 5, **self.kw)
                self.assertEqual(_RedirectHandler.hits, ["/job"])

    def test_redirect_loop_is_bounded(self):
        _RedirectHandler.routes = {"/job": (302, {"Location": "/job"}, b"")}
        with self.assertRaises(verify.UnsafeDestination):
            verify.safe_fetch(self.url("careers.synth.test", "/job"), 5, **self.kw)
        self.assertEqual(len(_RedirectHandler.hits), verify.MAX_REDIRECTS + 1)

    def test_robots_applies_to_every_hop(self):
        _RedirectHandler.routes = {"/job": (302, {"Location": "/private/job"}, b""),
                                   "/private/job": (200, {}, b"secret")}
        with self.assertRaises(verify.UnsafeDestination):
            verify.safe_fetch(self.url("careers.synth.test", "/job"), 5,
                              hop_allowed=lambda u: "/private" not in u, **self.kw)
        self.assertEqual(_RedirectHandler.hits, ["/job"])

    def test_final_url_is_recorded(self):
        _RedirectHandler.routes = {"/job": (302, {"Location": "/jobs/77"}, b""),
                                   "/jobs/77": (200, {}, b"<h1>IT Support Specialist</h1>" + b"x " * 300)}
        status, final, _ = verify.safe_fetch(self.url("careers.synth.test", "/job"), 5, **self.kw)
        self.assertEqual((status, final), (200, self.url("careers.synth.test", "/jobs/77")))


class PostingMatchTests(unittest.TestCase):
    PAGE = "<h1>IT Support Specialist</h1><p>Apply now</p>" + "x " * 400

    def test_redirect_to_unrelated_site_is_not_active(self):
        """Audit reproduction: a redirect to a generic site with one title token was marked active."""
        st, _ = verify.classify_response(200, "https://www.unrelated-blog.test/support", "<p>Support</p>" + "y " * 400,
                                         "IT Support Specialist", "https://careers.synthbank.test/jobs/77")
        self.assertEqual(st, "uncertain")
        st, _ = verify.classify_response(200, "https://jobs.other.test/x", self.PAGE, "IT Support Specialist",
                                         "https://careers.synthbank.test/jobs/77")
        self.assertEqual(st, "uncertain")

    def test_other_workday_tenant_is_not_the_same_site(self):
        st, _ = verify.classify_response(200, "https://other.wd5.myworkdayjobs.com/job/1", self.PAGE,
                                         "IT Support Specialist", "https://synth.wd5.myworkdayjobs.com/job/1")
        self.assertEqual(st, "uncertain")

    def test_title_words_must_match_whole_words(self):
        st, _ = verify.classify_response(200, "", "<p>Itinerary supporting specialists</p>" + "z " * 400,
                                         "IT Support Specialist", "")
        self.assertEqual(st, "uncertain")

    def test_requisition_must_match(self):
        st, why = verify.classify_response(200, "https://careers.synthbank.test/jobs/77", self.PAGE + " Req R-1234",
                                           "IT Support Specialist", "https://careers.synthbank.test/jobs/77", "R-9999")
        self.assertEqual(st, "uncertain")
        self.assertIn("requisition", why)
        st, _ = verify.classify_response(200, "https://careers.synthbank.test/jobs/77", self.PAGE + " Req R-9999",
                                         "IT Support Specialist", "https://careers.synthbank.test/jobs/77", "R-9999")
        self.assertEqual(st, "active")


class InputBindingTests(TailorTests):
    def test_changed_application_url_invalidates_verification(self):
        """Audit reproduction: a changed application URL stayed fresh under the old URL's evidence."""
        url = self.job()
        self.assertTrue(verify.is_fresh(self.row(url))[0])
        self.conn.execute("UPDATE jobs SET application_url = 'https://careers.synthbank.test/jobs/other'"
                          " WHERE url = ?", (url,))
        ok, why = verify.is_fresh(self.row(url))
        self.assertFalse(ok)
        self.assertIn("different URL", why)

    def test_location_change_invalidates_eligibility(self):
        """Audit reproduction: eligibility kept a current-version decision after a location change."""
        url = "https://careers.synthbank.test/jobs/88"
        self.add(url, application_url=url, location="Raleigh, NC")
        cfg = eligibility.EligibilityConfig(tiers={"acceptable": ["Raleigh"]})
        eligibility.classify_jobs(self.conn, cfg)
        self.assertEqual(self.row(url)["eligibility_status"], "eligible_nc")
        self.conn.execute("UPDATE jobs SET location = 'Austin, TX' WHERE url = ?", (url,))
        self.assertFalse(eligibility.decision_is_current(self.row(url), cfg))
        eligibility.classify_jobs(self.conn, cfg)
        self.assertEqual(self.row(url)["eligibility_status"], "ineligible")

    def test_manual_decision_is_bound_to_posting_text(self):
        url = self.job()
        self.assertTrue(eligibility.decision_is_current(self.row(url)))
        self.conn.execute("UPDATE jobs SET location = 'Austin, TX' WHERE url = ?", (url,))
        ok, reasons, _ = readiness.evaluate(self.row(url), self.conn)
        self.assertFalse(ok)
        self.assertTrue(any("eligibility decision is stale" in r for r in reasons), reasons)

    def test_cover_letters_only_for_verified_eligible_postings(self):
        url = self.job()
        fake = self.llm(FakeLLM())
        tailor.run_tailoring(min_score=7, validation_mode="strict")
        self.conn.execute("UPDATE jobs SET posting_status = 'closed' WHERE url = ?", (url,))
        self.conn.commit()
        cover_letter.run_cover_letters(min_score=7, validation_mode="strict")
        self.assertIsNone(self.row(url)["cover_letter_path"])
        self.assertNotIn("cover", fake.calls)


# --- R11: scoring ---------------------------------------------------------------------------------------

class _TextLLM:
    model = "fake"

    def __init__(self, text):
        self.text = text

    def chat(self, *a, **k):
        return self.text


class ScoreParsingTests(unittest.TestCase):
    def test_conflicting_scores_are_rejected(self):
        """Audit reproduction: 'SCORE: 2' followed by 'SCORE: 100' produced 10."""
        from applypilot.scoring import scorer
        for text in ("SCORE: 2\nSCORE: 100\nREASONING: x", "SCORE: 100\nREASONING: x", "SCORE: 0\nREASONING: x"):
            with self.subTest(text=text), mock.patch.object(scorer, "get_client", return_value=_TextLLM(text)), \
                    mock.patch.dict(os.environ, {"APPLYPILOT_SCORE_ATTEMPTS": "1"}):
                self.assertEqual(scorer.score_job("r", {"title": "t", "site": "s"})["score"], 0)
        self.assertEqual(scorer._parse_score_response("SCORE: 2\nSCORE: 9\nREASONING: x")["score"], 0)
        self.assertEqual(scorer._parse_score_response("SCORE: 7\nREASONING: good")["score"], 7)

    def test_score_config_identifies_evidence_and_resume_versions(self):
        from applypilot.scoring import scorer
        a = scorer.score_config("evidence A", "resume")
        b = scorer.score_config("evidence B", "resume")
        self.assertNotEqual(a["evidence_sha"], b["evidence_sha"])
        self.assertEqual(a["resume_sha"], b["resume_sha"])



# --- R5: scheduler ownership, recovery, migrations ------------------------------------------------------

from applypilot import daily, opsconfig, runctl

_CONTENDER = r"""
import json, sys, time
from pathlib import Path
from applypilot.daily import RunLock
path, start, hold = Path(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
while time.time() < start:
    time.sleep(0.001)
lock = RunLock(path)
got = lock.acquire()
print(json.dumps({"acquired": got, "took_over": lock.took_over}), flush=True)
if got:
    time.sleep(hold)
    if len(sys.argv) > 4 and sys.argv[4] == "crash":
        import os
        os._exit(9)   # die without releasing
    lock.release()
"""

_MIGRATOR = r"""
import sys, time
from pathlib import Path
from applypilot import database
path, start = Path(sys.argv[1]), float(sys.argv[2])
while time.time() < start:
    time.sleep(0.001)
conn = database.init_db(path)
print(len(conn.execute("PRAGMA table_info(jobs)").fetchall()), flush=True)
"""


def _spawn(code, *args):
    env = {**os.environ, "APPLYPILOT_DIR": tempfile.gettempdir()}
    return subprocess.Popen([sys.executable, "-c", code, *map(str, args)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)


class SchedulerLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lock = self.tmp / "daily.lock"

    def results(self, procs, timeout=60):
        out = []
        for p in procs:
            stdout, stderr = p.communicate(timeout=timeout)
            self.assertTrue(stdout.strip(), stderr)
            out.append(json.loads(stdout.strip().splitlines()[0]))
        return out

    def test_simultaneous_contenders_on_a_stale_lock_exactly_one_wins(self):
        """Audit reproduction: two contenders read the same stale owner and both acquired."""
        self.lock.write_text(json.dumps({"pid": 999999999, "token": "dead"}), encoding="utf-8")
        start = time.time() + 2.0
        procs = [_spawn(_CONTENDER, self.lock, start, 3.0) for _ in range(4)]
        res = self.results(procs)
        self.assertEqual(sum(r["acquired"] for r in res), 1, res)
        winner = next(r for r in res if r["acquired"])
        self.assertEqual(winner["took_over"]["token"], "dead")

    def test_crashed_owner_is_recovered_without_a_race(self):
        holder = _spawn(_CONTENDER, self.lock, 0, 1.0, "crash")
        self.assertTrue(self.results([holder])[0]["acquired"])
        nxt = _spawn(_CONTENDER, self.lock, 0, 0)
        res = self.results([nxt])[0]
        self.assertTrue(res["acquired"])
        self.assertIsNotNone(res["took_over"])  # the crash is reported

    def test_a_released_owner_cannot_remove_a_newer_owners_lock(self):
        a = daily.RunLock(self.lock)
        self.assertTrue(a.acquire())
        a.release()
        b = daily.RunLock(self.lock)
        self.assertTrue(b.acquire())
        self.addCleanup(b.release)
        a.release()  # late/double release of the old owner
        other = self.results([_spawn(_CONTENDER, self.lock, 0, 0)])[0]
        self.assertFalse(other["acquired"])
        self.assertTrue(self.lock.exists())

    def test_an_empty_or_half_written_lock_file_does_not_grant_ownership_to_two(self):
        self.lock.write_text("{\"pid\": 12", encoding="utf-8")  # torn write
        start = time.time() + 1.5
        res = self.results([_spawn(_CONTENDER, self.lock, start, 2.0) for _ in range(3)])
        self.assertEqual(sum(r["acquired"] for r in res), 1, res)

    def test_concurrent_bootstrap_of_an_old_schema_is_serialized(self):
        import sqlite3
        db = self.tmp / "old.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT, salary TEXT, description TEXT, location TEXT,"
                  " site TEXT, strategy TEXT, discovered_at TEXT)")
        c.execute("INSERT INTO jobs (url, title) VALUES ('https://x.test/1', 'kept')")
        c.commit()
        c.close()
        start = time.time() + 2.0
        procs = [_spawn(_MIGRATOR, db, start) for _ in range(4)]
        counts = []
        for p in procs:
            stdout, stderr = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, stderr[-500:])
            counts.append(int(stdout.strip()))
        self.assertEqual(len(set(counts)), 1)
        c = sqlite3.connect(db)
        self.assertEqual(c.execute("SELECT title FROM jobs").fetchone()[0], "kept")  # history preserved
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
        self.assertTrue({"resume_sha", "eligibility_input", "review_artifacts"} <= cols)
        c.close()


class DailyRunTests(TempEnv):
    def test_stage_with_item_errors_is_not_success(self):
        """Audit reproduction: a score stage with row errors and status ok produced exit 0."""
        code = daily.run_daily(stages=["score"], check_model=lambda: (True, "ok"),
                               stage_fns={"score": lambda: {"status": "ok", "scored": 3, "errors": 2}})
        self.assertEqual(code, daily.EXIT_STAGE_FAILED)

    def test_interrupted_runs_are_reconciled(self):
        self.conn.execute("INSERT INTO pipeline_runs (run_id, kind, started_at, status) VALUES"
                          " ('old', 'daily', '2026-11-01T00:00:00+00:00', 'running')")
        self.conn.commit()
        self.assertEqual(daily.run_daily(stages=["dedupe"], stage_fns={"dedupe": lambda: {"status": "ok"}}),
                         daily.EXIT_OK)
        row = self.conn.execute("SELECT status, finished_at FROM pipeline_runs WHERE run_id = 'old'").fetchone()
        self.assertEqual(row[0], "interrupted")
        self.assertIsNotNone(row[1])

    def test_deadline_stops_inside_a_stage(self):
        self.add("https://careers.synthbank.test/jobs/1", application_url="https://careers.synthbank.test/jobs/1")
        calls = []

        def fetch(url, timeout):
            calls.append(url)
            return 200, url, "<h1>IT Support Specialist</h1>" + "x " * 300

        from applypilot import verify
        runctl.set_deadline(time.monotonic() - 1)
        self.addCleanup(runctl.clear)
        out = verify.run_verification(fetch=fetch)
        self.assertTrue(out["stopped_early"])
        self.assertEqual(calls, [])
        runctl.clear()
        code = daily.run_daily(stages=["verify"], stage_fns={"verify": lambda: {"status": "ok", "stopped_early": True}})
        self.assertEqual(code, daily.EXIT_STAGE_FAILED)

    def test_manual_run_and_daily_share_the_lock(self):
        holder = daily.RunLock(config.APP_DIR / daily.LOCK_NAME)
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        from typer.testing import CliRunner

        from applypilot.cli import app
        res = CliRunner().invoke(app, ["run", "dedupe"])
        self.assertEqual(res.exit_code, daily.EXIT_LOCKED, res.output)

    def test_plan_does_not_open_or_migrate_the_database(self):
        database.close_connection(self.db_path)
        self.db_path.unlink()
        self.assertEqual(daily.run_daily(plan_only=True), daily.EXIT_OK)
        self.assertFalse(self.db_path.exists())
        self.assertIn("dedupe", opsconfig.DAILY_STAGES)


if __name__ == "__main__":
    unittest.main()
