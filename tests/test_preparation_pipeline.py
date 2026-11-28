"""Synthetic tests for the preparation pipeline (fork Sections 2-14).

Discovery history, migrations, posting verification, deduplication, scoring traceability,
grounded resume / cover letter validation, screening answers, readiness + review, the daily
orchestrator, reports, and one end-to-end run through application preparation and a
simulated (no browser, no model) submission record.

Everything is synthetic: a temporary database and application directory, a synthetic
candidate ("Testy Applicant"), synthetic employers on reserved ``.test`` domains, a fake
LLM and a fake HTTP fetcher. No network, browser, real profile or real database is used.
"""
import atexit
import json
import os
import sqlite3
import tempfile
import types
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from applypilot import config, daily, database, dedupe, eligibility, opsconfig, readiness, reports, screening, verify
from applypilot.apply import chrome, launcher
from applypilot.dates import iso_date
from applypilot.scoring import candidate_evidence, cover_letter, documents, factcheck, scorer, tailor

atexit.unregister(chrome.cleanup_on_exit)

NOW = datetime.now(UTC)

RESUME = """Testy Applicant
IT Support Specialist
testy.applicant@example.org | (919) 555-0142

SUMMARY
IT support technician with 3 years of help desk experience supporting 250 users in Windows 11 and Microsoft 365
environments, with hands-on Active Directory and Intune administration and PowerShell scripting.

TECHNICAL SKILLS
Systems: Windows 11, Active Directory, Microsoft 365, Intune, Group Policy
Scripting: PowerShell
Service: ServiceNow, ticket triage, remote support

EXPERIENCE
Help Desk Technician, Synthetic Health Co
Jan 2022 - Present
- Resolved hardware, software and account tickets for 250 users across 3 sites using ServiceNow
- Managed Active Directory accounts, password resets and Group Policy for clinical staff
- Enrolled laptops in Intune and supported Microsoft 365 mailbox and Teams issues
- Wrote PowerShell scripts that automated new-user account creation
IT Intern, Synthetic County Schools
Jun 2020 - Dec 2021
- Imaged and deployed Windows 11 workstations for classrooms and staff
- Documented support procedures and answered walk-up help desk requests

PROJECTS
HomeLab Cluster
- Built a Proxmox home lab running Ubuntu servers to practice Active Directory and backups

EDUCATION
A.A.S. Information Technology, Synthetic Community College
"""

PROFILE = {
    "personal": {"full_name": "Testy Applicant", "email": "testy.applicant@example.org", "phone": "(919) 555-0142",
                 "city": "Raleigh"},
    "work_authorization": {"legally_authorized_to_work": "Yes", "require_sponsorship": "No"},
    "compensation": {"salary_expectation": "60000"},
    "experience": {"years_of_experience_total": "3", "target_role": "IT Support Specialist"},
    "skills_boundary": {"systems": ["Windows 11", "Active Directory", "Microsoft 365", "Intune"],
                        "scripting": ["PowerShell"]},
    "resume_facts": {"preserved_companies": ["Synthetic Health Co", "Synthetic County Schools"],
                     "preserved_school": "Synthetic Community College", "preserved_projects": ["HomeLab Cluster"],
                     "real_metrics": ["250 users", "3 sites"]},
}

GOOD_JSON = {
    "title": "IT Support Specialist",
    "summary": ("IT support technician with 3 years of help desk experience supporting 250 users in Windows 11 "
                "and Microsoft 365 environments, with Active Directory, Intune and PowerShell skills."),
    "skills": {"Systems": "Windows 11, Active Directory, Microsoft 365, Intune, Group Policy",
               "Scripting": "PowerShell", "Service": "ServiceNow, ticket triage, remote support"},
    "experience": [
        {"header": "Help Desk Technician, Synthetic Health Co", "subtitle": "Jan 2022 - Present",
         "bullets": ["Resolved hardware, software and account tickets for 250 users across 3 sites using ServiceNow",
                     "Managed Active Directory accounts, password resets and Group Policy for clinical staff",
                     "Enrolled laptops in Intune and supported Microsoft 365 mailbox issues",
                     "Wrote PowerShell scripts that automated new-user account creation"]},
        {"header": "IT Intern, Synthetic County Schools", "subtitle": "Jun 2020 - Dec 2021",
         "bullets": ["Imaged and deployed Windows 11 workstations for classrooms and staff",
                     "Documented support procedures and answered walk-up help desk requests"]},
    ],
    "projects": [{"header": "HomeLab Cluster",
                  "bullets": ["Built a Proxmox home lab running Ubuntu servers to practice Active Directory"]}],
    "education": "A.A.S. Information Technology, Synthetic Community College",
}


def fabricated_json():
    data = json.loads(json.dumps(GOOD_JSON))
    data["summary"] = ("CCNA certified engineer with 5 years of experience running Kubernetes clusters and "
                       "reducing downtime by 40% for 250 users.")
    data["skills"]["Cloud"] = "Kubernetes, Terraform"
    data["experience"][0]["subtitle"] = "Jan 2019 - Present"  # changed employment date
    data["experience"].append({"header": "HomeLab Cluster, Systems Engineer", "subtitle": "",
                               "bullets": ["Ran production workloads"]})
    return data


GOOD_LETTER = """Dear Hiring Manager,

I am applying for the IT Support Specialist role at Synthetic Bank. For the last few years I have worked as a
help desk technician at Synthetic Health Co, where I resolve hardware, software and account tickets for 250
users across 3 sites using ServiceNow, manage Active Directory accounts and Group Policy, and enroll laptops in
Intune. I also wrote PowerShell scripts that automated new-user account creation, which shortened onboarding
for new staff.

Your posting asks for Windows 11 and Microsoft 365 support and clear communication with non-technical users.
That is most of my week: I support Microsoft 365 mailbox issues, image and deploy Windows 11 workstations,
and write short procedures so colleagues can solve common problems themselves.

I would welcome the chance to talk about how I can help the Synthetic Bank service desk. Thank you for your
time and consideration.

Sincerely,
Testy Applicant
"""



def _fake_pdf(p, kind=None, **_kw):
    """Stand-in renderer: a deterministic 'PDF' derived from the exact text (real rendering is
    exercised by RenderSafetyTests)."""
    import hashlib
    src = Path(p)
    out = src.with_suffix(".pdf")
    out.write_bytes(b"%PDF-fake " + hashlib.sha256(src.read_bytes()).hexdigest().encode())
    return out

class FakeLLM:
    """Answers by prompt type. ``judge``: PASS/FAIL; ``tailor``: dict or callable; ``score``: text."""

    model = "synthetic/fake-14b"

    def __init__(self, tailor=GOOD_JSON, judge="PASS", letter=GOOD_LETTER, score="SCORE: 8\nKEYWORDS: windows\n"
                 "REASONING: Strong help desk match."):
        self.tailor, self.judge, self.letter, self.score = tailor, judge, letter, score
        self.calls: list[str] = []

    def chat(self, messages, **kw):
        system = messages[0]["content"].lower()
        if "job fit evaluator" in system:
            self.calls.append("score")
            return self.score
        if "resume quality judge" in system:
            self.calls.append("judge")
            return f"VERDICT: {self.judge}\nISSUES: {'none' if self.judge == 'PASS' else 'invented CCNA'}"
        if "cover letter" in system:
            self.calls.append("cover")
            return self.letter
        self.calls.append("tailor")
        data = self.tailor() if callable(self.tailor) else self.tailor
        return json.dumps(data)


class TempEnv(unittest.TestCase):
    """Temporary APP_DIR + database + synthetic profile/resume. Nothing real is touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.app = Path(self._tmp.name)
        self.db_path = self.app / "applypilot.db"
        (self.app / "resume.txt").write_text(RESUME, encoding="utf-8")
        self.patches = [
            mock.patch.object(database, "DB_PATH", self.db_path),
            mock.patch.object(config, "APP_DIR", self.app),
            mock.patch.object(config, "LOG_DIR", self.app / "logs"),
            mock.patch.object(config, "RESUME_PATH", self.app / "resume.txt"),
            mock.patch.object(config, "load_profile", return_value=PROFILE),
            mock.patch.object(candidate_evidence, "EVIDENCE_PATH", self.app / "candidate_evidence.yaml"),
            mock.patch.object(tailor, "RESUME_PATH", self.app / "resume.txt"),
            mock.patch.object(tailor, "TAILORED_DIR", self.app / "tailored_resumes"),
            mock.patch.object(tailor, "load_profile", return_value=PROFILE),
            mock.patch.object(cover_letter, "RESUME_PATH", self.app / "resume.txt"),
            mock.patch.object(cover_letter, "COVER_LETTER_DIR", self.app / "cover_letters"),
            mock.patch.object(cover_letter, "load_profile", return_value=PROFILE),
            mock.patch.object(scorer, "RESUME_PATH", self.app / "resume.txt"),
            mock.patch("applypilot.scoring.pdf.convert_to_pdf", side_effect=_fake_pdf),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(database.close_connection, self.db_path)
        self.conn = database.init_db(self.db_path)
        db_file = self.conn.execute("PRAGMA database_list").fetchone()[2]
        self.assertTrue(Path(db_file).resolve().is_relative_to(self.app.resolve()))
        (self.app / "pipeline.yaml").write_text("verify_delay_s: 0\nmin_score: 7\n", encoding="utf-8")

    def llm(self, fake):
        for mod in (scorer, tailor, cover_letter):
            p = mock.patch.object(mod, "get_client", return_value=fake)
            p.start()
            self.addCleanup(p.stop)
        return fake

    def add(self, url, **cols):
        cols = {"title": "IT Support Specialist", "site": "Synthetic Bank", "company": "Synthetic Bank",
                "location": "Raleigh, NC", "discovered_at": NOW.isoformat(), "first_seen_at": NOW.isoformat(),
                "full_description": "Support Windows 11 and Microsoft 365 users. " * 20, **cols}
        keys = ", ".join(["url", *cols])
        self.conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({', '.join('?' * (len(cols) + 1))})",
                          (url, *cols.values()))
        self.conn.commit()

    def row(self, url):
        return dict(self.conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone())


# --- discovery history + migrations -----------------------------------------------------------------

class DiscoveryHistoryTests(TempEnv):
    def test_sightings_are_recorded_incrementally(self):
        jobs = [{"url": "https://boards.test/j/1", "title": "IT Support", "company": "Synthetic Bank",
                 "date_posted": "2026-11-20", "job_type": "fulltime"}]
        self.assertEqual(database.store_jobs(self.conn, jobs, "BoardA", "test"), (1, 0))
        self.assertEqual(database.store_jobs(self.conn, jobs, "BoardB", "test"), (0, 1))
        r = self.row("https://boards.test/j/1")
        self.assertEqual((r["seen_count"], r["company"], r["date_posted"], r["job_type"]),
                         (2, "Synthetic Bank", "2026-11-20", "fulltime"))
        self.assertIsNotNone(r["first_seen_at"])
        sources = {s[0] for s in self.conn.execute("SELECT source FROM job_sources WHERE url = ?", (r["url"],))}
        self.assertEqual(sources, {"BoardA", "BoardB"})

    def test_relative_dates_are_never_stored(self):
        for v in ("Posted 3 days ago", "30+ days", "", None, "yesterday"):
            self.assertIsNone(iso_date(v))
        self.assertEqual(iso_date("2026-11-02T10:00:00Z"), "2026-11-02")

    def test_migration_adds_columns_without_losing_data(self):
        old = self.app / "old.db"
        c = sqlite3.connect(old)
        c.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT, fit_score INTEGER)")
        c.execute("INSERT INTO jobs VALUES ('https://x.test/1', 'Old', 9)")
        c.commit()
        c.close()
        conn = database.init_db(old)
        self.addCleanup(database.close_connection, old)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        self.assertTrue({"posting_status", "duplicate_of", "readiness", "review_status", "score_model"} <= cols)
        self.assertEqual(tuple(conn.execute("SELECT title, fit_score FROM jobs").fetchone()), ("Old", 9))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"job_sources", "discovery_runs", "pipeline_runs", "score_history"} <= tables)


# --- posting verification ------------------------------------------------------------------------------

def fetcher(pages: dict):
    def fetch(url, timeout):
        if url.endswith("/robots.txt"):
            return pages.get("robots", (404, url, ""))
        v = pages.get(url)
        if isinstance(v, Exception):
            raise v
        return v or (404, url, "not found")
    return fetch


class VerifyTests(TempEnv):
    JOB = types.MappingProxyType({"url": "https://boards.test/j/1",
                                  "application_url": "https://careers.synthbank.test/jobs/77",
                                  "title": "IT Support Specialist"})

    def check(self, page, robots=None):
        pages = {"https://careers.synthbank.test/jobs/77": page}
        if robots:
            pages["robots"] = (200, "", robots)
        f = fetcher(pages)
        return verify.check_posting(dict(self.JOB), fetch=f, robots=verify.RobotsCache(f, 5))

    def test_outcomes(self):
        ok = (200, "u", "<h1>IT Support Specialist</h1><p>Apply now</p>" + "x " * 400)
        self.assertEqual(self.check(ok).status, "active")
        self.assertEqual(self.check((404, "u", "")).status, "removed")
        self.assertEqual(self.check((200, "u", "<p>This position has been filled.</p>")).status, "closed")
        self.assertEqual(self.check((403, "u", "")).status, "inaccessible")
        self.assertEqual(self.check((200, "u", "<p>Verify you are human</p>")).status, "inaccessible")
        generic = (200, "u", "<h1>Careers at Synthetic Bank</h1>" + "Search our openings. " * 60)
        self.assertEqual(self.check(generic).status, "uncertain")
        self.assertEqual(self.check(TimeoutError()).status, "inaccessible")
        blocked = self.check(ok, robots="User-agent: *\nDisallow: /jobs/")
        self.assertEqual((blocked.status, blocked.reason), ("inaccessible", "robots.txt disallows automated checks"))

    def test_official_url_preferred(self):
        self.assertEqual(verify.preferred_url(dict(self.JOB)), "https://careers.synthbank.test/jobs/77")
        board = {"url": "https://boards.test/j/1", "application_url": "https://www.indeed.com/apply/1"}
        self.assertEqual(verify.preferred_url(board), "https://boards.test/j/1")

    def test_freshness(self):
        cfg = opsconfig.load()
        base = {"posting_status": "active", "posting_checked_at": NOW.isoformat()}
        self.assertTrue(verify.is_fresh({**base, "date_posted": (NOW - timedelta(days=5)).date().isoformat()}, cfg)[0])
        self.assertFalse(verify.is_fresh({**base, "date_posted": (NOW - timedelta(days=45)).date().isoformat()}, cfg)[0])
        ok, why = verify.is_fresh({**base, "first_seen_at": NOW.isoformat()}, cfg)
        self.assertTrue(ok)
        self.assertIn("posting date unknown", why)
        stale = {**base, "posting_checked_at": (NOW - timedelta(days=5)).isoformat(), "first_seen_at": NOW.isoformat()}
        self.assertFalse(verify.is_fresh(stale, cfg)[0])
        self.assertFalse(verify.is_fresh({"posting_status": "uncertain"}, cfg)[0])

    def test_stage_persists_results(self):
        self.add("https://boards.test/j/1", application_url="https://careers.synthbank.test/jobs/77")
        f = fetcher({"https://careers.synthbank.test/jobs/77": (200, "u", "<h1>IT Support Specialist</h1>" + "y " * 300)})
        out = verify.run_verification(fetch=f)
        self.assertEqual(out["active"], 1)
        self.assertEqual(self.row("https://boards.test/j/1")["posting_status"], "active")


# --- duplicates ------------------------------------------------------------------------------------------

class DedupeTests(TempEnv):
    def test_workday_requisitions_in_urls_keep_openings_apart(self):
        base = "https://synth.wd5.myworkdayjobs.test/External/job/US-Raleigh/Implementation-Consultant_"
        a = {"url": base + "JREQ204876", "company": "Synth", "title": "Implementation Consultant",
             "location": "Raleigh, NC", "full_description": "same text " * 50, "strategy": "workday_api", "site": "x"}
        b = {**a, "url": base + "JREQ203197", "strategy": "jobspy", "site": "indeed"}
        self.assertEqual(dedupe.requisition_id(a), "JREQ204876")
        self.assertIsNone(dedupe.same_opening(a, b))
        self.assertIsNone(dedupe.requisition_id({"url": "https://www.indeed.com/viewjob?jk=12345678"}))

    def test_canonical_urls(self):

        a = dedupe.canonical_url("https://job-boards.greenhouse.io/acme/jobs/123?gh_src=abc&utm_source=x")
        b = dedupe.canonical_url("https://boards.greenhouse.io/acme/jobs/123/apply")
        self.assertEqual(a, b)

    def test_rules(self):
        desc = "Provide desktop support for Windows 11 users, manage Active Directory and Intune. " * 8
        a = {"url": "u1", "company": "Synthetic Bank, Inc.", "title": "Sr. IT Support Specialist (Remote)",
             "location": "Raleigh, NC", "full_description": desc, "strategy": "jobspy", "site": "indeed"}
        b = {"url": "u2", "company": "synthetic bank", "title": "Senior IT Support Specialist",
             "location": "Raleigh, NC", "full_description": desc, "strategy": "workday_api", "site": "Synthetic Bank"}
        self.assertIsNotNone(dedupe.same_opening(a, b))
        # same source, same title, different URLs: several openings, not a duplicate (real-data regression)
        self.assertIsNone(dedupe.same_opening(a, {**b, "strategy": "jobspy", "site": "indeed"}))
        # unknown location never matches on similarity
        self.assertIsNone(dedupe.same_opening({**a, "location": None}, {**b, "location": None}))
        # same title, different openings (different requisition IDs) are never merged
        self.assertIsNone(dedupe.same_opening({**a, "requisition_id": "R-1001"}, {**b, "requisition_id": "R-1002"}))
        # same title without comparable descriptions: not merged
        self.assertIsNone(dedupe.same_opening({**a, "full_description": None}, {**b, "full_description": None}))
        # different location: not merged
        self.assertIsNone(dedupe.same_opening(a, {**b, "location": "Durham, NC"}))
        self.assertEqual(dedupe.same_opening({**a, "requisition_id": "R-1001"}, {**b, "requisition_id": "r_1001"}),
                         "same employer and requisition ID")

    def test_run_keeps_rows_and_prefers_official_posting(self):
        desc = "Provide desktop support for Windows 11 users. " * 10
        self.add("https://www.indeed.com/viewjob?jk=9", application_url="https://www.indeed.com/viewjob?jk=9",
                 full_description=desc, site="indeed", strategy="jobspy")
        self.add("https://careers.synthbank.test/jobs/77", application_url="https://careers.synthbank.test/jobs/77",
                 full_description=desc)
        self.add("https://careers.synthbank.test/jobs/78", title="Network Engineer", full_description="other " * 50)
        out = dedupe.run_dedupe(self.conn)
        self.assertEqual(out["duplicates"], 1)
        self.assertEqual(self.row("https://www.indeed.com/viewjob?jk=9")["duplicate_of"],
                         "https://careers.synthbank.test/jobs/77")
        self.assertIsNone(self.row("https://careers.synthbank.test/jobs/77")["duplicate_of"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 3)


# --- grounding / documents --------------------------------------------------------------------------------

class FactcheckTests(unittest.TestCase):
    def sources(self, evidence=None):
        return factcheck.build_sources(RESUME, PROFILE, evidence or {})

    def test_grounded_resume_passes(self):
        text = tailor.assemble_resume_text(GOOD_JSON, PROFILE)
        self.assertEqual(factcheck.check_claims(text, self.sources()), [])
        self.assertEqual(factcheck.ats_problems(text), [])

    def test_fabrications_are_caught(self):
        text = tailor.assemble_resume_text(fabricated_json(), PROFILE)
        problems = " | ".join(factcheck.check_claims(text, self.sources()))
        for expected in ("kubernetes", "terraform", "ccna", "40%", "5 years", "jan 2019", "HomeLab Cluster"):
            self.assertIn(expected.lower(), problems.lower(), expected)

    def test_certifications_in_progress_must_be_stated(self):
        ev = {"certifications": ["CompTIA A+ (in progress, exam scheduled)"]}
        claim = "SUMMARY\nHelp desk technician. A+ certified.\n"
        self.assertTrue(any("a+" in p for p in factcheck.check_claims(claim, self.sources(ev), kind="cover")))
        honest = "SUMMARY\nHelp desk technician pursuing the CompTIA A+ certification.\n"
        self.assertFalse(any("a+" in p for p in factcheck.check_claims(honest, self.sources(ev), kind="cover")))

    def test_cover_letter_checks(self):
        job = {"title": "IT Support Specialist", "company": "Synthetic Bank"}
        self.assertEqual(factcheck.cover_letter_problems(GOOD_LETTER, job, self.sources()), [])
        bad = GOOD_LETTER.replace("Thank you", "I guarantee 100% uptime. Thank you")
        self.assertTrue(any("promise" in p for p in factcheck.cover_letter_problems(bad, job, self.sources())))
        other = factcheck.cover_letter_problems(GOOD_LETTER, {**job, "company": "Other Corp"}, self.sources())
        self.assertTrue(any("employer" in p for p in other))


class TailorTests(TempEnv):
    def job(self, url="https://careers.synthbank.test/jobs/77"):
        self.add(url, application_url=url, fit_score=8, posting_status="active", posting_checked_at=NOW.isoformat(),
                 posting_verified_url=url,
                 eligibility_status="eligible_manual", eligibility_version=eligibility.MANUAL_VERSION)
        eligibility.set_manual_decision(url, True, "synthetic", conn=self.conn)  # binds the decision to inputs
        return url

    def test_judge_rejected_resume_never_becomes_ready(self):
        url = self.job()
        self.llm(FakeLLM(judge="FAIL"))
        out = tailor.run_tailoring(min_score=7)
        self.assertEqual(out["approved"], 0)
        r = self.row(url)
        self.assertIsNone(r["tailored_resume_path"])
        self.assertEqual(r["resume_validation"], "needs_review")
        self.assertIn("rejected", r["resume_rejected_path"])
        self.assertTrue(Path(r["resume_rejected_path"]).exists())
        self.assertEqual(list((self.app / "tailored_resumes").glob("*.txt")), [])  # nothing approved on disk

    def test_fabricated_resume_rejected_even_in_lenient_mode(self):
        url = self.job()
        self.llm(FakeLLM(tailor=fabricated_json))
        tailor.run_tailoring(min_score=7, validation_mode="lenient")
        r = self.row(url)
        self.assertIsNone(r["tailored_resume_path"])
        self.assertEqual(r["resume_validation"], "rejected")
        reasons = json.loads(r["resume_validation_reasons"])
        self.assertEqual(reasons[0], "rejected_unsupported_claims")

    def test_grounded_resume_approved_with_traceability(self):
        url = self.job()
        self.llm(FakeLLM())
        self.assertEqual(tailor.run_tailoring(min_score=7)["approved"], 1)
        r = self.row(url)
        self.assertEqual((r["resume_validation"], r["resume_model"]), ("passed", FakeLLM.model))
        self.assertTrue(Path(r["tailored_resume_path"]).exists())
        self.assertEqual(r["documents_for"], documents.fingerprint(r, PROFILE))

    def test_rejected_cover_letter_is_never_marked_ready(self):
        url = self.job()
        self.llm(FakeLLM(letter=GOOD_LETTER.replace("Thank you", "I guarantee 100% uptime. Thank you")))
        tailor.run_tailoring(min_score=7)
        cover_letter.run_cover_letters(min_score=7)
        r = self.row(url)
        self.assertIsNone(r["cover_letter_path"])
        self.assertEqual(r["cover_validation"], "rejected")
        self.assertTrue(Path(r["cover_rejected_path"]).exists())


class ScoringTests(TempEnv):
    def test_model_and_config_recorded_and_failures_resumable(self):
        self.add("https://x.test/1", eligibility_status="eligible_manual", eligibility_version="manual")
        fake = self.llm(FakeLLM(score="I think this is a good fit"))  # malformed: no SCORE line
        scorer.run_scoring()
        r = self.row("https://x.test/1")
        self.assertIsNone(r["fit_score"])
        self.assertEqual(r["score_attempts"], 1)
        self.assertIn("SCORE", r["score_error"])
        fake.score = "SCORE: 8\nKEYWORDS: windows\nREASONING: Good."
        scorer.run_scoring()
        r = self.row("https://x.test/1")
        self.assertEqual((r["fit_score"], r["score_model"]), (8, FakeLLM.model))
        self.assertEqual(json.loads(r["score_config"])["prompt_version"], scorer.SCORE_PROMPT_VERSION)
        self.assertIsNone(r["score_error"])
        scorer.run_scoring(rescore=True)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM score_history").fetchone()[0], 1)


# --- screening ------------------------------------------------------------------------------------------------

class ScreeningTests(TempEnv):
    def write_bank(self):
        (self.app / "screening_answers.yaml").write_text(
            "answers:\n"
            "  - id: work_authorization\n    patterns: ['authori[sz]ed to work']\n    answer: 'Yes'\n    reusable: true\n"
            "  - id: salary\n    patterns: ['salary']\n    answer: '60000'\n    reusable: false\n"
            "  - id: eeo_gender\n    patterns: ['gender']\n    answer: 'Decline to self-identify'\n    reusable: true\n",
            encoding="utf-8")

    def test_decisions(self):
        self.write_bank()
        bank = screening.load_bank()
        self.assertEqual(screening.decide("Are you legally authorized to work in the US?", bank).answer, "Yes")
        self.assertEqual(screening.decide("What are your salary expectations?", bank).status, "needs_review")
        self.assertEqual(screening.decide("Do you have experience with Kubernetes?", bank).status, "needs_review")
        # sensitive categories need the candidate even with an answer, unless explicitly authorized
        self.assertEqual(screening.decide("What is your gender?", bank).status, "sensitive")
        self.assertEqual(screening.decide("Have you ever been convicted of a felony?", bank).status, "sensitive")
        self.assertEqual(screening.decide("I certify that the information is true", bank).status, "sensitive")
        cov = screening.coverage(bank)
        self.assertIn("work_authorization", cov["covered"])
        self.assertIn("salary", cov["missing"])

    def test_agent_prompt_has_no_inferred_answers(self):
        self.write_bank()
        text = screening.agent_section()
        self.assertIn("Yes", text)
        self.assertNotIn("60000", text)          # not authorized for reuse
        self.assertIn("needs_human_answer", text)
        from applypilot.apply import prompt
        full = prompt._build_profile_summary(PROFILE) + prompt._build_salary_section(PROFILE)
        for invented in ("Felony: No", "Background Check: Yes", "Age 18+", "Gender:", "MIDPOINT"):
            self.assertNotIn(invented, full)


# --- readiness + review ----------------------------------------------------------------------------------------

class ReadinessTests(TailorTests):
    def prepared(self):
        url = self.job()
        self.llm(FakeLLM())
        tailor.run_tailoring(min_score=7)
        cover_letter.run_cover_letters(min_score=7)
        return url

    def test_ready_then_blocked_by_each_condition(self):
        url = self.prepared()
        readiness.run_prepare(self.conn)
        self.assertEqual(self.row(url)["readiness"], "ready", self.row(url)["readiness_reasons"])
        checks = {
            "posting_status = 'closed'": "posting not verified active",
            "fit_score = 5": "fit score 5 below 7",
            "eligibility_status = 'review'": "eligibility not verified",
            "duplicate_of = 'https://other.test/1'": "duplicate of",
            "apply_status = 'needs_review'": "application history",
        }
        for change, reason in checks.items():
            with self.subTest(change=change):
                snapshot = self.row(url)
                self.conn.execute(f"UPDATE jobs SET {change} WHERE url = ?", (url,))
                ok, reasons, _ = readiness.evaluate(self.row(url), self.conn)
                self.assertFalse(ok)
                self.assertTrue(any(reason in r for r in reasons), reasons)
                cols = change.split(" = ")[0]
                self.conn.execute(f"UPDATE jobs SET {cols} = ? WHERE url = ?", (snapshot[cols], url))

    def test_resume_change_makes_documents_stale(self):
        url = self.prepared()
        (self.app / "resume.txt").write_text(RESUME + "\nNew line\n", encoding="utf-8")
        readiness.run_prepare(self.conn)
        r = self.row(url)
        self.assertEqual((r["readiness"], r["resume_validation"]), ("blocked", "stale"))
        self.assertIsNone(r["tailored_resume_path"])

    def test_review_workflow(self):
        url = self.prepared()
        readiness.run_prepare(self.conn)
        ok, _ = readiness.review(url, "approve", "looks right", self.conn)
        self.assertTrue(ok)
        self.assertEqual(self.row(url)["review_status"], "approved")
        self.conn.execute("UPDATE jobs SET posting_status = 'closed' WHERE url = ?", (url,))
        readiness.run_prepare(self.conn)
        self.assertIsNone(self.row(url)["review_status"])     # approval withdrawn once not ready
        ok, msg = readiness.review(url, "approve", conn=self.conn)
        self.assertFalse(ok)
        self.assertIn("not ready", msg)
        self.assertTrue(readiness.review(url, "manual_submitted", "applied on site", self.conn)[0])
        r = self.row(url)
        self.assertEqual((r["apply_status"], r["verification_confidence"]), ("applied", "human_reported"))


# --- daily orchestration ------------------------------------------------------------------------------------------

class DailyTests(TempEnv):
    def test_overlap_prevented(self):
        (self.app / daily.LOCK_NAME).write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        self.assertEqual(daily.run_daily(stage_fns={}, stages=["dedupe"]), daily.EXIT_LOCKED)

    def test_stale_lock_taken_over(self):
        (self.app / daily.LOCK_NAME).write_text(json.dumps({"pid": 999999999}), encoding="utf-8")
        code = daily.run_daily(stages=["dedupe"], stage_fns={"dedupe": lambda: {"status": "ok"}})
        self.assertEqual(code, daily.EXIT_OK)
        self.assertFalse((self.app / daily.LOCK_NAME).exists())

    def test_model_unavailable_skips_llm_stages_and_failures_do_not_stop_the_run(self):
        ran = []
        fns = {s: (lambda s=s: ran.append(s) or {"status": "ok"}) for s in opsconfig.DAILY_STAGES}
        def boom() -> dict:
            raise RuntimeError("synthetic failure")
        fns["verify"] = boom  # pyright: ignore[reportArgumentType]
        code = daily.run_daily(stage_fns=fns, check_model=lambda: (False, "server down"))
        self.assertEqual(code, daily.EXIT_STAGE_FAILED)
        self.assertNotIn("score", ran)
        self.assertIn("prepare", ran)          # later stages still ran
        log = (self.app / "logs").glob("daily-*.jsonl")
        events = [json.loads(line)["event"] for p in log for line in p.read_text().splitlines()]
        self.assertIn("stage_skipped", events)
        run = dict(self.conn.execute("SELECT * FROM pipeline_runs").fetchone())
        self.assertEqual(run["exit_code"], daily.EXIT_STAGE_FAILED)

    def test_apply_is_never_a_daily_stage(self):
        (self.app / "pipeline.yaml").write_text("daily:\n  stages: [apply, dedupe]\n", encoding="utf-8")
        self.assertEqual(opsconfig.load()["daily"]["stages"], ["dedupe"])
        self.assertNotIn("apply", opsconfig.DAILY_STAGES)


# --- reports -------------------------------------------------------------------------------------------------------

class ReportTests(TempEnv):
    def test_reports_filter_sort_and_export_safely(self):
        self.add("https://a.test/1", fit_score=9, title="=HYPERLINK(\"x\")")
        self.add("https://a.test/2", fit_score=7)
        rows = reports.run_report("top", min_score=8)
        self.assertEqual([r["url"] for r in rows], ["https://a.test/1"])
        rows = reports.run_report("top", sort="fit_score", desc=False)
        self.assertEqual(rows[0]["fit_score"], 7)
        csv_text = reports.to_csv(reports.run_report("top"))
        self.assertIn("'=HYPERLINK", csv_text)
        self.assertNotIn("testy.applicant", csv_text)
        self.assertIn("scoring", reports.inference_report())


# --- end to end ------------------------------------------------------------------------------------------------------

class EndToEndTests(TempEnv):
    """discover -> enrich -> verify -> dedupe -> classify -> score -> tailor -> cover -> prepare -> review ->
    claim -> simulated submission record -> duplicate prevention -> crash recovery."""

    def test_full_preparation_and_supervised_submission_record(self):
        official = "https://careers.synthbank.test/jobs/77"
        board = "https://www.indeed.com/viewjob?jk=77"  # a known board host: never canonical
        desc = ("Synthetic Bank seeks an IT Support Specialist to support Windows 11 and Microsoft 365 users, "
                "manage Active Directory accounts and enroll devices in Intune. ") * 6
        # discover (two sources, same opening)
        database.store_jobs(self.conn, [{"url": board, "title": "IT Support Specialist", "company": "Synthetic Bank",
                                         "location": "Raleigh, NC"}], "BoardA", "test")
        database.store_jobs(self.conn, [{"url": official, "title": "IT Support Specialist",
                                         "company": "Synthetic Bank", "location": "Raleigh, NC"}], "SynthBank", "test")
        # enrich (synthetic)
        self.conn.execute("UPDATE jobs SET full_description = ?, detail_scraped_at = ?, application_url = url",
                          (desc, NOW.isoformat()))
        self.conn.commit()
        # verify
        page = (200, official, "<h1>IT Support Specialist</h1>" + desc)
        f = fetcher({official: page, board: page})
        verify.run_verification(fetch=f)
        # dedupe
        self.assertEqual(dedupe.run_dedupe(self.conn)["duplicates"], 1)
        self.assertEqual(self.row(board)["duplicate_of"], official)
        # classify (human decision on the synthetic job; the classifier itself is tested elsewhere)
        eligibility.set_manual_decision(official, approve=True, note="synthetic", conn=self.conn)
        # score / tailor / cover with the fake model
        fake = self.llm(FakeLLM())
        scorer.run_scoring()
        self.assertIsNone(self.row(board)["fit_score"])  # duplicates are not scored
        tailor.run_tailoring(min_score=7)
        cover_letter.run_cover_letters(min_score=7)
        self.assertEqual(fake.calls.count("score"), 1)
        # prepare + review
        out = readiness.run_prepare(self.conn)
        self.assertEqual(out["ready"], 1, self.row(official)["readiness_reasons"])
        self.assertIsNone(launcher.acquire_job(target_url=official))          # not approved yet
        self.assertTrue(readiness.review(official, "approve", "checked", self.conn)[0])
        # claim for a (real-mode) supervised run
        job = launcher.acquire_job(target_url=official)
        assert job is not None
        self.assertEqual(launcher._still_ready(job), (True, []))
        # simulated browser session: approved submit -> launcher-confirmed release -> confirmation
        result = launcher.classify_session("RESULT:APPLIED", final_is_error=False, dry_run=False, submit_attempts=1,
                                           unblocked_submits=1, submit_approved=True, confirmation_seen=True,
                                           released_writes=1, evidence_correlated=True)
        self.assertEqual(launcher.record_outcome(official, result, dry_run=False, owner=job["agent_id"]), "applied")
        self.assertIsNotNone(self.row(official)["applied_at"])
        # no second application: not the same row, not its duplicate
        self.assertIsNone(launcher.acquire_job(target_url=official))
        ok, reasons, _ = readiness.evaluate(self.row(board), self.conn)
        self.assertFalse(ok)
        self.assertTrue(any("duplicate" in r for r in reasons))

    def test_uncertain_and_crashed_sessions_go_to_review_never_retry(self):
        url = "https://careers.synthbank.test/jobs/88"
        self.add(url, application_url=url, fit_score=8, tailored_resume_path=str(self.app / "r.txt"),
                 eligibility_status="eligible_manual", eligibility_version="manual", readiness="ready",
                 review_status="approved")
        job = launcher.acquire_job(target_url=url)
        assert job is not None
        # a released write followed by an ordinary failure is uncertain, not retryable
        res = launcher.classify_session("RESULT:FAILED:page_error", final_is_error=False, dry_run=False,
                                        submit_attempts=0, unblocked_submits=0, released_writes=1)
        self.assertEqual(launcher.record_outcome(url, res, dry_run=False, owner=job["agent_id"]), "needs_review")
        self.assertIsNone(launcher.acquire_job(target_url=url))
        # crash recovery: a stale claim becomes needs_review
        url2 = "https://careers.synthbank.test/jobs/89"
        old = (NOW - timedelta(days=2)).isoformat()
        self.add(url2, apply_status="in_progress", agent_id="dead-launcher:worker-0", last_attempted_at=old)
        self.assertGreaterEqual(launcher.recover_stale_locks(), 1)
        self.assertEqual(self.row(url2)["apply_status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
