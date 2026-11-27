"""Deterministic eligibility classifier tests (synthetic jobs only)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from applypilot import database, eligibility
from applypilot.eligibility import EligibilityConfig, classify

CFG = EligibilityConfig(
    home_state="NC",
    tiers={
        "highest": ["Burlington", "Graham", "Mebane", "Whitsett"],
        "high": ["Greensboro"],
        "acceptable": ["High Point", "Durham", "Raleigh"],
    },
    other_nc="review",
)


def job(location="", desc="", title="IT Support Specialist", url="https://example.test/job/1"):
    return {"title": title, "location": location, "full_description": desc, "url": url,
            "application_url": None}


def status(**kw):
    return classify(job(**kw), CFG).status


class NorthCarolinaTests(unittest.TestCase):
    def test_commute_tiers(self):
        for loc, tier in [("Burlington, NC, USA", "highest"), ("Mebane, North Carolina", "highest"),
                          ("Greensboro, NC", "high"), ("Raleigh, NC, USA", "acceptable"),
                          ("High Point, NC", "acceptable"), ("RTP, North Carolina, US", "acceptable")]:
            with self.subTest(loc=loc):
                e = classify(job(location=loc), CFG)
                self.assertEqual((e.status, e.tier), ("eligible_nc", tier))

    def test_nc_and_north_carolina_equivalent(self):
        self.assertEqual(status(location="Graham, NC"), status(location="Graham, North Carolina"))

    def test_same_city_name_in_another_state_is_not_nc(self):
        self.assertEqual(status(location="Burlington, Vermont"), "ineligible")
        self.assertEqual(status(location="Burlington, MA"), "ineligible")
        self.assertEqual(status(location="Greensboro, GA"), "ineligible")

    def test_other_nc_city_routes_to_review_by_default(self):
        e = classify(job(location="Charlotte, NC, USA"), CFG)
        self.assertEqual(e.status, "unknown")
        self.assertIn("commute", e.reason)
        self.assertEqual(classify(job(location="Charlotte, NC"),
                                  EligibilityConfig(home_state="NC", tiers=CFG.tiers, other_nc="eligible")).status,
                         "eligible_nc")

    def test_hybrid_in_nc_tier(self):
        self.assertEqual(status(location="Hybrid - Durham, NC"), "eligible_nc")

    def test_remote_located_in_nc(self):
        self.assertEqual(status(location="North Carolina Remote Work"), "eligible_us_remote")


class RemoteTests(unittest.TestCase):
    def test_nationwide_us_remote(self):
        for loc in ("Remote, USA (Remote)", "Remote-US", "US - Remote", "United States (Remote)"):
            with self.subTest(loc=loc):
                self.assertEqual(status(location=loc), "eligible_us_remote")

    def test_remote_word_alone_is_not_eligible(self):
        self.assertEqual(status(location="Remote"), "unknown")
        self.assertEqual(status(location="", desc="This is a remote role. Great benefits."), "unknown")

    def test_plain_remote_with_us_wide_description(self):
        self.assertEqual(status(location="Remote", desc="You can work remotely from anywhere in the United States."),
                         "eligible_us_remote")

    def test_state_restricted_remote_without_nc(self):
        self.assertEqual(status(location="Texas Remote Work"), "ineligible")
        self.assertEqual(status(location="US, UT, Remote"), "ineligible")

    def test_truncated_state_list_goes_to_review(self):
        self.assertEqual(status(location="Colorado Remote Work, More..."), "restricted_remote")

    def test_description_state_list_including_nc(self):
        desc = ("This position is eligible for remote work in one of the following states: "
                "Georgia, North Carolina, South Carolina, Virginia.")
        self.assertEqual(status(location="Remote, USA", desc=desc), "eligible_us_remote")
        self.assertEqual(status(location="Colorado Remote Work, More...", desc=desc), "eligible_us_remote")

    def test_description_state_list_excluding_nc(self):
        desc = "**Eligible States for Remote Work**: Arizona, California, Hawaii, Nevada, Washington"
        e = classify(job(location="Remote, USA (Remote)", desc=desc), CFG)
        self.assertEqual(e.status, "ineligible")
        self.assertTrue(any("restriction" in ev for ev in e.evidence))

    def test_explicit_exclusion_overrides_generic_remote(self):
        desc = ("Fully remote, open to candidates anywhere in the United States. "
                "We are not able to hire candidates residing in North Carolina, Ohio or Alaska.")
        e = classify(job(location="Remote, USA", desc=desc), CFG)
        self.assertEqual(e.status, "ineligible")
        self.assertIn("excludes", e.reason)

    def test_exclusion_of_other_place_does_not_block(self):
        desc = "Remote anywhere in the United States. We are not able to hire candidates living in Puerto Rico."
        self.assertEqual(status(location="Remote, USA", desc=desc), "eligible_us_remote")

    def test_residency_requirement_other_state(self):
        self.assertEqual(status(location="Remote, USA", desc="Candidates must reside in MN."), "ineligible")
        self.assertEqual(status(location="Remote, USA",
                                desc="This remote position requires the candidate to reside in North Texas."),
                         "ineligible")

    def test_conditional_residence_boilerplate_is_not_a_restriction(self):
        desc = ("If you reside in the United States and require an accommodation in the recruiting process, "
                "you may contact us. Remote anywhere in the US.")
        self.assertEqual(status(location="Remote, USA", desc=desc), "eligible_us_remote")

    def test_conflicting_remote_and_onsite_goes_to_review(self):
        desc = "This is an onsite position at our Austin office."
        e = classify(job(location="Remote, USA (Remote)", desc=desc), CFG)
        self.assertEqual(e.status, "unknown")
        self.assertIn("conflict", e.reason)

    def test_foreign_remote(self):
        for loc in ("Vietnam, Remote", "UK, Remote", "Canada, Full Time Remote",
                    "REMOTE/TELETRAVAIL, ON, CAN"):
            with self.subTest(loc=loc):
                self.assertEqual(status(location=loc), "ineligible")

    def test_new_mexico_is_not_mexico(self):
        self.assertEqual(status(location="Albuquerque, New Mexico"), "ineligible")
        e = classify(job(location="Albuquerque, New Mexico"), CFG)
        self.assertIn("NM", e.reason)


class MultipleAndMissingLocationTests(unittest.TestCase):
    def test_multiple_locations_any_nc_tier_wins(self):
        self.assertEqual(status(location="Austin, TX; Raleigh, NC"), "eligible_nc")
        self.assertEqual(status(location="Austin, TX | Denver, CO"), "ineligible")

    def test_title_embedded_location(self):
        e = classify(job(title="Service Desk Engineer II (The Woodlands, TX or Remote)",
                         location="Remote, USA (Remote)"), CFG)
        self.assertEqual(e.status, "eligible_us_remote")
        self.assertEqual(status(title="Desktop Support - Greensboro, NC", location=""), "eligible_nc")

    def test_out_of_state_onsite(self):
        self.assertEqual(status(location="Frisco, TX, USA"), "ineligible")

    def test_missing_location_unknown(self):
        e = classify(job(location="", desc="Great team, great benefits."), CFG)
        self.assertEqual(e.status, "unknown")
        self.assertIn("no usable location", e.reason)

    def test_missing_location_recovered_from_workday_url(self):
        url = "https://acme.wd5.myworkdayjobs.com/External/job/United-States-of-America-Eagan-Minnesota/Tech_R1"
        self.assertEqual(classify(job(location="", url=url), CFG).status, "ineligible")
        url = "https://acme.wd1.myworkdayjobs.com/Careers/job/Warsaw---Poland/Engineer_R2"
        self.assertEqual(classify(job(location="", url=url), CFG).status, "ineligible")
        url = "https://acme.wd1.myworkdayjobs.com/Careers/job/Greensboro-North-Carolina/Tech_R3"
        self.assertEqual(classify(job(location="", url=url), CFG).status, "eligible_nc")

    def test_workday_office_with_nationwide_remote_description(self):
        url = "https://acme.wd5.myworkdayjobs.com/External/job/United-States-of-America-Frisco-Texas/Tech_R4"
        desc = "This role is remote and can be performed from anywhere in the United States."
        self.assertEqual(classify(job(location="", url=url, desc=desc), CFG).status, "eligible_us_remote")

    def test_missing_location_recovered_from_description_line(self):
        desc = "About the role\nLocation: Durham, NC\nResponsibilities..."
        self.assertEqual(status(location="", desc=desc), "eligible_nc")

    def test_us_marker_without_state(self):
        url = "https://acme.wd5.myworkdayjobs.com/External/job/United-States-of-America/Tech_R5"
        self.assertEqual(classify(job(location="", url=url), CFG).status, "unknown")

    def test_deterministic(self):
        j = job(location="Colorado Remote Work, More...", desc="Remote role.")
        self.assertEqual(classify(j, CFG), classify(j, CFG))


class ConfigTests(unittest.TestCase):
    def test_eligibility_section(self):
        cfg = eligibility.load_eligibility_config({"eligibility": {
            "home_state": "nc", "commute_tiers": {"highest": ["Mebane"]}, "other_nc": "ineligible"}})
        self.assertEqual((cfg.home_state, cfg.tiers["highest"], cfg.other_nc), ("NC", ["Mebane"], "ineligible"))

    def test_legacy_location_accept_is_backward_compatible(self):
        cfg = eligibility.load_eligibility_config({"location_accept": ["Burlington, NC", "Durham", "NC"]})
        self.assertEqual(cfg.tiers["acceptable"], ["Burlington", "Durham"])

    def test_apply_prompt_reads_current_config_keys(self):
        from applypilot.apply.prompt import _build_location_check, accepted_onsite_cities
        self.assertEqual(accepted_onsite_cities({"location_accept": ["Mebane", "Graham"]}), ["Mebane", "Graham"])
        self.assertEqual(accepted_onsite_cities({"location": {"accept_patterns": ["Durham"]}}), ["Durham"])
        self.assertEqual(accepted_onsite_cities({"eligibility": {"commute_tiers": {"highest": ["Whitsett"]}},
                                                 "location_accept": ["Ignored"]}), ["Whitsett"])
        text = _build_location_check({"personal": {"city": "Town", "province_state": "NC"}},
                                     {"location_accept": ["Mebane"]},
                                     {"eligibility_status": "eligible_nc", "eligibility_reason": "tier"})
        self.assertIn("Mebane", text)
        self.assertIn("Pre-screen result: eligible_nc", text)
        self.assertNotIn('"Remote" or "work from anywhere" -> ELIGIBLE', text)
        self.assertIn('"remote" alone does not prove eligibility', text)

    def test_home_state_from_profile(self):
        cfg = eligibility.load_eligibility_config({}, {"personal": {"province_state": "North Carolina"}})
        self.assertEqual(cfg.home_state, "NC")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "e.db"
        p = mock.patch.object(database, "DB_PATH", self.db_path)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(database.close_connection, self.db_path)
        self.conn = database.init_db(self.db_path)
        rows = [("https://e.test/1", "Raleigh, NC", 8), ("https://e.test/2", "Frisco, TX", 9),
                ("https://e.test/3", "", 5)]
        for url, loc, score in rows:
            self.conn.execute("INSERT INTO jobs (url, title, location, full_description, fit_score) "
                              "VALUES (?, 'Tech', ?, 'desc', ?)", (url, loc, score))
        self.conn.commit()

    def test_classify_jobs_stores_reason_evidence_version_and_preserves_data(self):
        counts = eligibility.classify_jobs(self.conn, CFG)
        self.assertEqual(counts, {"eligible_nc": 1, "ineligible": 1, "unknown": 1})
        row = self.conn.execute("SELECT * FROM jobs WHERE url = 'https://e.test/2'").fetchone()
        self.assertEqual(row["eligibility_status"], "ineligible")
        self.assertEqual(row["eligibility_version"], eligibility.CLASSIFIER_VERSION)
        self.assertIn("TX", row["eligibility_reason"])
        self.assertIn("Frisco", row["eligibility_evidence"])
        self.assertEqual(row["fit_score"], 9)  # historical score untouched
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 3)
        self.assertEqual(eligibility.classify_jobs(self.conn, CFG), {}, "already current: no work")

    def test_manual_decisions_survive_reclassification(self):
        eligibility.classify_jobs(self.conn, CFG)
        self.assertTrue(eligibility.set_manual_decision("https://e.test/3", approve=True, note="checked"))
        eligibility.classify_jobs(self.conn, CFG, force=True)
        row = self.conn.execute("SELECT * FROM jobs WHERE url = 'https://e.test/3'").fetchone()
        self.assertEqual(row["eligibility_status"], "eligible_manual")


if __name__ == "__main__":
    unittest.main()
