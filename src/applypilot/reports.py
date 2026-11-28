"""Operational reports and exports (fork). Read-only.

Every report is a SQL query over the jobs table (and the run-history tables). Exports
contain posting data and pipeline state only -- never profile fields, credentials,
résumé text, approval files or session logs.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from applypilot import config
from applypilot.database import get_connection

_BASE = ["url", "title", "company", "site", "location", "salary"]
REPORTS: dict[str, dict] = {
    "new": {"desc": "Newly discovered jobs", "where": "COALESCE(first_seen_at, discovered_at) >= :since",
            "cols": _BASE + ["date_posted", "first_seen_at", "seen_count", "eligibility_status"],
            "sort": "first_seen_at"},
    "eligible": {"desc": "Active, eligible, non-duplicate jobs",
                 "where": "eligibility_status LIKE 'eligible%' AND duplicate_of IS NULL "
                          "AND COALESCE(posting_status, '') NOT IN ('closed', 'removed') "
                          "AND (apply_status IS NULL OR apply_status IN ('failed', 'dry_run'))",
                 "cols": _BASE + ["eligibility_status", "eligibility_tier", "fit_score", "posting_status"],
                 "sort": "fit_score"},
    "top": {"desc": "High-scoring opportunities (score >= min)", "where": "fit_score >= :min_score "
            "AND duplicate_of IS NULL AND COALESCE(posting_status, '') NOT IN ('closed', 'removed')",
            "cols": _BASE + ["fit_score", "eligibility_status", "posting_status", "readiness"], "sort": "fit_score"},
    "duplicates": {"desc": "Duplicate postings", "where": "duplicate_of IS NOT NULL",
                   "cols": ["url", "title", "company", "site", "duplicate_of", "duplicate_reason"], "sort": "company"},
    "expired": {"desc": "Closed, removed, inaccessible or uncertain postings",
                "where": "posting_status IN ('closed', 'removed', 'inaccessible', 'uncertain')",
                "cols": _BASE + ["posting_status", "posting_reason", "posting_checked_at"], "sort": "posting_checked_at"},
    "prepared": {"desc": "Application-ready jobs", "where": "readiness = 'ready'",
                 "cols": _BASE + ["fit_score", "review_status", "tailored_resume_path", "cover_letter_path",
                                  "screening_status"], "sort": "fit_score"},
    "validation": {"desc": "Document validation failures",
                   "where": "resume_validation IN ('rejected', 'needs_review', 'stale') "
                            "OR cover_validation IN ('rejected', 'needs_review')",
                   "cols": ["url", "title", "company", "resume_validation", "resume_validation_reasons",
                            "cover_validation", "cover_validation_reasons", "resume_rejected_path"],
                   "sort": "title"},
    "review": {"desc": "Awaiting human review",
               "where": "(readiness = 'ready' AND review_status IS NULL) OR apply_status = 'needs_review' "
                        "OR eligibility_status = 'review' OR resume_validation = 'needs_review' "
                        "OR posting_status = 'uncertain'",
               "cols": _BASE + ["readiness", "apply_status", "apply_error", "eligibility_status",
                                "resume_validation", "posting_status"], "sort": "fit_score"},
    "applied": {"desc": "Confirmed applications", "where": "apply_status = 'applied'",
                "cols": _BASE + ["applied_at", "verification_confidence", "review_status"], "sort": "applied_at"},
    "uncertain": {"desc": "Uncertain application outcomes (needs_review)", "where": "apply_status = 'needs_review'",
                  "cols": _BASE + ["apply_error", "last_attempted_at"], "sort": "last_attempted_at"},
    "blocked": {"desc": "Prepared but blocked jobs and why", "where": "readiness = 'blocked'",
                "cols": ["url", "title", "company", "fit_score", "readiness_reasons"], "sort": "fit_score"},
}
SORTABLE = {"fit_score", "first_seen_at", "date_posted", "company", "title", "applied_at", "posting_checked_at",
            "last_attempted_at", "url"}


def run_report(name: str, *, since_days: int = 7, min_score: int = 7, sort: str | None = None,
               desc: bool = True, limit: int = 200, filter_text: str | None = None,
               conn: sqlite3.Connection | None = None) -> list[dict]:
    spec = REPORTS[name]
    conn = conn or get_connection()
    cols = ", ".join(spec["cols"])
    sort = sort if sort in SORTABLE else spec["sort"]
    params: dict = {"since": (datetime.now(UTC) - timedelta(days=since_days)).isoformat(),
                    "min_score": min_score, "limit": limit}
    where = f"({spec['where']})"
    if filter_text:
        where += " AND (title LIKE :f OR company LIKE :f OR site LIKE :f OR location LIKE :f)"
        params["f"] = f"%{filter_text}%"
    sql = (f"SELECT {cols} FROM jobs WHERE {where} ORDER BY {sort} IS NULL, {sort} "
           f"{'DESC' if desc else 'ASC'} LIMIT :limit")
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def inference_report(conn: sqlite3.Connection | None = None) -> dict:
    """LLM usage/outcomes by model, plus recent daily-run stage durations."""
    conn = conn or get_connection()
    out: dict = {"scoring": [], "tailoring": [], "cover": [], "daily_stage_seconds": {}}
    for r in conn.execute("SELECT COALESCE(score_model, '(unrecorded)') m, COUNT(*) n, AVG(fit_score) avg,"
                          " SUM(COALESCE(score_attempts, 0)) attempts FROM jobs WHERE fit_score IS NOT NULL GROUP BY m"):
        out["scoring"].append(dict(r))
    out["scoring_failures"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE score_error IS NOT NULL AND"
                                           " fit_score IS NULL").fetchone()[0]
    for key, col, val in (("tailoring", "resume_model", "resume_validation"), ("cover", "cover_model", "cover_validation")):
        for r in conn.execute(f"SELECT COALESCE({col}, '(unrecorded)') m, COALESCE({val}, '(none)') v, COUNT(*) n "
                              f"FROM jobs WHERE {col} IS NOT NULL OR {val} IS NOT NULL GROUP BY m, v"):
            out[key].append(dict(r))
    logs = sorted(config.LOG_DIR.glob("daily-*.jsonl"))[-7:] if config.LOG_DIR.exists() else []
    for p in logs:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") == "stage_end":
                out["daily_stage_seconds"].setdefault(ev["stage"], []).append(ev.get("seconds"))
    return out


def failures_report(conn: sqlite3.Connection | None = None, limit: int = 20) -> dict:
    conn = conn or get_connection()
    runs = [dict(r) for r in conn.execute("SELECT run_id, kind, started_at, finished_at, status, exit_code, summary "
                                          "FROM pipeline_runs ORDER BY started_at DESC LIMIT ?", (limit,))]
    disc = [dict(r) for r in conn.execute("SELECT source, started_at, status, new_count, seen_count, error FROM "
                                          "discovery_runs WHERE status != 'ok' ORDER BY started_at DESC LIMIT ?",
                                          (limit,))]
    return {"pipeline_runs": runs, "discovery_failures": disc}


def to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    for r in rows:
        # Spreadsheet formula injection: neutralise cells starting with = + - @
        w.writerow({k: ("'" + v if isinstance(v, str) and v[:1] in "=+-@" else v) for k, v in r.items()})
    return buf.getvalue()


def to_json(rows) -> str:
    return json.dumps(rows, indent=2, default=str)
