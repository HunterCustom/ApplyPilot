"""ApplyPilot database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import sqlite3
import threading
import time
from datetime import UTC, datetime, timezone
from pathlib import Path

from applypilot.config import DB_PATH

# Thread-local connection storage — each thread gets its own connection
# (required for SQLite thread safety with parallel workers)
_local = threading.local()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local cached SQLite connection with WAL mode enabled.

    Each thread gets its own connection (required for SQLite thread safety).
    Connections are cached and reused within the same thread.

    Args:
        db_path: Override the default DB_PATH. Useful for testing.

    Returns:
        sqlite3.Connection configured with WAL mode and row factory.
    """
    path = str(db_path or DB_PATH)

    if not hasattr(_local, 'connections'):
        _local.connections = {}

    conn = _local.connections.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass

    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA busy_timeout=10000")
    # Switching a database to WAL needs an exclusive lock and can fail immediately (no busy wait)
    # while another process opens it at the same time; retry for a bounded time (audit R5).
    deadline = time.monotonic() + 30
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    conn.row_factory = sqlite3.Row
    _local.connections[path] = conn
    return conn


def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(db_path or DB_PATH)
    if hasattr(_local, 'connections'):
        conn = _local.connections.pop(path, None)
        if conn is not None:
            conn.close()


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, salary, description, location, site, strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error
      - Scoring:    fit_score, score_reasoning, scored_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   agent_id, last_attempted_at, apply_duration_ms, apply_task_id,
                   verification_confidence

    Args:
        db_path: Override the default DB_PATH.

    Returns:
        sqlite3.Connection with the schema initialized.
    """
    path = db_path or DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            -- Discovery stage (smart_extract / job_search)
            url                   TEXT PRIMARY KEY,
            title                 TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            strategy              TEXT,
            discovered_at         TEXT,

            -- Enrichment stage (detail_scraper)
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,

            -- Scoring stage (job_scorer)
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,

            -- Tailoring stage (resume tailor)
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,

            -- Cover letter stage
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,

            -- Application stage
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT
        )
    """)
    conn.commit()

    # Run migrations for any columns added after initial schema
    ensure_columns(conn)

    return conn


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    "scored_at": "TEXT",
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_at": "TEXT",
    "tailor_attempts": "INTEGER DEFAULT 0",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    # Application
    "applied_at": "TEXT",
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "apply_task_id": "TEXT",
    "verification_confidence": "TEXT",
    # Geographic eligibility (deterministic, see applypilot.eligibility)
    "eligibility_status": "TEXT",
    "eligibility_reason": "TEXT",
    "eligibility_evidence": "TEXT",
    "eligibility_tier": "TEXT",
    "eligibility_version": "TEXT",
    "eligibility_at": "TEXT",
    # --- Fork additions (all additive; see DEPLOYMENT.md "Database migration") ---
    # Discovery history: when/how often/where a posting was seen
    "company": "TEXT",
    "job_type": "TEXT",
    "date_posted": "TEXT",          # only as reported by the source; never invented
    "first_seen_at": "TEXT",
    "last_seen_at": "TEXT",
    "seen_count": "INTEGER DEFAULT 0",
    "requisition_id": "TEXT",
    # Posting verification (applypilot.verify)
    "posting_status": "TEXT",       # active | closed | removed | inaccessible | uncertain
    "posting_reason": "TEXT",
    "posting_http_status": "INTEGER",
    "posting_checked_at": "TEXT",
    "posting_verified_url": "TEXT",
    # Cross-source duplicates (applypilot.dedupe)
    "dedupe_key": "TEXT",
    "duplicate_of": "TEXT",
    "duplicate_reason": "TEXT",
    # Scoring traceability
    "score_model": "TEXT",
    "score_config": "TEXT",
    "score_error": "TEXT",
    "score_attempts": "INTEGER DEFAULT 0",
    # Document validation (resume + cover letter)
    "resume_validation": "TEXT",    # passed | rejected | needs_review
    "resume_validation_reasons": "TEXT",
    "resume_model": "TEXT",
    "resume_rejected_path": "TEXT",
    "cover_validation": "TEXT",
    "cover_validation_reasons": "TEXT",
    "cover_model": "TEXT",
    "cover_rejected_path": "TEXT",
    "documents_for": "TEXT",        # fingerprint of the job text the documents were made from
    # Screening questions (applypilot.screening)
    "screening_status": "TEXT",     # supported | needs_review | none
    "screening_report": "TEXT",
    # Readiness + human review (applypilot.readiness)
    "readiness": "TEXT",            # ready | blocked
    "readiness_reasons": "TEXT",
    "readiness_at": "TEXT",
    "review_status": "TEXT",        # approved | rejected | deferred | manual_submitted
    "review_note": "TEXT",
    "reviewed_at": "TEXT",
    # Immutable artifact binding (scoring.documents): exact content hashes of the approved
    # text, the PDF rendered from that text, and the hashes a human approved.
    "resume_sha": "TEXT",
    "resume_pdf_path": "TEXT",
    "resume_pdf_sha": "TEXT",
    "cover_sha": "TEXT",
    "cover_pdf_path": "TEXT",
    "cover_pdf_sha": "TEXT",
    "cover_for": "TEXT",            # input fingerprint the cover letter was made from
    "review_artifacts": "TEXT",     # JSON manifest of hashes at human approval
    # Verification / eligibility input binding (applypilot.verify, eligibility)
    "posting_final_url": "TEXT",    # URL actually reached after redirects
    "eligibility_input": "TEXT",    # hash of the inputs the eligibility decision used
}

# Auxiliary tables (created if missing; never dropped).
_AUX_TABLES = (
    """CREATE TABLE IF NOT EXISTS job_sources (
        url TEXT NOT NULL, source TEXT NOT NULL, source_url TEXT,
        first_seen_at TEXT, last_seen_at TEXT, seen_count INTEGER DEFAULT 0,
        PRIMARY KEY (url, source))""",
    """CREATE TABLE IF NOT EXISTS discovery_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, source TEXT, query TEXT,
        started_at TEXT, finished_at TEXT, status TEXT, new_count INTEGER DEFAULT 0,
        seen_count INTEGER DEFAULT 0, error TEXT)""",
    """CREATE TABLE IF NOT EXISTS score_history (
        url TEXT NOT NULL, fit_score INTEGER, score_reasoning TEXT, scored_at TEXT, score_model TEXT,
        score_config TEXT, replaced_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS pipeline_runs (

        run_id TEXT PRIMARY KEY, kind TEXT, started_at TEXT, finished_at TEXT, status TEXT,
        exit_code INTEGER, summary TEXT)""",
    # Append-only application/review history (never updated or deleted by ApplyPilot).
    """CREATE TABLE IF NOT EXISTS application_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, at TEXT NOT NULL, event TEXT NOT NULL,
        actor TEXT, detail TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_application_events_url ON application_events(url)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_dedupe_key ON jobs(dedupe_key)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_application_url ON jobs(application_url)",
)


def ensure_aux_tables(conn: sqlite3.Connection) -> None:
    for sql in _AUX_TABLES:
        conn.execute(sql)
    conn.commit()


def log_event(conn: sqlite3.Connection, url: str, event: str, detail: str = "", actor: str = "") -> None:
    """Append one application/review history record (caller commits with its own change)."""
    conn.execute("INSERT INTO application_events (url, at, event, actor, detail) VALUES (?, ?, ?, ?, ?)",
                 (url, datetime.now(UTC).isoformat(), event, actor or None, (detail or "")[:500] or None))


def record_sighting(conn: sqlite3.Connection, url: str, source: str, *, company: str | None = None,
                    date_posted: str | None = None, job_type: str | None = None,
                    source_url: str | None = None, now: str | None = None) -> None:
    """Persistent discovery history for one posting seen by one source (new or already known).

    Only fills fields that are still empty (first source wins) and never overwrites a stored
    posting date. ``date_posted`` must come from the source itself -- callers never guess it."""
    now = now or datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE jobs SET first_seen_at = COALESCE(first_seen_at, discovered_at, ?), last_seen_at = ?,"
        " seen_count = COALESCE(seen_count, 0) + 1, company = COALESCE(company, ?),"
        " date_posted = COALESCE(date_posted, ?), job_type = COALESCE(job_type, ?) WHERE url = ?",
        (now, now, company or None, date_posted or None, job_type or None, url))
    conn.execute(
        "INSERT INTO job_sources (url, source, source_url, first_seen_at, last_seen_at, seen_count)"
        " VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT(url, source) DO UPDATE SET last_seen_at = excluded.last_seen_at,"
        " seen_count = job_sources.seen_count + 1", (url, source or "unknown", source_url or url, now, now))


def log_discovery_run(conn: sqlite3.Connection, *, run_id: str, source: str, query: str = "",
                      started_at: str, status: str, new_count: int = 0, seen_count: int = 0,
                      error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO discovery_runs (run_id, source, query, started_at, finished_at, status, new_count,"
        " seen_count, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, source, query, started_at, datetime.now(UTC).isoformat(), status, new_count,
         seen_count, (error or "")[:500] or None))
    conn.commit()


def ensure_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Reads the current table schema via PRAGMA table_info and compares against
    the full column registry. Any missing columns are added with ALTER TABLE.

    This makes it safe to upgrade the database from any previous version --
    columns are only added, never removed or renamed.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of column names that were added (empty if schema was already current).
    """
    if conn is None:
        conn = get_connection()

    # Serialized (audit R5/R9): the schema is read and altered inside one write transaction, so two
    # processes bootstrapping an older database at the same time cannot both try the same ALTER.
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    added = []
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        for col, dtype in _ALL_COLUMNS.items():
            if col not in existing:
                # PRIMARY KEY columns can't be added via ALTER TABLE, but url
                # is always created with the table itself so this is safe
                if "PRIMARY KEY" in dtype:
                    continue
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
                added.append(col)
        for sql in _AUX_TABLES:
            conn.execute(sql)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return added



def get_stats(conn: sqlite3.Connection | None = None) -> dict:
    """Return job counts by pipeline stage.

    Provides a snapshot of how many jobs are at each stage, useful for
    dashboard display and pipeline progress tracking.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with keys:
            total, by_site, pending_detail, with_description,
            scored, unscored, tailored, untailored_eligible,
            with_cover_letter, applied, score_distribution
    """
    if conn is None:
        conn = get_connection()

    stats: dict = {}

    # Total jobs
    stats["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # By site breakdown
    rows = conn.execute(
        "SELECT site, COUNT(*) as cnt FROM jobs GROUP BY site ORDER BY cnt DESC"
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Enrichment stage
    stats["pending_detail"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL"
    ).fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND fit_score IS NULL"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL"
    ).fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= 7 AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= 5 "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '')"
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL"
    ).fetchone()[0]

    stats["dry_run"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_status = 'dry_run'"
    ).fetchone()[0]

    stats["needs_review"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_status = 'needs_review'"
    ).fetchone()[0]

    stats["eligibility"] = [
        (row[0] or "unclassified", row[1]) for row in conn.execute(
            "SELECT eligibility_status, COUNT(*) FROM jobs GROUP BY eligibility_status ORDER BY 2 DESC"
        ).fetchall()
    ]

    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND applied_at IS NULL "
        "AND application_url IS NOT NULL"
    ).fetchone()[0]

    return stats


def store_jobs(conn: sqlite3.Connection, jobs: list[dict],
               site: str, strategy: str) -> tuple[int, int]:
    """Store discovered jobs, skipping duplicates by URL.

    Args:
        conn: Database connection.
        jobs: List of job dicts with keys: url, title, salary, description, location.
        site: Source site name (e.g. "RemoteOK", "Dice").
        strategy: Extraction strategy used (e.g. "json_ld", "api_response", "css_selectors").

    Returns:
        Tuple of (new_count, duplicate_count).
    """
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), job.get("salary"), job.get("description"),
                 job.get("location"), site, strategy, now),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1
        record_sighting(conn, url, site, company=job.get("company"), date_posted=job.get("date_posted"),
                        job_type=job.get("job_type"), now=now)

    conn.commit()
    return new, existing


def get_jobs_by_stage(conn: sqlite3.Connection | None = None,
                      stage: str = "discovered",
                      min_score: int | None = None,
                      limit: int = 100) -> list[dict]:
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied".
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        limit: Maximum number of rows to return.

    Returns:
        List of job dicts.
    """
    if conn is None:
        conn = get_connection()

    from applypilot.eligibility import SQL_ELIGIBLE, SQL_NOT_INELIGIBLE

    conditions = {
        "discovered": "1=1",
        "pending_detail": "detail_scraped_at IS NULL",
        "enriched": "full_description IS NOT NULL",
        # Ineligible jobs never consume LLM scoring; unknown/review jobs are still scored.
        # Duplicates of another row and postings confirmed closed/removed are skipped; a job whose
        # scoring failed 5 times stops being retried automatically (see score_error).
        "pending_score": (
            f"full_description IS NOT NULL AND fit_score IS NULL AND {SQL_NOT_INELIGIBLE} "
            "AND duplicate_of IS NULL AND COALESCE(score_attempts, 0) < 5 "
            "AND (posting_status IS NULL OR posting_status NOT IN ('closed', 'removed'))"
        ),
        "scored": "fit_score IS NOT NULL",
        # Tailoring and applying require verified eligibility.
        "pending_tailor": (
            "fit_score >= ? AND full_description IS NOT NULL "
            f"AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5 AND {SQL_ELIGIBLE} "
            # documents are prepared only for live, non-duplicate postings (applypilot.verify / dedupe)
            "AND posting_status = 'active' AND duplicate_of IS NULL "
            "AND COALESCE(resume_validation, '') != 'needs_review'"
        ),
        "tailored": "tailored_resume_path IS NOT NULL",
        "pending_apply": (
            "tailored_resume_path IS NOT NULL AND applied_at IS NULL "
            f"AND application_url IS NOT NULL AND {SQL_ELIGIBLE}"
        ),
        "applied": "applied_at IS NOT NULL",
    }

    where = conditions.get(stage, "1=1")
    params: list = []

    if "?" in where and min_score is not None:
        params.append(min_score)
    elif "?" in where:
        params.append(7)  # default min_score

    if min_score is not None and "fit_score" not in where and stage in ("scored", "tailored", "applied"):
        where += " AND fit_score >= ?"
        params.append(min_score)

    query = f"SELECT * FROM jobs WHERE {where} ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()

    # Convert sqlite3.Row objects to dicts
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []
