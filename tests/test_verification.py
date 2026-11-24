"""Offline tests for conservative job verification tracking."""
import sqlite3
import unittest

from applypilot.verification import ensure_verification_table, list_candidates, mark_review


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""
            CREATE TABLE jobs (
                url TEXT PRIMARY KEY, title TEXT, site TEXT, location TEXT,
                fit_score INTEGER, discovered_at TEXT, applied_at TEXT
            )
        """)
        self.conn.executemany(
            "INSERT INTO jobs(url,title,site,location,fit_score,discovered_at) VALUES(?,?,?,?,?,?)",
            [
                ("https://example.org/1", "IT Support", "Example", "Remote, USA", 8, "2026-11-23"),
                ("https://example.org/2", "Network Engineer", "Example", "NC", 7, "2026-11-23"),
            ],
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_unreviewed_stays_unverified(self):
        rows = list_candidates(self.conn)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][5], "needs_review")
        self.assertEqual(rows[0][6], "unverified")

    def test_mark_review_preserves_scoring(self):
        mark_review(self.conn, "https://example.org/2", "open",
                    official_url="https://example.org/jobs/2",
                    eligibility="eligible", notes="Reviewed official posting")
        rows = list_candidates(self.conn)
        self.assertEqual(rows[0][0], "https://example.org/2")
        self.assertEqual(rows[0][5], "open")
        self.assertEqual(rows[0][6], "eligible")
        self.assertEqual(self.conn.execute("SELECT fit_score FROM jobs WHERE url='https://example.org/2'").fetchone()[0], 7)

    def test_invalid_status_and_url_not_saved(self):
        with self.assertRaises(ValueError):
            mark_review(self.conn, "https://example.org/1", "active")
        with self.assertRaises(ValueError):
            mark_review(self.conn, "https://example.org/missing", "open")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM sqlite_master WHERE name='job_verifications'").fetchone()[0], 0)

    def test_restricted_or_closed_not_treated_as_eligible(self):
        ensure_verification_table(self.conn)
        mark_review(self.conn, "https://example.org/1", "closed", eligibility="restricted")
        rows = list_candidates(self.conn)
        self.assertEqual(rows[-1][0], "https://example.org/1")


if __name__ == "__main__":
    unittest.main()
