"""Evidence-based, conservative verification of discovered job listings.

A reachable job URL is not proof that the opening is active. Only an explicit
manual review can mark a job open or closed; unknown remains the default.
"""
import argparse
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit

from applypilot.database import get_connection

STATUSES = {"open", "closed", "needs_review"}


def ensure_verification_table(conn: sqlite3.Connection) -> None:
    """Create additive verification metadata without changing the jobs table."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_verifications (
            job_url TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'needs_review'
                CHECK (status IN ('open', 'closed', 'needs_review')),
            official_url TEXT,
            employer TEXT,
            geographic_eligibility TEXT NOT NULL DEFAULT 'unverified'
                CHECK (geographic_eligibility IN ('eligible', 'restricted', 'unverified')),
            notes TEXT NOT NULL DEFAULT '',
            checked_at TEXT NOT NULL,
            FOREIGN KEY(job_url) REFERENCES jobs(url)
        )
    """)
    conn.commit()


def mark_review(
    conn: sqlite3.Connection,
    job_url: str,
    status: str,
    *,
    official_url: str | None = None,
    employer: str | None = None,
    eligibility: str = "unverified",
    notes: str = "",
) -> None:
    """Persist a human-reviewed assessment; never infer eligibility from 'Remote'."""
    if status not in STATUSES:
        raise ValueError("status must be open, closed, or needs_review")
    if eligibility not in {"eligible", "restricted", "unverified"}:
        raise ValueError("eligibility must be eligible, restricted, or unverified")
    if official_url and urlsplit(official_url).scheme not in {"http", "https"}:
        raise ValueError("official_url must be HTTP(S)")
    if conn.execute("SELECT 1 FROM jobs WHERE url=?", (job_url,)).fetchone() is None:
        raise ValueError("Job URL not found in database")
    ensure_verification_table(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO job_verifications
            (job_url, status, official_url, employer, geographic_eligibility, notes, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_url) DO UPDATE SET
            status=excluded.status,
            official_url=excluded.official_url,
            employer=excluded.employer,
            geographic_eligibility=excluded.geographic_eligibility,
            notes=excluded.notes,
            checked_at=excluded.checked_at
    """, (job_url, status, official_url, employer, eligibility, notes, now))
    conn.commit()


def list_candidates(conn: sqlite3.Connection, *, min_score: int = 7, limit: int = 30):
    """Return highly scored, unapplied jobs; closed/restricted jobs shown separately.

    This is a review queue, not an approved auto-apply queue.
    """
    ensure_verification_table(conn)
    return conn.execute("""
        SELECT j.url, j.title, j.site, j.location, j.fit_score,
               COALESCE(v.status, 'needs_review') AS status,
               COALESCE(v.geographic_eligibility, 'unverified') AS eligibility,
               v.official_url, v.checked_at
        FROM jobs j LEFT JOIN job_verifications v ON v.job_url = j.url
        WHERE j.fit_score >= ? AND j.applied_at IS NULL
        ORDER BY
          CASE WHEN v.status='open' AND v.geographic_eligibility='eligible' THEN 0
               WHEN v.status='closed' OR v.geographic_eligibility='restricted' THEN 2
               ELSE 1 END,
          j.fit_score DESC, j.discovered_at DESC
        LIMIT ?
    """, (min_score, max(1, limit))).fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(description="Review ApplyPilot job verification status (no auto-apply).")
    parser.add_argument("--list", action="store_true", help="List high-scoring jobs for verification")
    parser.add_argument("--min-score", type=int, default=7)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--mark", choices=sorted(STATUSES), help="Record manually reviewed status")
    parser.add_argument("--url", help="Exact stored job URL to mark")
    parser.add_argument("--official-url", help="Official employer application page URL")
    parser.add_argument("--employer", help="Verified employer name")
    parser.add_argument("--eligibility", choices=["eligible", "restricted", "unverified"], default="unverified")
    parser.add_argument("--notes", default="", help="Evidence, requirements, or follow-up notes")
    args = parser.parse_args()

    if not args.list and not args.mark:
        parser.error("choose --list or --mark")
    if args.mark and not args.url:
        parser.error("--mark requires --url")
    conn = get_connection()
    if args.mark:
        mark_review(
            conn, args.url, args.mark, official_url=args.official_url,
            employer=args.employer, eligibility=args.eligibility, notes=args.notes,
        )
        print("Review saved. This does not submit an application.")
    if args.list:
        for job in list_candidates(conn, min_score=args.min_score, limit=args.limit):
            url, title, site, location, score, status, eligibility, official, checked = job
            print(f"{score}/10 | {title} | {site} | {location or 'Location unknown'}")
            print(f"    Status={status} | NC eligibility={eligibility} | Checked={checked or 'never'}")
            print(f"    Listing={url}")
            if official:
                print(f"    Official={official}")


if __name__ == "__main__":
    main()
