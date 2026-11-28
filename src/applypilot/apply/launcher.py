"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live

from applypilot import config, eligibility
from applypilot.database import get_connection
from applypilot.apply import chrome, dashboard, dry_run_guard, prompt as prompt_mod
from applypilot.apply import approval, network_policy, policy as policy_mod, verification_mcp
from applypilot.apply import policy_hook as policy_hook_mod
from applypilot.apply.network_guard import NetworkGuard, NetworkGuardError
from applypilot.apply.redact import Redactor
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals,
)

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Hard ceiling on one agent session; a hung session is killed and flagged for review.
APPLY_MAX_SECONDS = int(os.environ.get("APPLYPILOT_APPLY_MAX_SECONDS", "1200"))

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_lock = threading.Lock()
# Workers whose current job reached the agent (a submission may have happened).
_agent_started: set[int] = set()

_exit_hooks_installed = False


def _install_exit_hooks() -> None:
    """Register Chrome cleanup and SIGTERM handling. Called from main(), never at import."""
    global _exit_hooks_installed
    if _exit_hooks_installed:
        return
    atexit.register(cleanup_on_exit)
    if platform.system() != "Windows":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    _exit_hooks_installed = True


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

# Exact versions: never fetch @latest at run time (supply-chain drift, E3).
PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp@0.0.86"


def verification_enabled() -> bool:
    """The verification-code helper is offered only on explicit opt-in AND with readonly credentials.

    Off by default (audit L1): it cannot yet bind a message to this application/session, so a
    concurrent session could receive an unrelated code. Opt in with APPLYPILOT_ENABLE_VERIFY_HELPER=1
    for a supervised single-worker run only. A credential-path override (APPLYPILOT_GMAIL_CREDENTIALS)
    is refused because the helper process never receives it."""
    if os.environ.get("APPLYPILOT_ENABLE_VERIFY_HELPER") != "1" or os.environ.get("APPLYPILOT_GMAIL_CREDENTIALS"):
        return False
    return verification_mcp.credentials_path().exists()


def _make_mcp_config(cdp_port: int, policy_path: Path | None = None,
                     verify_enabled: bool = False) -> dict:
    """MCP servers for one apply session: the pinned browser and, optionally, the
    narrow verification-code helper. There is no general email server (S3)."""
    servers = {
        "playwright": {
            "command": "npx",
            "args": [
                "-y", PLAYWRIGHT_MCP_PACKAGE,
                f"--cdp-endpoint=http://127.0.0.1:{cdp_port}",
                f"--viewport-size={config.DEFAULTS['viewport']}",
            ],
        },
    }
    if verify_enabled:
        if policy_path is None:
            raise ValueError("the verification helper needs the session policy file")
        servers["applypilot_verify"] = {
            "command": sys.executable,
            "args": ["-m", "applypilot.apply.verification_mcp", str(policy_path)],
        }
    return {"mcpServers": servers}


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

# Apply status lifecycle
#   NULL / 'failed' (attempts < max)  -> eligible for the queue
#   'dry_run'                         -> eligible for real runs; skipped by dry-run batches
#   'in_progress'                     -> claimed by a worker (agent_id marks dry-run claims)
#   'applied'                         -> terminal; only set by a real run or --mark-applied
#   'needs_review'                    -> outcome unknown (agent may have submitted) or a human
#                                        action is needed (apply_error 'human:<reason>'); a human
#                                        must resolve it with --mark-applied / --mark-failed.
#                                        Dry runs land here too when their outcome is uncertain,
#                                        they were interrupted after the agent started, or the
#                                        policy hook recorded a violation -- never requeued.
#   'manual'                          -> ATS requires manual handling
# Agent failure reasons that need a human (account creation, email-only application, an
# unanswerable question, a refused submission approval). Real runs -> needs_review.
HUMAN_ACTION_FAILURES = {
    "account_required", "email_application", "email_verification_required", "needs_human",
    "needs_human_answer", "submit_not_approved", "policy_violation",
}
# Statuses that block acquisition of any other row sharing the same application_url.
DUPLICATE_BLOCKING_STATUSES = ("applied", "in_progress", "needs_review")
DRY_RUN_AGENT_SUFFIX = ":dry_run"
# Identifies this launcher process in agent_id ("<session>:worker-N[:dry_run]") for claim ownership.
SESSION_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

_JOB_COLUMNS = """j.url, j.title, j.site, j.application_url, j.tailored_resume_path,
                  j.fit_score, j.location, j.full_description, j.cover_letter_path,
                  j.eligibility_status, j.eligibility_reason"""

# Only verified-eligible jobs can be claimed, in queue mode AND for explicit --url targets.
# Automatic decisions must come from the current classifier version; human approvals always count.
_ELIGIBLE_CLAUSE = (
    f"j.eligibility_status IN ({','.join(repr(s) for s in eligibility.ELIGIBLE_STATUSES)})"
    f" AND (j.eligibility_version = '{eligibility.CLASSIFIER_VERSION}'"
    f" OR j.eligibility_version = '{eligibility.MANUAL_VERSION}')"
)

# No other row for the same application URL may already be applied, claimed or under review.
_NO_DUPLICATE_CLAUSE = f"""NOT EXISTS (
        SELECT 1 FROM jobs AS o
        WHERE o.url != j.url
          AND COALESCE(o.application_url, o.url) = COALESCE(j.application_url, j.url)
          AND o.apply_status IN ({",".join(repr(s) for s in DUPLICATE_BLOCKING_STATUSES)})
    )"""


# Fork: only application-ready jobs are claimed (applypilot.readiness); a real submission also
# needs the human review approval. Readiness is re-evaluated after the claim (worker_loop).
_READY_CLAUSE = "j.readiness = 'ready' AND j.duplicate_of IS NULL"
_APPROVED_CLAUSE = "j.review_status = 'approved'"


def _ready_clause(dry_run: bool) -> str:
    return _READY_CLAUSE if dry_run else f"{_READY_CLAUSE} AND {_APPROVED_CLAUSE}"


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _select_target(conn, target_url: str, dry_run: bool = False):
    """Find an explicitly requested job by its exact job URL (audit M6: never a fuzzy match).

    The target must resolve to exactly one row; an application URL shared by several rows is
    ambiguous and refused. The attempt cap applies as in queue mode."""
    rows = conn.execute(f"""
        SELECT {_JOB_COLUMNS}
        FROM jobs AS j
        WHERE (j.url = ? OR (j.application_url = ? AND NOT EXISTS (SELECT 1 FROM jobs WHERE url = ?)))
          AND j.tailored_resume_path IS NOT NULL
          AND (j.apply_status IS NULL OR j.apply_status IN ('failed', 'dry_run'))
          AND j.applied_at IS NULL
          AND (j.apply_attempts IS NULL OR j.apply_attempts < ?)
          AND {_ELIGIBLE_CLAUSE}
          AND {_NO_DUPLICATE_CLAUSE}
          AND {_ready_clause(dry_run)}
        LIMIT 2
    """, (target_url, target_url, target_url, config.DEFAULTS["max_apply_attempts"])).fetchall()
    return rows[0] if len(rows) == 1 else None


def _select_next(conn, min_score: int, dry_run: bool):
    """Pick the highest-scoring retry-eligible job from the queue."""
    blocked_sites, blocked_patterns = _load_blocked()
    params: list = [config.DEFAULTS["max_apply_attempts"], min_score]
    site_clause = ""
    if blocked_sites:
        placeholders = ",".join("?" * len(blocked_sites))
        site_clause = f"AND j.site NOT IN ({placeholders})"
        params.extend(blocked_sites)
    url_clauses = " ".join("AND j.url NOT LIKE ?" for _ in blocked_patterns)
    params.extend(blocked_patterns)
    # Real runs may pick up jobs that were only dry-run; dry-run batches move on to new jobs.
    statuses = "('failed')" if dry_run else "('failed', 'dry_run')"
    return conn.execute(f"""
        SELECT {_JOB_COLUMNS}
        FROM jobs AS j
        WHERE j.tailored_resume_path IS NOT NULL
          AND (j.apply_status IS NULL OR j.apply_status IN {statuses})
          AND j.applied_at IS NULL
          AND (j.apply_attempts IS NULL OR j.apply_attempts < ?)
          AND j.fit_score >= ?
          {site_clause}
          {url_clauses}
          AND {_ELIGIBLE_CLAUSE}
          AND {_NO_DUPLICATE_CLAUSE}
          AND {_ready_clause(dry_run)}
        ORDER BY CASE j.eligibility_tier WHEN 'highest' THEN 0 WHEN 'high' THEN 1
                      WHEN 'acceptable' THEN 2 ELSE 3 END,
                 j.fit_score DESC, j.url
        LIMIT 1
    """, params).fetchone()


def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0, dry_run: bool = False) -> dict | None:
    """Atomically claim the next job to apply to.

    Selection and claim run inside one ``BEGIN IMMEDIATE`` transaction, so
    concurrent workers (each with its own connection) can never claim the
    same row. Applied, in-progress and needs-review jobs are never returned,
    nor are jobs whose application URL was already applied to via another row.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold (queue mode only).
        worker_id: Worker claiming this job (for tracking).
        dry_run: Claim for a dry run (recorded in agent_id for crash recovery).

    Returns:
        Job dict or None if nothing eligible is available.
    """
    from applypilot.config import is_manual_ats

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        while True:
            row = (_select_target(conn, target_url, dry_run) if target_url
                   else _select_next(conn, min_score, dry_run))
            if not row:
                conn.rollback()
                return None

            apply_url = row["application_url"] or row["url"]
            if is_manual_ats(apply_url):
                # Unsolvable-CAPTCHA ATS: hand off to the human and keep looking.
                conn.execute(
                    "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                    (row["url"],),
                )
                logger.info("Skipping manual ATS: %s", row["url"][:80])
                if target_url:
                    conn.commit()
                    return None
                continue

            agent_id = f"{SESSION_ID}:worker-{worker_id}" + (DRY_RUN_AGENT_SUFFIX if dry_run else "")
            claimed = conn.execute("""
                UPDATE jobs SET apply_status = 'in_progress',
                               agent_id = ?,
                               last_attempted_at = ?
                WHERE url = ?
                  AND (apply_status IS NULL OR apply_status IN ('failed', 'dry_run'))
                  AND applied_at IS NULL
            """, (agent_id, datetime.now(timezone.utc).isoformat(), row["url"])).rowcount
            if claimed != 1:  # Defensive: the row changed under us.
                conn.rollback()
                return None
            conn.commit()
            job = dict(row)
            job["agent_id"] = agent_id
            return job
    except Exception:
        conn.rollback()
        raise


def explain_target(target_url: str, dry_run: bool = False) -> str | None:
    """Why an explicit --url target cannot be acquired, or None if it can (read-only)."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT url, apply_status, tailored_resume_path, eligibility_status, eligibility_reason,
               eligibility_version, readiness, readiness_reasons, review_status
        FROM jobs WHERE url = ?
    """, (target_url,)).fetchall()
    if not rows:
        return "no job with exactly this URL (use the job URL shown by `applypilot review`)"
    row = rows[0]
    status = row["eligibility_status"]
    if status not in eligibility.ELIGIBLE_STATUSES:
        return (f"eligibility is '{status or 'unclassified'}' ({row['eligibility_reason'] or 'not evaluated'}). "
                "Review it with `applypilot eligibility`; approve with --approve URL if appropriate.")
    if row["apply_status"] in DUPLICATE_BLOCKING_STATUSES:
        return f"apply status is '{row['apply_status']}'"
    if not row["tailored_resume_path"]:
        return "no tailored resume yet (run `applypilot run tailor`)"
    if row["readiness"] != "ready":
        return f"not application-ready: {row['readiness_reasons'] or 'run `applypilot run prepare`'}"
    if not dry_run and row["review_status"] != "approved":
        return "not approved in review (check the documents, then `applypilot review --approve URL`)"
    return None


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None, owner: str | None = None) -> bool:
    """Update a job's apply status in the database. Returns False if nothing was written.

    Only status 'applied' ever sets applied_at. Non-applied statuses never
    overwrite an 'applied' row. 'dry_run' and 'needs_review' leave
    apply_attempts untouched so they never consume retries.

    With ``owner`` (the agent_id from acquire_job) the write only happens
    while the row is still that owner's in-progress claim, so a late result
    can never overwrite another worker's claim, a stale-lock recovery or a
    human's --mark-applied/--mark-failed decision. A late result for a row
    already under review is appended to its apply_error for the reviewer.
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if owner is not None:
        guard, guard_params = "AND apply_status = 'in_progress' AND agent_id = ?", [owner]
    elif status == "applied":
        guard, guard_params = "", []
    else:
        guard, guard_params = "AND (apply_status IS NULL OR apply_status != 'applied')", []

    if status == "applied":
        cur = conn.execute(f"""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ? {guard}
        """, [now, duration_ms, task_id, url, *guard_params])
    elif status in ("dry_run", "needs_review"):
        cur = conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ? {guard}
        """, [status, error, duration_ms, task_id, url, *guard_params])
    else:
        attempts = 99 if permanent else "COALESCE(apply_attempts, 0) + 1"
        cur = conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ? {guard}
        """, [status, error or "unknown", duration_ms, task_id, url, *guard_params])

    written = cur.rowcount == 1
    if not written and owner is not None:
        late = f"{status}:{error}" if error else status
        conn.execute("""
            UPDATE jobs SET apply_error = COALESCE(apply_error, '') || ' | late result: ' || ?
            WHERE url = ? AND apply_status = 'needs_review'
        """, (late[:120], url))
        logger.warning("Ownership of %s lost before result '%s' was recorded", url[:80], late[:60])
    conn.commit()
    return written


def record_outcome(url: str, result: str, dry_run: bool,
                   duration_ms: int | None = None, owner: str | None = None) -> str:
    """Persist an agent result.

    Returns 'applied', 'dry_run', 'needs_review', 'failed', or
    'ownership_lost' when the claim was no longer ours (nothing overwritten).

    Launcher-side enforcement: a dry run can never be recorded as applied,
    whatever the agent reports, and never consumes retry attempts; a dry run
    whose submit-like action was not blocked by the guard goes to review. In
    real runs, outcomes where a submission may have happened ('uncertain:*')
    go to human review instead of being retried.
    """
    if dry_run:
        # The dry-run guard is best-effort: any uncertain dry-run outcome (or an agent
        # claiming APPLIED) may hide a real submission, so it is never requeued.
        if result == "applied":
            result = "uncertain:applied_claimed_in_dry_run"
        if result.startswith("uncertain:"):
            status, note, outcome = "needs_review", result, "needs_review"
        else:
            note = None if result == "dry_run" else f"dry_run:{result}"
            status, outcome = "dry_run", "dry_run"
        written = mark_result(url, status, note, duration_ms=duration_ms, owner=owner)
        return outcome if written else "ownership_lost"
    if result == "dry_run":
        result = "uncertain:unexpected_dry_run_result"
    reason_only = result.split(":", 1)[-1].lower() if result.startswith("failed:") else ""
    if result == "applied":
        status, error, permanent, outcome = "applied", None, False, "applied"
    elif result.startswith("uncertain:"):
        status, error, permanent, outcome = "needs_review", result, False, "needs_review"
    elif reason_only in HUMAN_ACTION_FAILURES:
        status, error, permanent, outcome = "needs_review", f"human:{reason_only}", False, "needs_review"
    else:
        reason = result.split(":", 1)[-1] if ":" in result else result
        status, error, permanent, outcome = "failed", reason, _is_permanent_failure(result), "failed"
    written = mark_result(url, status, error, permanent=permanent,
                          duration_ms=duration_ms, owner=owner)
    return outcome if written else "ownership_lost"


def recover_stale_locks(max_age_seconds: int | None = None) -> int:
    """Resolve 'in_progress' claims left behind by crashed or killed workers.

    Every stale claim -- real or dry run -- becomes 'needs_review': the agent
    may have submitted before dying (the dry-run guard is best-effort), so
    retrying automatically could produce a duplicate application.

    Returns:
        Number of rows recovered.
    """
    if max_age_seconds is None:
        # A live claim cannot outlast the watchdog plus Chrome start-up and process shutdown.
        max_age_seconds = APPLY_MAX_SECONDS + config.DEFAULTS["apply_timeout"] + 300
    cutoff = datetime.fromtimestamp(time.time() - max_age_seconds, tz=timezone.utc).isoformat()
    # Never touch this process's own claims; other instances' claims only once provably stale.
    stale = ("apply_status = 'in_progress' AND (last_attempted_at IS NULL OR last_attempted_at < ?)"
             " AND (agent_id IS NULL OR agent_id NOT LIKE ?)")
    own = f"{SESSION_ID}:%"
    conn = get_connection()
    # Dry-run claims are flagged too: we cannot tell whether the agent had started,
    # and the dry-run guard does not prove that nothing was submitted.
    released = 0
    flagged = conn.execute(f"""
        UPDATE jobs SET apply_status = 'needs_review',
                       apply_error = 'uncertain:interrupted_stale_lock', agent_id = NULL
        WHERE {stale}
    """, (cutoff, own)).rowcount
    conn.commit()
    purge_stale_secrets()
    return released + flagged


def purge_stale_secrets() -> int:
    """Delete verification-code files left by sessions that ended abruptly (audit R10).

    Normal session end removes them; a crash or power loss could leave plaintext codes on
    disk. Runs at every launcher start (with stale-claim recovery), before any new session."""
    root = config.APP_DIR / "apply-sessions"
    n = 0
    for f in root.glob("*/secrets.txt") if root.exists() else []:
        try:
            f.unlink()
            n += 1
        except OSError:
            logger.warning("could not remove stale verification secrets in %s", f.parent.name)
    return n


def release_lock(url: str, owner: str | None = None) -> bool:
    """Release an in_progress claim without changing status (only our own claim if owner given)."""
    conn = get_connection()
    sql = "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'"
    params: list = [url]
    if owner is not None:
        sql += " AND agent_id = ?"
        params.append(owner)
    released = conn.execute(sql, params).rowcount == 1
    conn.commit()
    return released


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text)

    # Release the lock so the job stays available
    release_lock(job["url"], owner=job["agent_id"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> tuple[bool, str]:
    """Operator recovery: record a job as applied or failed (audit R8: transactional, checked).

    * A live ``in_progress`` claim is never changed (a worker may still be acting on it).
    * Marking failed never erases an application: a row with ``applied_at`` stays applied.
    * The change must hit exactly one row; every change is appended to ``application_events``.

    Returns:
        (changed, message)
    """
    from applypilot.database import log_event
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT apply_status, applied_at FROM jobs WHERE url = ?", (url,)).fetchone()
        if row is None:
            conn.rollback()
            return False, "no job with that exact URL"
        if row["apply_status"] == "in_progress":
            conn.rollback()
            return False, "a worker currently holds this job; wait for it to finish (or for stale-claim recovery)"
        if status == "applied":
            cur = conn.execute("""
                UPDATE jobs SET apply_status = 'applied', applied_at = COALESCE(applied_at, ?),
                               apply_error = NULL, agent_id = NULL, verification_confidence = 'human_reported'
                WHERE url = ? AND COALESCE(apply_status, '') != 'in_progress'
            """, (now, url))
        else:
            if row["applied_at"] or row["apply_status"] == "applied":
                conn.rollback()
                return False, "an application is recorded for this job; it cannot be marked failed"
            cur = conn.execute("""
                UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                               apply_attempts = 99, agent_id = NULL
                WHERE url = ? AND applied_at IS NULL AND COALESCE(apply_status, '') NOT IN ('in_progress', 'applied')
            """, (reason or "manual", url))
        if cur.rowcount != 1:
            conn.rollback()
            return False, "the job changed concurrently; nothing recorded"
        log_event(conn, url, f"operator_mark_{status}", reason or "", actor="cli")
        conn.commit()
        return True, status
    except BaseException:
        conn.rollback()
        raise


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Never touches applied, in-progress or needs-review jobs: a needs-review
    job may already have been submitted and must be resolved by a human. A row with
    ``applied_at`` set is never reset, whatever its status says (audit R8).

    Returns:
        Number of jobs reset.
    """
    from applypilot.database import log_event
    conn = get_connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        urls = [r[0] for r in conn.execute("""
            SELECT url FROM jobs WHERE apply_status IS NOT NULL AND applied_at IS NULL
              AND apply_status NOT IN ('applied', 'in_progress', 'needs_review')""")]
        cursor = conn.execute("""
            UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                           apply_attempts = 0, agent_id = NULL
            WHERE apply_status IS NOT NULL AND applied_at IS NULL
              AND apply_status NOT IN ('applied', 'in_progress', 'needs_review')
        """)
        for u in urls:
            log_event(conn, u, "operator_reset_failed", actor="cli")
        conn.commit()
        return cursor.rowcount
    except BaseException:
        conn.rollback()
        raise



# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

# A result line must stand alone: optional markdown decoration, the code, an optional
# ":reason" and an optional " -- comment". Inline mentions in prose are ignored.
_RESULT_LINE_RE = re.compile(
    r"RESULT:(?P<code>[A-Z_]+)(?::[ \t]*(?P<reason>[A-Za-z0-9_\- ]{1,80}?))?(?:[ \t]+--[^\n]*)?"
)
_SIMPLE_RESULTS = {"APPLIED", "DRY_RUN", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE"}
_PROMOTE_FAILURE_REASONS = {"captcha", "expired", "login_issue"}
# Failure reasons that themselves imply a submission may already have happened.
_POST_SUBMIT_FAILURES = {"captcha_after_submit"}


def parse_agent_result(final_text: str | None) -> str:
    """Classify the agent's FINAL reply (stream-json ``result`` event) into a result string.

    Only standalone result lines count, so result codes quoted in reasoning,
    web pages or tool output cannot establish anything. Returns 'applied',
    'dry_run', 'expired', 'captcha', 'login_issue', 'failed:<reason>' or
    'uncertain:<reason>'. Missing, malformed or conflicting results are
    uncertain and go to human review.
    """
    if final_text is None:
        return "uncertain:no_final_result"
    found: list[tuple[str, str]] = []
    for raw in final_text.splitlines():
        line = re.sub(r"[*`]", "", raw).strip().lstrip(">#_ ").rstrip("_ ").strip()
        if not line.startswith("RESULT:"):
            continue
        match = _RESULT_LINE_RE.fullmatch(line)
        if not match:
            return "uncertain:malformed_result"
        found.append((match["code"], (match["reason"] or "").strip()))
    if not found:
        return "uncertain:no_result_line"
    if len(set(found)) > 1:
        return "uncertain:conflicting_results"
    code, reason = found[0]
    if code in _SIMPLE_RESULTS and not reason:
        return code.lower()
    if code == "FAILED":
        reason = reason or "unknown"
        if reason.lower() in _POST_SUBMIT_FAILURES:
            return f"uncertain:{reason.lower()}"
        if reason.lower() in _PROMOTE_FAILURE_REASONS:
            return reason.lower()
        return f"failed:{reason}"
    return "uncertain:malformed_result"


def classify_session(final_text: str | None, *, final_is_error: bool, dry_run: bool,
                     submit_attempts: int, unblocked_submits: int,
                     watchdog_fired: bool = False, interrupted: bool = False,
                     security_failure: str | None = None, tool_uses: int = 0,
                     audit_result: str | None = None,
                     submit_approved: bool = False, confirmation_seen: bool = False,
                     released_writes: int = 0, ambiguous_writes: int = 0,
                     evidence_correlated: bool | None = None) -> str:
    """Combine the agent's final reply with launcher observations into one result.

    The agent's own claim is never enough on its own:
    - the security configuration could not be verified -> failed if the agent
      never used a tool, otherwise uncertain;
    - the policy hook audit found a violation or a tool call without a hook
      decision -> uncertain (possible injection / unenforced policy);
    - a session killed by the watchdog / interrupted, or one that ended in an
      agent error, is uncertain unless it produced a clean result;
    - in a real run, APPLIED needs independent evidence: a human-approved
      submit action plus confirmation text seen afterwards; otherwise uncertain;
    - in a real run, any non-APPLIED result after a submit-like action means
      a submission may have happened -> uncertain (human review);
    - in a dry run, a submit-like action the guard did not block, or an agent
      claiming APPLIED, means a real submission may have happened -> uncertain.
    - any human-approved write the network guard released (or may have released) makes
      every non-APPLIED outcome uncertain: data reached the employer, so the job is never
      retried automatically (audit H3). APPLIED additionally needs causal evidence
      (approved submit -> released write -> later confirmation).
    """
    if evidence_correlated is None:
        evidence_correlated = submit_approved and confirmation_seen
    wrote = released_writes + ambiguous_writes
    if security_failure:
        if tool_uses:
            return "uncertain:security_config_unverified"
        return "failed:security_config_unverified"
    if audit_result:
        return audit_result
    result = parse_agent_result(final_text)
    if watchdog_fired:
        result = "uncertain:watchdog_timeout"
    elif interrupted and result.startswith("uncertain:"):
        result = "uncertain:interrupted"
    elif final_is_error and not result.startswith("uncertain:"):
        result = f"uncertain:agent_error_after_{result.replace(':', '_')}"
    if dry_run:
        if wrote:
            return "uncertain:dry_run_write_released"
        if unblocked_submits:
            return "uncertain:dry_run_submit_not_blocked"
        if result == "applied":
            return "uncertain:applied_claimed_in_dry_run"
        return result
    if result == "applied" and not (submit_approved and evidence_correlated and released_writes
                                    and not ambiguous_writes):
        return "uncertain:applied_without_evidence"
    if wrote and result != "applied" and not result.startswith("uncertain:"):
        return f"uncertain:after_released_write:{result}"
    if submit_attempts and result != "applied" and not result.startswith("uncertain:"):
        return f"uncertain:after_submit_action:{result}"
    return result


def is_submit_action(tool_name: str, tool_input: dict) -> bool:
    """True when a tool call looks like it commits an application (click Submit, send email)."""
    name = (tool_name or "").split("__")[-1]
    if name == "send_email":
        return True
    if name in {"browser_click", "browser_double_click"}:
        label = " ".join(str((tool_input or {}).get(k, "")) for k in ("element", "text", "name"))
        return bool(dry_run_guard.SUBMIT_WORDS.search(label))
    # Same rules as the hook: Enter and type-with-submit can commit a form too.
    return policy_hook_mod._is_submit_like(tool_name or "", tool_input or {}) is not None


EXPECTED_MCP_SERVERS = {"playwright", "applypilot_verify"}
HOOK_TIMEOUT_S = 900  # covers the real-run human approval wait


def policy_hook_settings(policy_path: Path) -> dict:
    """Claude Code settings installing the session policy hooks for every tool call."""
    base = f'"{sys.executable}" -m applypilot.apply.policy_hook'
    pp = f'"{policy_path}"'
    return {"hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": f"{base} start {pp}"}]}],
        "PreToolUse": [{"matcher": ".*", "hooks": [
            {"type": "command", "command": f"{base} pre {pp}", "timeout": HOOK_TIMEOUT_S}]}],
        "PostToolUse": [{"matcher": ".*", "hooks": [
            {"type": "command", "command": f"{base} post {pp}"}]}],
    }}


def build_agent_command(model: str, mcp_config_path: Path, dry_run: bool,
                        settings_path: Path | None = None,
                        verify_enabled: bool = False) -> list[str]:
    """Build the least-privilege Claude Code command line for one application (S1).

    - ``--tools ""``: no built-in tools at all (no shell, files, web fetch, subagents).
    - ``--setting-sources ""``: none of the user's settings, plugins, hooks or skills.
    - ``--strict-mcp-config``: only the browser (+ verification helper) MCP servers.
    - ``--permission-mode dontAsk`` + ``--allowedTools``: anything not listed is denied.
    - ``--disallowedTools``: known-dangerous tools denied explicitly as well.
    - ``--settings``: the policy hooks (navigation/upload/value policy, dry-run guard,
      real-run submit approval). Required for every run; refuse to build without it.
    ``dry_run`` only changes the policy file, not the command line.
    """
    if settings_path is None:
        raise ValueError("apply sessions require the policy hook settings file")
    return [
        "claude",
        "--model", model,
        "-p",
        "--mcp-config", str(mcp_config_path),
        "--strict-mcp-config",
        "--tools", "",
        "--setting-sources", "",
        "--permission-mode", "dontAsk",
        "--allowedTools", ",".join(policy_mod.allowed_tool_names(verify_enabled)),
        "--disallowedTools", ",".join(policy_mod.denied_tool_names()),
        "--disable-slash-commands",
        "--no-session-persistence",
        "--settings", str(settings_path),
        "--output-format", "stream-json", "--verbose", "-",
    ]


def check_agent_cli(help_text: str | None = None) -> str | None:
    """Fail closed if the installed Claude Code cannot enforce the restrictions.

    Returns None when every required flag exists, else a reason string.
    """
    if help_text is None:
        try:
            help_text = subprocess.run(["claude", "--help"], capture_output=True, text=True, check=False,
                                       encoding="utf-8", errors="replace", timeout=60).stdout
        except (OSError, subprocess.SubprocessError) as e:
            return f"cannot run claude --help ({type(e).__name__})"
    required = ("--tools", "--setting-sources", "--strict-mcp-config", "--allowedTools",
                "--disallowedTools", "--settings", "dontAsk", "--disable-slash-commands")
    missing = [f for f in required if f not in (help_text or "")]
    return f"installed Claude Code lacks: {', '.join(missing)}" if missing else None


def verify_init_event(msg: dict, verify_enabled: bool) -> str | None:
    """Check the session's actual tool surface (stream-json init event). None = OK.

    Any deviation kills the session before the agent acts.
    """
    if msg.get("permissionMode") != "dontAsk":
        return f"permission mode is {msg.get('permissionMode')!r}"
    for tool in msg.get("tools") or []:
        if not tool.startswith((policy_mod.PLAYWRIGHT_PREFIX, policy_mod.VERIFY_PREFIX)):
            return f"unexpected tool {tool!r}"
    servers = {s.get("name"): s.get("status") for s in msg.get("mcp_servers") or []}
    if set(servers) - EXPECTED_MCP_SERVERS:
        return f"unexpected MCP servers {sorted(set(servers) - EXPECTED_MCP_SERVERS)}"
    if servers.get("playwright") != "connected":
        return "browser MCP server not connected"
    if verify_enabled and servers.get("applypilot_verify") != "connected":
        return "verification helper not connected"
    if not verify_enabled and "applypilot_verify" in servers:
        return "verification helper present but not enabled"
    for plugin in msg.get("plugins") or []:
        if not str(plugin.get("source", "")).endswith("@builtin"):
            return f"unexpected plugin {plugin.get('name')!r}"
    if msg.get("skills"):
        return "skills are loaded"
    return None


def audit_session(session_dir: Path, tool_uses: int, tool_use_ids: set[str] | None = None) -> str | None:
    """Post-session audit of the hook records. Returns an uncertain:* result or None.

    With ``tool_use_ids`` every streamed tool call must have its own PreToolUse decision
    (matched by id, not merely counted -- audit M3)."""
    audit = policy_mod.read_jsonl(session_dir / "audit.jsonl")
    violations = policy_mod.read_jsonl(session_dir / "violations.jsonl")
    pre = sum(1 for r in audit if r.get("phase") == "pre")
    if tool_uses and not (session_dir / "hook_alive").exists():
        return "uncertain:policy_hook_not_active"
    if pre < tool_uses:
        return "uncertain:policy_hook_not_enforced"
    if tool_use_ids:
        decided = {r.get("id") for r in audit if r.get("phase") == "pre"}
        if tool_use_ids - decided:
            return "uncertain:policy_hook_not_enforced"
    if (session_dir / policy_mod.GUARD_FAILED_FILE).exists():
        return "uncertain:network_guard_failed"
    if tool_uses and not _guard_verified(session_dir):
        return "uncertain:network_guard_not_verified"
    if violations:
        return "uncertain:policy_violation"
    return None


def _guard_verified(session_dir: Path) -> bool:
    try:
        status = json.loads((session_dir / policy_mod.GUARD_STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(status, dict) and status.get("active") is True and status.get("selftest") == "ok"


def submission_evidence(session_dir: Path) -> dict:
    """Causal submission evidence from launcher-owned records (audit H3 / R4).

    Every human-approved write is tracked by its request id through: approval granted ->
    ``release_attempt`` (journaled before Chrome may send it) -> ``released`` -> ``response``
    (the server's HTTP status, captured by the guard). ``released`` counts writes handed back to
    Chrome; ``ambiguous`` counts writes whose fate is unknown (unconfirmed release, an attempt
    with no outcome, or a release whose response was never observed) -- data may have reached
    the employer, so the job is never retried automatically.

    ``correlated`` (the only path to ``applied``) requires, for one approved submit click:
    exactly one released write between that click and the confirmation; that write's approval
    was requested after the click and matches its request id; the server answered it with a
    2xx/3xx status; and confirmation text was observed after that response. Timestamps alone,
    a different request, or a confirmation that precedes the response never correlate.
    """
    audit = policy_mod.read_jsonl(session_dir / "audit.jsonl")
    clicks = sorted(float(r.get("t", 0)) for r in audit if r.get("submit_approved"))
    grants = {r.get("request_id"): r for r in audit if r.get("net_approved") and r.get("request_id")}
    life: dict[str, dict[str, dict]] = {}
    for r in audit:
        if r.get("net_release") and r.get("request_id"):
            life.setdefault(r["request_id"], {})[r["net_release"]] = r
    released = {rid: d for rid, d in life.items() if "released" in d}
    ambiguous = 0
    for d in life.values():
        if "release_unconfirmed" in d or ("release_attempt" in d and "released" not in d):
            ambiguous += 1
        elif "released" in d and "response" not in d:
            ambiguous += 1  # sent, but the outcome of the request itself was never observed
    confirms = sorted(float(r.get("t", 0)) for r in audit if r.get("evidence") == "confirmation_text")

    def ok_response(d: dict) -> float | None:
        resp = d.get("response") or {}
        status = resp.get("status")
        return float(resp.get("t", 0)) if isinstance(status, int) and 200 <= status < 400 else None

    correlated = False
    for c in clicks:
        for rid, d in released.items():
            g = grants.get(rid)
            resp_t = ok_response(d)
            if g is None or resp_t is None or float(g.get("requested_at", 0)) < c:
                continue
            later = [x for x in confirms if x > resp_t]
            if not later:
                continue
            window_end = later[0]
            others = [o for o, od in released.items()
                      if o != rid and c <= float(od["released"].get("t", 0)) <= window_end]
            if not others:
                correlated = True
    return {"released": len(released), "ambiguous": ambiguous, "correlated": correlated,
            "approved": bool(clicks) and bool(released), "confirmed": bool(confirms)}


_SECRET_ENV_RE = re.compile(r"(API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)
_AGENT_AUTH_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


_AGENT_ENV_ALLOW = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "OS", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "USERNAME", "USER", "LOGNAME",
    "LANG", "LANGUAGE", "TZ", "TERM", "SHELL", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR",
})


def agent_env() -> dict:
    """Minimal environment for the agent process (audit R10: an allowlist, not a denylist).

    Only OS essentials, locale/proxy settings and Claude Code's own authentication variables
    are passed; ApplyPilot settings, LLM keys from .env, data-directory paths and every other
    variable are dropped.
    """
    env = {k: v for k, v in os.environ.items()
           if k in _AGENT_AUTH_ENV or (k.upper() in _AGENT_ENV_ALLOW and not _SECRET_ENV_RE.search(k))
           or k.upper().startswith("LC_")}
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env.pop("APPLYPILOT_GMAIL_CREDENTIALS", None)
    for k in list(env):
        # Playwright MCP reads these to widen file access / output roots / config (audit M3).
        if k.upper().startswith(("PLAYWRIGHT_MCP_", "PLAYWRIGHT_BROWSERS_PATH", "NODE_OPTIONS", "PWDEBUG")):
            env.pop(k, None)
    return env


def new_session_dir(worker_id: int) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    d = config.APP_DIR / "apply-sessions" / f"w{worker_id}_{ts}_{uuid.uuid4().hex[:6]}"
    (d / "uploads").mkdir(parents=True, exist_ok=True)
    return d


def build_session_policy(job: dict, session: Path, dry_run: bool,
                         verify_enabled: bool) -> policy_mod.SessionPolicy:
    hosts = policy_mod.session_allowed_hosts(job)
    uploads = sorted(str(p.resolve()) for p in (session / "uploads").glob("*.pdf"))
    sender_domains = sorted({h[2:] if h.startswith("*.") else policy_mod.base_domain(h) for h in hosts})
    return policy_mod.SessionPolicy(
        session_dir=str(session), dry_run=dry_run, allowed_hosts=hosts, upload_paths=uploads,
        verify_enabled=verify_enabled, verify_sender_domains=sender_domains,
        require_submit_approval=not dry_run,
        job_url=job["url"], asset_hosts=list(network_policy.STATIC_ASSET_HOSTS),
        require_network_guard=True, tenant_hosts=policy_mod.session_tenant_hosts(job),
    )


def sensitive_url_tokens(profile: dict) -> list[str]:
    """Applicant values that must never leave the browser inside a URL (kept in memory only)."""
    personal = (profile or {}).get("personal", {}) or {}
    out: list[str] = []
    for key in ("email", "phone", "full_name", "address"):
        v = str(personal.get(key) or "").strip()
        if key == "phone":
            v = re.sub(r"\D", "", v)
        if len(v) >= 4:
            out.append(v)
    return out



def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False,
            guard: NetworkGuard | None = None) -> tuple[str, int]:
    """Spawn a Claude Code session for one job application.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.

    ``guard`` is the launcher's network boundary for the Chrome on ``port``
    (started before Chrome, see worker_loop). Without a verified guard the
    agent is never started.
    """
    if guard is None:
        return "failed:network_guard_unavailable", 0
    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    # Per-session directory (outside the agent's cwd): policy, hook audit, uploads.
    session = new_session_dir(worker_id)
    upload_dir = session / "uploads"
    verify_enabled = verification_enabled()

    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
        upload_dir=upload_dir,
        verify_enabled=verify_enabled,
    )

    policy = build_session_policy(job, session, dry_run, verify_enabled)
    policy_path = policy.write()

    mcp_config_path = session / "mcp.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port, policy_path, verify_enabled)),
                               encoding="utf-8")
    settings_path = session / "settings.json"
    settings_path.write_text(json.dumps(policy_hook_settings(policy_path)), encoding="utf-8")
    cmd = build_agent_command(model, mcp_config_path, dry_run, settings_path, verify_enabled)

    env = agent_env()
    profile = config.load_profile()
    redact = Redactor(profile, secrets_file=session / "secrets.txt")
    guard.sensitive_tokens = sensitive_url_tokens(profile)

    worker_dir = reset_worker_dir(worker_id)

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None
    watchdog_fired = threading.Event()
    watchdog: threading.Timer | None = None
    agent_launched = False

    try:
        try:
            guard.on_approval_request = lambda req: add_event(
                f"[W{worker_id}] APPROVAL NEEDED: applypilot apply --approve {session} --nonce {req['nonce']}")
            guard.activate(policy, port)  # fail closed: raises unless both layers are verified
        except NetworkGuardError as e:
            add_event(f"[W{worker_id}] Network guard unavailable: {str(e)[:40]}")
            update_state(worker_id, status="failed", last_action="network guard unavailable")
            return "failed:network_guard_unavailable", int((time.time() - start) * 1000)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
        )
        agent_launched = True
        with _claude_lock:
            _claude_procs[worker_id] = proc
            _agent_started.add(worker_id)

        def _on_watchdog(p=proc):
            watchdog_fired.set()
            _kill_process_tree(p.pid)

        guard.on_failure = lambda p=proc: _kill_process_tree(p.pid)  # enforcement lost -> stop now
        if guard.failed.is_set():
            _kill_process_tree(proc.pid)

        watchdog = threading.Timer(APPLY_MAX_SECONDS, _on_watchdog)
        watchdog.daemon = True
        watchdog.start()

        proc.stdin.write(agent_prompt)
        proc.stdin.close()

        text_parts: list[str] = []
        final_text: str | None = None  # structured final reply from the "result" event
        final_is_error = False
        submit_ids: set[str] = set()  # tool_use ids of submit-like actions
        tool_use_ids: set[str] = set()
        init_seen = False
        guard_blocked_ids: set[str] = set()  # ids the dry-run guard refused
        tool_uses = 0
        security_failure: str | None = None
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(redact(log_header))

            for line in proc.stdout:
                if security_failure:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "system" and msg.get("subtype") == "init":
                        init_seen = True
                        security_failure = verify_init_event(msg, verify_enabled)
                        if security_failure:
                            lf.write(f"  !! security configuration not enforced: {security_failure}\n")
                            _kill_process_tree(proc.pid)
                            break
                    elif msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text":
                                text_parts.append(block["text"])
                                lf.write(redact(block["text"]) + "\n")
                            elif bt == "tool_use":
                                tool_uses += 1
                                tool_use_ids.add(str(block.get("id", f"anon-{tool_uses}")))
                                if not init_seen:
                                    # The tool surface must be verified before the agent acts.
                                    security_failure = "tool use before the init event"
                                    lf.write("  !! tool use before the init event; stopping session\n")
                                    _kill_process_tree(proc.pid)
                                    break
                                if tool_uses == 1 and not (session / "hook_alive").exists():
                                    # SessionStart runs before the first turn; no canary = no policy.
                                    security_failure = "policy hooks are not active"
                                    lf.write("  !! policy hooks are not active; stopping session\n")
                                    _kill_process_tree(proc.pid)
                                    break
                                name = (
                                    block.get("name", "")
                                    .replace("mcp__playwright__", "")
                                    .replace("mcp__gmail__", "gmail:")
                                )
                                inp = block.get("input", {})
                                if "url" in inp:
                                    desc = f"{name} {inp['url'][:60]}"
                                elif "ref" in inp:
                                    desc = f"{name} {inp.get('element', inp.get('text', ''))}"[:50]
                                elif "fields" in inp:
                                    desc = f"{name} ({len(inp['fields'])} fields)"
                                elif "paths" in inp:
                                    desc = f"{name} upload"
                                else:
                                    desc = name

                                lf.write(f"  >> {redact(desc)}\n")
                                if is_submit_action(block.get("name", ""), inp):
                                    submit_ids.add(block.get("id", f"anon-{len(submit_ids)}"))
                                    lf.write("  !! submit-like action\n")
                                ws = get_state(worker_id)
                                cur_actions = ws.actions if ws else 0
                                update_state(worker_id,
                                             actions=cur_actions + 1,
                                             last_action=desc[:35])
                    elif msg_type == "result":
                        stats = {
                            "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                            "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                            "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                            "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                            "cost_usd": msg.get("total_cost_usd", 0),
                            "turns": msg.get("num_turns", 0),
                        }
                        final_text = msg.get("result") or ""
                        final_is_error = bool(msg.get("is_error"))
                        text_parts.append(final_text or "")
                    elif msg_type == "user":
                        for block in msg.get("message", {}).get("content", []):
                            if (isinstance(block, dict) and block.get("type") == "tool_result"
                                    and "DRY RUN GUARD" in str(block.get("content", ""))):
                                guard_blocked_ids.add(block.get("tool_use_id", ""))
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(redact(line) + "\n")

        proc.wait(timeout=config.DEFAULTS["apply_timeout"])
        returncode = proc.returncode
        proc = None
        # Quiesce before reading evidence (audit H3): no byte leaves the browser from here on,
        # so the outcome is decided on a final record. An unconfirmed seal makes it uncertain (R4).
        if not guard.seal() and not security_failure:
            security_failure = "network guard seal could not be confirmed"


        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"claude_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(redact(output), encoding="utf-8")
        (session / "secrets.txt").unlink(missing_ok=True)  # codes are only needed for redaction

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        ev = submission_evidence(session)
        result = classify_session(
            final_text, final_is_error=final_is_error, dry_run=dry_run,
            submit_attempts=len(submit_ids),
            unblocked_submits=len(submit_ids - guard_blocked_ids),
            watchdog_fired=watchdog_fired.is_set(),
            interrupted=bool(returncode and returncode < 0),
            security_failure=security_failure, tool_uses=tool_uses,
            audit_result=audit_session(session, tool_uses, tool_use_ids),
            submit_approved=ev["approved"], confirmation_seen=ev["confirmed"],
            released_writes=ev["released"], ambiguous_writes=ev["ambiguous"],
            evidence_correlated=ev["correlated"],
        )

        label, _, detail = result.partition(":")
        add_event(f"[W{worker_id}] {label.upper()} ({elapsed}s): {(detail or job['title'])[:30]}")
        update_state(worker_id, status=label, last_action=f"{label.upper()} ({elapsed}s)")
        return result, duration_ms

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="uncertain", last_action=f"TIMEOUT ({elapsed}s)")
        return "uncertain:timeout", duration_ms
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        if not agent_launched:
            # The agent never started, so nothing can have been submitted.
            return f"failed:launch_error:{str(e)[:80]}", duration_ms
        return f"uncertain:error:{str(e)[:80]}", duration_ms
    finally:
        (session / "secrets.txt").unlink(missing_ok=True)
        approval.cancel_all(session)  # no grant may outlive its session (audit M4)
        if watchdog is not None:
            watchdog.cancel()
        with _claude_lock:
            _claude_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue",
    "not_eligible_location", "not_eligible_salary", "location_unclear",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

def _still_ready(job: dict, dry_run: bool | None = None) -> tuple[bool, list[str]]:
    """Re-evaluate a claimed job from its *current* row (audit R8).

    Ready only if this worker still owns the in-progress claim, nothing has recorded an
    application, the full readiness evaluation passes, and (for a real run) the human
    approval still stands. Callers continue with ``fresh_job`` -- never the claim snapshot."""
    from applypilot import readiness
    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (job["url"],)).fetchone()
    if row is None:
        return False, ["job row disappeared"]
    current = dict(row)
    if dry_run is None:
        dry_run = str(job.get("agent_id") or "").endswith(DRY_RUN_AGENT_SUFFIX)
    ownership: list[str] = []
    if current.get("apply_status") != "in_progress" or current.get("agent_id") != job.get("agent_id"):
        ownership.append("claim no longer held by this worker")
    if current.get("applied_at"):
        ownership.append("an application is already recorded for this job")
    if not dry_run and current.get("review_status") != "approved":
        ownership.append(f"human approval no longer stands ({current.get('review_status') or 'none'})")
    _ok, reasons, _ = readiness.evaluate(current, conn)
    # In flight because *we* hold the claim (checked above) -- that alone is not a blocker.
    reasons = ownership + [r for r in reasons if r != "application history: status is in_progress"]
    if reasons:
        conn.execute("UPDATE jobs SET readiness = 'blocked', readiness_reasons = ? WHERE url = ?",
                     (json.dumps(reasons), job["url"]))
        conn.commit()
    else:
        job.clear()
        job.update(current)  # continue with the row that was just evaluated
    return not reasons, reasons


def _release_interrupted(url: str, worker_id: int, dry_run: bool, reason: str,

                         owner: str | None = None) -> None:
    """Handle a job abandoned mid-flight (Ctrl+C or launcher error).

    If the agent never started, the claim is simply released. Otherwise the
    agent may already have submitted -- also in a dry run, whose guard is
    best-effort -- so the job is flagged for human review rather than silently
    returned to the queue. Only this worker's own claim (owner) is ever touched.
    """
    with _claude_lock:
        started = worker_id in _agent_started
    if started:
        mark_result(url, "needs_review", f"{reason}{':dry_run' if dry_run else ''}", owner=owner)
    else:
        release_lock(url, owner=owner)


def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id, dry_run=dry_run)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0
        with _claude_lock:
            _agent_started.discard(worker_id)

        chrome_proc = None
        guard = None
        try:
            # Fresh readiness check after the claim (ownership, approval, posting, documents,
            # duplicates, history), inside the release/exception block.
            ready_ok, ready_reasons = _still_ready(job, dry_run)
            if not ready_ok:
                add_event(f"[W{worker_id}] Not ready, released: {'; '.join(ready_reasons)[:60]}")
                release_lock(job["url"], owner=job["agent_id"])
                jobs_done += 1
                continue

            # The network guard (egress proxy) exists before Chrome, so Chrome never has a direct route.
            guard = NetworkGuard()
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless,
                                        proxy_port=guard.proxy_port,
                                        on_escape=lambda why, g=guard: g.fail(why))
            guard.kill_browser = lambda p=chrome_proc: _kill_process_tree(p.pid)

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                            model=model, dry_run=dry_run, guard=guard)

            outcome = record_outcome(job["url"], result, dry_run=dry_run,
                                     duration_ms=duration_ms, owner=job["agent_id"])
            if outcome == "ownership_lost":
                add_event(f"[W{worker_id}] CLAIM LOST, result not recorded: {job['title'][:30]}")
            if outcome == "applied" or (outcome == "dry_run" and result in ("applied", "dry_run")):
                applied += 1
            else:
                failed += 1
                if outcome == "needs_review":
                    add_event(f"[W{worker_id}] NEEDS REVIEW: {job['title'][:30]}")
            update_state(worker_id, jobs_applied=applied, jobs_failed=failed,
                         jobs_done=applied + failed)

        except KeyboardInterrupt:
            _release_interrupted(job["url"], worker_id, dry_run, "uncertain:interrupted",
                                 owner=job["agent_id"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            _release_interrupted(job["url"], worker_id, dry_run,
                                 f"uncertain:launcher_error:{str(e)[:80]}", owner=job["agent_id"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)
            if guard is not None:
                guard.stop()  # after Chrome is gone: the browser never outlives its boundary

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()

    # Fail closed: never start an agent the installed CLI cannot restrict (S1).
    problem = check_agent_cli()
    if problem:
        console.print(f"[red]Refusing to run: {problem}. The apply agent's tool restrictions "
                      "cannot be enforced.[/red]")
        return
    _install_exit_hooks()

    recovered = recover_stale_locks()
    if recovered:
        console.print(f"[yellow]Recovered {recovered} stale in-progress job(s); "
                      "real-run ones are marked needs_review.[/yellow]")
    reclassified = eligibility.classify_jobs()
    if reclassified:
        console.print(f"[dim]Eligibility (re)classified: {reclassified}[/dim]")
    if target_url:
        reason = explain_target(target_url, dry_run=dry_run)

        if reason:
            console.print(f"[red]Not applying to {target_url}:[/red] {reason}")
            return
    if dry_run:
        console.print("[bold yellow]DRY RUN: the agent is instructed not to submit, email sending is "
                      "disabled and a guard hook blocks submit-like actions. This is best-effort, "
                      "not a guarantee: supervise every dry run.[/bold yellow]")

    if target_url:
        workers = 1  # one exact target is never worked on by two workers (audit M6)
    if continuous:
        effective_limit = 0
        mode_label = "continuous"

    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Claude processes to skip current jobs
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    # Never start a worker with a zero quota: worker_loop reads 0 as
                    # "continuous", so a small batch could keep claiming jobs (audit M6).
                    workers = min(workers, effective_limit)
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode


                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        done_label = "dry runs completed" if dry_run else "applied"
        console.print(
            f"\n[bold]Done: {total_applied} {done_label}, {total_failed} not completed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
