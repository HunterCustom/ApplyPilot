"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client
from applypilot.scoring.candidate_evidence import load_candidate_evidence, format_candidate_evidence

log = logging.getLogger(__name__)


# â”€â”€ Scoring Prompt â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume, optional user-verified evidence, and job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)
- Only credit claims explicitly supported by the resume or verified evidence; never invent credentials, employers, or years.
- Distinguish professional work from personal projects. Personal projects show transferable skills but do not establish years of paid experience.
- Identify core requirements vs preferred qualifications. Do not assume every preference is mandatory.
- In REASONING briefly identify demonstrated skills, meaningful gaps, and any mandatory requirement that needs verification.
- Job location and remote eligibility must be independently verified; do not equate remote with eligibility.

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response

    # Exactly one structured score, in range, on its own line (audit R11): conflicting or
    # out-of-range values are never clamped into a valid score.
    score_lines = [ln.strip() for ln in response.split("\n") if re.match(r"\s*\**\s*SCORE\s*\**\s*:", ln, re.IGNORECASE)]
    if len(score_lines) == 1:
        m = re.fullmatch(r"\**\s*SCORE\s*\**\s*:\s*\**\s*(10|[1-9])\s*\**", score_lines[0], re.IGNORECASE)
        score = int(m.group(1)) if m else 0
    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            continue
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


SCORE_PROMPT_VERSION = "2"  # bump when SCORE_PROMPT or the parse rules change (recorded per score)
SCORE_TEMPERATURE = 0.2


def score_config(evidence_text: str, resume_text: str = "") -> dict:
    """What produced a score: recorded with it for traceability (model is recorded separately).

    ``evidence_sha`` / ``resume_sha`` identify the exact candidate evidence and resume versions."""
    import hashlib
    return {"prompt_version": SCORE_PROMPT_VERSION,
            "prompt_sha": hashlib.sha256(SCORE_PROMPT.encode()).hexdigest()[:12],
            "temperature": SCORE_TEMPERATURE,
            "max_tokens": max(256, int(os.environ.get("APPLYPILOT_SCORE_MAX_TOKENS", "2048"))),
            "evidence": bool(evidence_text),
            "evidence_sha": hashlib.sha256(evidence_text.encode("utf-8")).hexdigest()[:16] if evidence_text else None,
            "resume_sha": hashlib.sha256(resume_text.encode("utf-8")).hexdigest()[:16] if resume_text else None}


def score_job(resume_text: str, job: dict, evidence_text: str = "") -> dict:
    """Score one job, retrying empty, malformed and failed LLM responses.

    An unsuccessful evaluation returns score=0 as an internal error marker;
    run_scoring never persists that marker to the jobs table.
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )
    extra_evidence = f"\n\nADDITIONAL USER-VERIFIED EVIDENCE:\n{evidence_text}" if evidence_text else ""
    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}{extra_evidence}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    max_tokens = max(256, int(os.environ.get("APPLYPILOT_SCORE_MAX_TOKENS", "2048")))
    max_attempts = max(1, int(os.environ.get("APPLYPILOT_SCORE_ATTEMPTS", "3")))
    last_error = "Unknown scoring failure"

    for attempt in range(1, max_attempts + 1):
        try:
            response = get_client().chat(messages, max_tokens=max_tokens, temperature=SCORE_TEMPERATURE)
            if not response or not response.strip():
                raise ValueError("Empty LLM response")
            if not re.search(r"(?im)^\s*SCORE:\s*(?:10|[1-9])\s*$", response):
                raise ValueError("Missing or invalid SCORE field")
            if len(re.findall(r"(?im)^\s*\**\s*SCORE\s*\**\s*:", response)) != 1:
                raise ValueError("More than one SCORE field (conflicting output)")
            if not re.search(r"(?im)^\s*REASONING:\s*\S", response):
                raise ValueError("Missing REASONING field")
            parsed = _parse_score_response(response)
            return parsed
        except Exception as exc:
            last_error = str(exc)
            log.warning(
                "Scoring attempt %d/%d failed for '%s': %s",
                attempt, max_attempts, job.get("title", "?"), last_error,
            )
    log.error("All scoring attempts failed for '%s'", job.get("title", "?"))
    return {"score": 0, "keywords": "", "reasoning": f"LLM error: {last_error}"}



def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score jobs, committing each successful evaluation immediately.

    Failed evaluations remain unscored and are eligible for subsequent runs.
    """
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    evidence_text = format_candidate_evidence(load_candidate_evidence())
    conn = get_connection()

    # Deterministic geographic eligibility first: ineligible jobs never reach the LLM.
    from applypilot.eligibility import SQL_NOT_INELIGIBLE, classify_jobs
    classified = classify_jobs(conn)
    if classified:
        log.info("Eligibility classified %d jobs: %s", sum(classified.values()), classified)

    if rescore:
        # Same filters as ordinary selection (audit R11), except that scored rows are included;
        # the previous score is preserved in score_history before it is replaced.
        query = (f"SELECT * FROM jobs WHERE full_description IS NOT NULL AND {SQL_NOT_INELIGIBLE}"
                 " AND duplicate_of IS NULL AND (posting_status IS NULL OR posting_status NOT IN ('closed', 'removed'))")
        if limit > 0:
            query += f" LIMIT {int(limit)}"
        jobs = conn.execute(query).fetchall()
    else:
        # Load all candidates before limiting, so ranking applies globally.
        # Without a user priority.yaml, preserve the original database ordering.
        from applypilot.scoring.priority import load_priority_config, prioritize_jobs
        priority_config = load_priority_config()
        jobs = get_jobs_by_stage(
            conn=conn, stage="pending_score", limit=0 if priority_config else limit
        )
        if priority_config:
            jobs = prioritize_jobs(jobs, priority_config)
            if limit > 0:
                jobs = jobs[:limit]

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    scored = 0
    errors = 0

    try:
        model = get_client().model
    except Exception:  # noqa: BLE001 -- recorded as unknown, scoring itself reports LLM errors
        model = "unknown"
    cfg_json = json.dumps(score_config(evidence_text, resume_text), sort_keys=True)

    for index, job in enumerate(jobs, start=1):
        result = score_job(resume_text, job, evidence_text)
        if result["score"] == 0:
            errors += 1
            # Not a score: the error and attempt count are kept so the job is retried (resumable)
            # up to the pending_score attempt cap, and the failure is visible in reports.
            conn.execute("UPDATE jobs SET score_error = ?, score_attempts = COALESCE(score_attempts, 0) + 1"
                         " WHERE url = ?", (result["reasoning"][:300], job["url"]))
            conn.commit()
            log.warning("[%d/%d] not saved (retryable): %s", index, len(jobs), job.get("title", "?"))
            continue

        now = datetime.now(timezone.utc).isoformat()
        if job.get("fit_score") is not None:
            # Rescoring never loses history.
            conn.execute("INSERT INTO score_history (url, fit_score, score_reasoning, scored_at, score_model,"
                         " score_config, replaced_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (job["url"], job.get("fit_score"), job.get("score_reasoning"), job.get("scored_at"),
                          job.get("score_model"), job.get("score_config"), now))
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ?, score_model = ?, score_config = ?,"
            " score_error = NULL, score_attempts = COALESCE(score_attempts, 0) + 1 WHERE url = ?",
            (result["score"], f"{result['keywords']}\n{result['reasoning']}", now, model, cfg_json, job["url"]),
        )
        conn.commit()

        scored += 1
        log.info("[%d/%d] score=%d saved: %s", index, len(jobs), result["score"], job.get("title", "?")[:60])

    elapsed = time.time() - t0
    log.info("Done: %d saved, %d errors in %.1fs", scored, errors, elapsed)
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]
    return {"scored": scored, "errors": errors, "elapsed": elapsed, "distribution": distribution}

