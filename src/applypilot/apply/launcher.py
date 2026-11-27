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

def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
            "gmail": {
                "command": "npx",
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
        }
    }


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

# Apply status lifecycle
#   NULL / 'failed' (attempts < max)  -> eligible for the queue
#   'dry_run'                         -> eligible for real runs; skipped by dry-run batches
#   'in_progress'                     -> claimed by a worker (agent_id marks dry-run claims)
#   'applied'                         -> terminal; only set by a real run or --mark-applied
#   'needs_review'                    -> outcome unknown (agent may have submitted); a human
#                                        must resolve it with --mark-applied / --mark-failed
#   'manual'                          -> ATS requires manual handling
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
          AND o.application_url IS NOT NULL
          AND o.application_url = j.application_url
          AND o.apply_status IN ({",".join(repr(s) for s in DUPLICATE_BLOCKING_STATUSES)})
    )"""


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _select_target(conn, target_url: str):
    """Find an explicitly requested job. Exact URL matches win over substring matches."""
    like = f"%{_like_escape(target_url.split('?')[0].rstrip('/'))}%"
    return conn.execute(f"""
        SELECT {_JOB_COLUMNS}
        FROM jobs AS j
        WHERE (j.url = ? OR j.application_url = ?
               OR j.application_url LIKE ? ESCAPE '\\' OR j.url LIKE ? ESCAPE '\\')
          AND j.tailored_resume_path IS NOT NULL
          AND (j.apply_status IS NULL OR j.apply_status IN ('failed', 'dry_run'))
          AND {_ELIGIBLE_CLAUSE}
          AND {_NO_DUPLICATE_CLAUSE}
        ORDER BY (j.url = ? OR j.application_url = ?) DESC, j.url
        LIMIT 1
    """, (target_url, target_url, like, like, target_url, target_url)).fetchone()


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
          AND (j.apply_attempts IS NULL OR j.apply_attempts < ?)
          AND j.fit_score >= ?
          {site_clause}
          {url_clauses}
          AND {_ELIGIBLE_CLAUSE}
          AND {_NO_DUPLICATE_CLAUSE}
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
            row = _select_target(conn, target_url) if target_url else _select_next(conn, min_score, dry_run)
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


def explain_target(target_url: str) -> str | None:
    """Why an explicit --url target cannot be acquired, or None if it can (read-only)."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT url, apply_status, tailored_resume_path, eligibility_status, eligibility_reason,
               eligibility_version
        FROM jobs WHERE url = ? OR application_url = ?
    """, (target_url, target_url)).fetchall()
    if not rows:
        return None  # may still match by substring; acquire_job decides
    row = rows[0]
    status = row["eligibility_status"]
    if status not in eligibility.ELIGIBLE_STATUSES:
        return (f"eligibility is '{status or 'unclassified'}' ({row['eligibility_reason'] or 'not evaluated'}). "
                "Review it with `applypilot eligibility`; approve with --approve URL if appropriate.")
    if row["apply_status"] in DUPLICATE_BLOCKING_STATUSES:
        return f"apply status is '{row['apply_status']}'"
    if not row["tailored_resume_path"]:
        return "no tailored resume yet (run `applypilot run tailor`)"
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
        if result == "uncertain:dry_run_submit_not_blocked":
            status, note, outcome = "needs_review", result, "needs_review"
        else:
            note = None if result in ("applied", "dry_run") else f"dry_run:{result}"
            status, outcome = "dry_run", "dry_run"
        written = mark_result(url, status, note, duration_ms=duration_ms, owner=owner)
        return outcome if written else "ownership_lost"
    if result == "dry_run":
        result = "uncertain:unexpected_dry_run_result"
    if result == "applied":
        status, error, permanent, outcome = "applied", None, False, "applied"
    elif result.startswith("uncertain:"):
        status, error, permanent, outcome = "needs_review", result, False, "needs_review"
    else:
        reason = result.split(":", 1)[-1] if ":" in result else result
        status, error, permanent, outcome = "failed", reason, _is_permanent_failure(result), "failed"
    written = mark_result(url, status, error, permanent=permanent,
                          duration_ms=duration_ms, owner=owner)
    return outcome if written else "ownership_lost"


def recover_stale_locks(max_age_seconds: int | None = None) -> int:
    """Resolve 'in_progress' claims left behind by crashed or killed workers.

    Dry-run claims are released back to the queue. Real claims become
    'needs_review': the agent may have submitted before dying, so retrying
    automatically could produce a duplicate application.

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
    released = conn.execute(f"""
        UPDATE jobs SET apply_status = NULL, agent_id = NULL
        WHERE {stale} AND agent_id LIKE ?
    """, (cutoff, own, f"%{DRY_RUN_AGENT_SUFFIX}")).rowcount
    flagged = conn.execute(f"""
        UPDATE jobs SET apply_status = 'needs_review',
                       apply_error = 'uncertain:interrupted_stale_lock', agent_id = NULL
        WHERE {stale}
    """, (cutoff, own)).rowcount
    conn.commit()
    return released + flagged


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


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Never touches applied, in-progress or needs-review jobs: a needs-review
    job may already have been submitted and must be resolved by a human.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status IS NOT NULL
          AND apply_status NOT IN ('applied', 'in_progress', 'needs_review')
    """)
    conn.commit()
    return cursor.rowcount


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
                     watchdog_fired: bool = False, interrupted: bool = False) -> str:
    """Combine the agent's final reply with launcher observations into one result.

    The agent's own claim is never enough on its own:
    - a session killed by the watchdog / interrupted, or one that ended in an
      agent error, is uncertain unless it produced a clean result;
    - in a real run, any non-APPLIED result after a submit-like action means
      a submission may have happened -> uncertain (human review);
    - in a dry run, a submit-like action the guard did not block means a real
      submission may have happened -> 'uncertain:dry_run_submit_not_blocked'.
    """
    result = parse_agent_result(final_text)
    if watchdog_fired:
        result = "uncertain:watchdog_timeout"
    elif interrupted and result.startswith("uncertain:"):
        result = "uncertain:interrupted"
    elif final_is_error and not result.startswith("uncertain:"):
        result = f"uncertain:agent_error_after_{result.replace(':', '_')}"
    if dry_run:
        if unblocked_submits:
            return "uncertain:dry_run_submit_not_blocked"
        return result
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
    return False


GMAIL_ALWAYS_DISALLOWED = (
    "mcp__gmail__modify_email", "mcp__gmail__delete_email", "mcp__gmail__download_attachment",
    "mcp__gmail__batch_modify_emails", "mcp__gmail__batch_delete_emails",
    "mcp__gmail__create_label", "mcp__gmail__update_label", "mcp__gmail__delete_label",
    "mcp__gmail__get_or_create_label", "mcp__gmail__list_email_labels",
    "mcp__gmail__create_filter", "mcp__gmail__list_filters", "mcp__gmail__get_filter",
    "mcp__gmail__delete_filter", "mcp__gmail__draft_email",
)
GMAIL_DRY_RUN_DISALLOWED = ("mcp__gmail__send_email",)


def dry_run_guard_settings() -> dict:
    """Claude Code settings that install the dry-run PreToolUse guard hook."""
    command = f'"{sys.executable}" -m applypilot.apply.dry_run_guard'
    return {"hooks": {"PreToolUse": [
        {"matcher": "mcp__playwright__.*|mcp__gmail__.*",
         "hooks": [{"type": "command", "command": command}]},
    ]}}


def build_agent_command(model: str, mcp_config_path: Path, dry_run: bool,
                        settings_path: Path | None = None) -> list[str]:
    """Build the Claude Code command line for one application session.

    --strict-mcp-config keeps the user's own MCP servers out of the agent.
    Dry runs additionally block email sending outright and install the
    dry-run guard hook (settings_path) that refuses submit-like tool calls.
    """
    disallowed = list(GMAIL_ALWAYS_DISALLOWED)
    if dry_run:
        disallowed += GMAIL_DRY_RUN_DISALLOWED
    cmd = [
        "claude",
        "--model", model,
        "-p",
        "--mcp-config", str(mcp_config_path),
        "--strict-mcp-config",
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--disallowedTools", ",".join(disallowed),
    ]
    if dry_run:
        if settings_path is None:
            raise ValueError("dry runs require the guard settings file")
        cmd += ["--settings", str(settings_path)]
    cmd += ["--output-format", "stream-json", "--verbose", "-"]
    return cmd


def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False) -> tuple[str, int]:
    """Spawn a Claude Code session for one job application.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    # Build the prompt
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
    )

    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    settings_path = None
    if dry_run:
        settings_path = config.APP_DIR / f".apply-dry-run-settings-{worker_id}.json"
        settings_path.write_text(json.dumps(dry_run_guard_settings()), encoding="utf-8")
    cmd = build_agent_command(model, mcp_config_path, dry_run, settings_path)

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

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

        watchdog = threading.Timer(APPLY_MAX_SECONDS, _on_watchdog)
        watchdog.daemon = True
        watchdog.start()

        proc.stdin.write(agent_prompt)
        proc.stdin.close()

        text_parts: list[str] = []
        final_text: str | None = None  # structured final reply from the "result" event
        final_is_error = False
        submit_ids: set[str] = set()  # tool_use ids of submit-like actions
        guard_blocked_ids: set[str] = set()  # ids the dry-run guard refused
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text":
                                text_parts.append(block["text"])
                                lf.write(block["text"] + "\n")
                            elif bt == "tool_use":
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

                                lf.write(f"  >> {desc}\n")
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
                        text_parts.append(final_text)
                    elif msg_type == "user":
                        for block in msg.get("message", {}).get("content", []):
                            if (isinstance(block, dict) and block.get("type") == "tool_result"
                                    and "DRY RUN GUARD" in str(block.get("content", ""))):
                                guard_blocked_ids.add(block.get("tool_use_id", ""))
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(line + "\n")

        proc.wait(timeout=config.DEFAULTS["apply_timeout"])
        returncode = proc.returncode
        proc = None

        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"claude_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        result = classify_session(
            final_text, final_is_error=final_is_error, dry_run=dry_run,
            submit_attempts=len(submit_ids),
            unblocked_submits=len(submit_ids - guard_blocked_ids),
            watchdog_fired=watchdog_fired.is_set(),
            interrupted=bool(returncode and returncode < 0),
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

def _release_interrupted(url: str, worker_id: int, dry_run: bool, reason: str,
                         owner: str | None = None) -> None:
    """Handle a job abandoned mid-flight (Ctrl+C or launcher error).

    If the agent never started, or this is a dry run, the claim is simply
    released. Otherwise the agent may already have submitted, so the job is
    flagged for human review rather than silently returned to the queue.
    Only this worker's own claim (owner) is ever touched.
    """
    with _claude_lock:
        started = worker_id in _agent_started
    if started and not dry_run:
        mark_result(url, "needs_review", reason, owner=owner)
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
        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                            model=model, dry_run=dry_run)

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
    _install_exit_hooks()

    recovered = recover_stale_locks()
    if recovered:
        console.print(f"[yellow]Recovered {recovered} stale in-progress job(s); "
                      "real-run ones are marked needs_review.[/yellow]")
    reclassified = eligibility.classify_jobs()
    if reclassified:
        console.print(f"[dim]Eligibility (re)classified: {reclassified}[/dim]")
    if target_url:
        reason = explain_target(target_url)
        if reason:
            console.print(f"[red]Not applying to {target_url}:[/red] {reason}")
            return
    if dry_run:
        console.print("[bold yellow]DRY RUN: the agent is instructed not to submit, email sending is "
                      "disabled and a guard hook blocks submit-like actions. This is best-effort, "
                      "not a guarantee: supervise every dry run.[/bold yellow]")

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
