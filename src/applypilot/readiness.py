"""Application readiness and the human review queue (fork).

A job is *ready* only when every automatic check passes:

1. the posting is verified active and fresh enough      (applypilot.verify)
2. geographic eligibility is verified                    (applypilot.eligibility)
3. the fit score meets ``min_score``                      (pipeline.yaml)
4. it is not a duplicate, and nothing in its duplicate group was applied to / is in flight
5. the tailored resume passed validation, exists, and is not stale
6. the cover letter passed validation (when ``require_cover_letter``)
7. application history allows it (no apply attempt pending review, no recent application
   to the same role at the same employer)

Screening questions without a verified reusable answer do not block readiness; they are
listed as *outstanding* and shown at review time (the apply agent stops on any of them).

Readiness is advisory for humans and binding for the launcher: real submissions need
``readiness = 'ready'`` **and** a human ``review_status = 'approved'``; the launcher
re-evaluates readiness right after claiming a job and releases it if anything changed.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from applypilot import config, eligibility, opsconfig, screening
from applypilot.database import get_connection
from applypilot.dates import age_days
from applypilot.dedupe import company_of, norm_title
from applypilot.verify import is_fresh

IN_FLIGHT = ("applied", "in_progress", "needs_review", "manual")
REVIEW_STATES = ("approved", "rejected", "deferred", "manual_submitted")
RECENT_SIMILAR_DAYS = 90


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _group_rows(conn: sqlite3.Connection, job: dict) -> list[dict]:
    root = job.get("duplicate_of") or job["url"]
    return [dict(r) for r in conn.execute(
        "SELECT url, apply_status, review_status FROM jobs WHERE (url = ? OR duplicate_of = ?) AND url != ?",
        (root, root, job["url"]))]


def evaluate(job: dict, conn: sqlite3.Connection | None = None, cfg: dict | None = None,
             now: datetime | None = None, profile: dict | None = None) -> tuple[bool, list[str], dict]:
    """(ready, blocking reasons, info). Pure apart from reading the DB and document files."""
    conn = conn or get_connection()
    cfg = cfg or opsconfig.load()
    reasons: list[str] = []
    info: dict = {}

    fresh, why = is_fresh(job, cfg, now)
    info["freshness"] = why
    if not fresh:
        reasons.append(why)

    status, version = job.get("eligibility_status"), job.get("eligibility_version")
    if status not in eligibility.ELIGIBLE_STATUSES or version not in (eligibility.CLASSIFIER_VERSION,
                                                                       eligibility.MANUAL_VERSION):
        reasons.append(f"eligibility not verified ({status or 'unclassified'})")
    elif not eligibility.decision_is_current(job):
        reasons.append("eligibility decision is stale (posting text, location, URL or rules changed); reclassify")


    score = job.get("fit_score")
    if score is None or score < int(cfg["min_score"]):
        reasons.append(f"fit score {score if score is not None else 'missing'} below {cfg['min_score']}")

    if job.get("duplicate_of"):
        reasons.append(f"duplicate of {job['duplicate_of']} ({job.get('duplicate_reason') or 'same opening'})")
    for other in _group_rows(conn, job):
        if other.get("apply_status") in IN_FLIGHT or other.get("review_status") == "manual_submitted":
            reasons.append(f"a duplicate posting is already {other.get('apply_status') or other.get('review_status')}")
            break

    if job.get("apply_status") in IN_FLIGHT:
        reasons.append(f"application history: status is {job['apply_status']}")
    if job.get("review_status") in ("rejected", "manual_submitted"):
        reasons.append(f"review status is {job['review_status']}")
    comp, title = company_of(job), norm_title(job.get("title"))
    if comp and title:
        for r in conn.execute("SELECT url, company, site, strategy, title, applied_at FROM jobs WHERE url != ? "
                              "AND applied_at IS NOT NULL", (job["url"],)):
            r = dict(r)
            if company_of(r) == comp and norm_title(r.get("title")) == title:
                days = age_days(r.get("applied_at"), now)
                if days is not None and days <= RECENT_SIMILAR_DAYS:
                    reasons.append(f"already applied to this role at this employer {days:.0f} days ago")
                    break

    # Documents: the exact validated bytes (text + the PDF rendered from it), built from the
    # current inputs. Any change, missing file or unbound PDF fails closed (audit R1).
    from applypilot.scoring import documents as D
    current = D.fingerprint(job, profile if profile is not None else config.load_profile())
    resume_problems = D.artifact_problems(job, "resume")
    reasons += resume_problems
    if (job.get("tailored_resume_path") and job.get("resume_validation") == "passed"
            and job.get("documents_for") != current):

        reasons.append("documents are stale (job text, resume, profile or evidence changed)")
    cover_problems = D.artifact_problems(job, "cover")
    info["cover_letter"] = "verified" if not cover_problems else "omitted (" + cover_problems[0] + ")"
    if not cover_problems and job.get("cover_for") != current:
        cover_problems = ["cover letter is stale (built from other inputs)"]
        info["cover_letter"] = "omitted (stale)"
    if cfg.get("require_cover_letter") and cover_problems:
        reasons += cover_problems
    # A human approval covers exactly the artifacts that were reviewed.
    if job.get("review_status") == "approved":
        try:
            approved_manifest = json.loads(job.get("review_artifacts") or "null")
        except ValueError:
            approved_manifest = None
        if approved_manifest != D.manifest(job):
            reasons.append("documents changed since human approval (review again)")

    try:
        cov = screening.coverage()
        info["screening_missing"] = cov["missing"]
    except ValueError as e:
        reasons.append(f"screening answers file invalid: {e}")
        info["screening_missing"] = ["(file invalid)"]
    return (not reasons), reasons, info


_CANDIDATES = """SELECT * FROM jobs WHERE fit_score IS NOT NULL AND tailored_resume_path IS NOT NULL
                 AND (apply_status IS NULL OR apply_status IN ('failed', 'dry_run'))"""


def run_prepare(conn: sqlite3.Connection | None = None, cfg: dict | None = None) -> dict:
    """Stage: (re)compute readiness for every prepared job; clear stale documents for regeneration."""
    conn = conn or get_connection()
    cfg = cfg or opsconfig.load()
    profile = config.load_profile()
    ready = blocked = stale = 0
    for row in conn.execute(_CANDIDATES).fetchall():
        job = dict(row)
        ok, reasons, info = evaluate(job, conn, cfg, profile=profile)
        if any(r.startswith(("documents are stale", "cover letter is stale")) for r in reasons):
            # The old files stay on disk (recorded as rejected=stale); tailor/cover regenerate them.
            resume_too = any(r.startswith("documents are stale") for r in reasons)
            if resume_too:
                conn.execute("UPDATE jobs SET resume_rejected_path = tailored_resume_path, tailored_resume_path = NULL,"
                             " resume_validation = 'stale', resume_sha = NULL, resume_pdf_path = NULL,"
                             " resume_pdf_sha = NULL, tailor_attempts = 0 WHERE url = ?", (job["url"],))
            conn.execute("UPDATE jobs SET cover_rejected_path = COALESCE(cover_letter_path, cover_rejected_path),"
                         " cover_letter_path = NULL, cover_validation = NULL, cover_sha = NULL, cover_pdf_path = NULL,"
                         " cover_pdf_sha = NULL, cover_for = NULL, cover_attempts = 0, review_artifacts = NULL"
                         " WHERE url = ?", (job["url"],))
            stale += 1
        screening_status = "supported" if not info.get("screening_missing") else "needs_review"
        conn.execute("UPDATE jobs SET readiness = ?, readiness_reasons = ?, readiness_at = ?, screening_status = ?,"
                     " screening_report = ? WHERE url = ?",
                     ("ready" if ok else "blocked", json.dumps(reasons), _now(), screening_status,
                      json.dumps(info), job["url"]))
        if ok:
            ready += 1
        else:
            blocked += 1
        # A human approval is void once the job is no longer ready.
        if not ok and job.get("review_status") == "approved":
            conn.execute("UPDATE jobs SET review_status = NULL, review_artifacts = NULL, review_note = ? WHERE url = ?",
                         ("approval withdrawn: " + "; ".join(reasons)[:200], job["url"]))
    conn.commit()
    return {"status": "ok", "ready": ready, "blocked": blocked, "stale": stale}


# --- human review actions ----------------------------------------------------------------------------

def _job(conn: sqlite3.Connection, url: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
    return dict(row) if row else None


def review(url: str, action: str, note: str = "", conn: sqlite3.Connection | None = None) -> tuple[bool, str]:
    """Human decision on one job: approve | reject | defer | manual_submitted | reset."""
    conn = conn or get_connection()
    job = _job(conn, url)
    if job is None:
        return False, "no job with that exact URL"
    if action == "approve":
        pending = dict(job, review_status=None)  # readiness before this decision
        ok, reasons, _ = evaluate(pending, conn)
        if not ok:
            return False, "not ready: " + "; ".join(reasons)
        from applypilot.scoring import documents as D
        conn.execute("UPDATE jobs SET review_status = 'approved', review_note = ?, reviewed_at = ?, readiness = 'ready',"
                     " review_artifacts = ? WHERE url = ?",
                     (note[:500], _now(), json.dumps(D.manifest(job)), url))

    elif action in ("reject", "defer"):
        conn.execute("UPDATE jobs SET review_status = ?, review_note = ?, reviewed_at = ?, review_artifacts = NULL"
                     " WHERE url = ?", ("rejected" if action == "reject" else "deferred", note[:500], _now(), url))
    elif action == "manual_submitted":
        if job.get("apply_status") == "applied" or job.get("applied_at"):
            return False, "already recorded as applied"
        if job.get("apply_status") == "in_progress":
            return False, "a worker currently holds this job; wait for it to finish before recording a manual submission"
        # The candidate applied by hand: record it so no automated or duplicate application follows.
        cur = conn.execute("UPDATE jobs SET review_status = 'manual_submitted', review_note = ?, reviewed_at = ?,"
                           " apply_status = 'applied', applied_at = ?, verification_confidence = 'human_reported',"
                           " apply_error = NULL WHERE url = ? AND applied_at IS NULL AND"
                           " COALESCE(apply_status, '') NOT IN ('in_progress', 'applied')",
                           (note[:500], _now(), _now(), url))
        if cur.rowcount != 1:
            conn.rollback()
            return False, "the job changed concurrently; nothing recorded"
    elif action == "reset":
        conn.execute("UPDATE jobs SET review_status = NULL, review_note = ?, reviewed_at = ?, review_artifacts = NULL"
                     " WHERE url = ?", (note[:500], _now(), url))
    else:
        return False, f"unknown action {action!r}"
    from applypilot.database import log_event
    log_event(conn, url, f"review_{action}", note, actor="cli")
    conn.commit()
    return True, action



def queue(conn: sqlite3.Connection | None = None, status: str = "ready", limit: int = 50) -> list[dict]:
    """Jobs for the review screen: 'ready' (awaiting decision), 'approved', 'blocked', 'deferred'."""
    conn = conn or get_connection()
    where = {
        "ready": "readiness = 'ready' AND review_status IS NULL",
        "approved": "review_status = 'approved'",
        "blocked": "readiness = 'blocked'",
        "deferred": "review_status = 'deferred'",
        "rejected": "review_status = 'rejected'",
    }.get(status, "readiness = 'ready' AND review_status IS NULL")
    rows = conn.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY fit_score DESC, readiness_at DESC",
                        ()).fetchall()
    from applypilot.ranking import rank
    return rank([dict(r) for r in rows])[:limit]
