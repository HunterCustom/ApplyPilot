"""Cover letter generation: LLM-powered, profile-driven, with validation.

Generates concise, engineering-voice cover letters tailored to specific job
postings. All personal data (name, skills, achievements) comes from the user's
profile at runtime. No hardcoded personal information.
"""

import json
import logging
import time
from datetime import datetime, timezone

from applypilot.config import COVER_LETTER_DIR, RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client
from applypilot.scoring.validator import (
    BANNED_WORDS,
    LLM_LEAK_PHRASES,
    sanitize_text,
    validate_cover_letter,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


# ── Prompt Builder (profile-driven) ──────────────────────────────────────

def _build_cover_letter_prompt(profile: dict) -> str:
    """Build the cover letter system prompt from the user's profile.

    All personal data, skills, and sign-off name come from the profile.
    """
    personal = profile.get("personal", {})
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Preferred name for the sign-off (falls back to full name)
    sign_off_name = personal.get("preferred_name") or personal.get("full_name", "")

    # Flatten all allowed skills
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "the tools listed in the resume"

    # Real metrics from resume_facts
    real_metrics = resume_facts.get("real_metrics", [])
    preserved_projects = resume_facts.get("preserved_projects", [])

    # Build achievement examples for the prompt
    projects_hint = ""
    if preserved_projects:
        projects_hint = f"\nKnown projects to reference: {', '.join(preserved_projects)}"

    metrics_hint = ""
    if real_metrics:
        metrics_hint = f"\nReal metrics to use: {', '.join(real_metrics)}"

    # Build the full banned list from the validator so the prompt stays in sync
    # with what will actually be rejected — the validator checks all of these.
    all_banned = ", ".join(f'"{w}"' for w in BANNED_WORDS)
    leak_banned = ", ".join(f'"{p}"' for p in LLM_LEAK_PHRASES)

    return f"""Write a cover letter for {sign_off_name}. The goal is to get an interview.

STRUCTURE: 3 short paragraphs. Under 250 words. Every sentence must earn its place.

PARAGRAPH 1 (2-3 sentences): Open with a specific thing YOU built that solves THEIR problem. Not "I'm excited about this role." Not "This role aligns with my experience." Start with the work.

PARAGRAPH 2 (3-4 sentences): Pick 2 achievements from the resume that are MOST relevant to THIS job. Use numbers. Frame as solving their problem, not listing your accomplishments.{projects_hint}{metrics_hint}

PARAGRAPH 3 (1-2 sentences): One specific thing about the company from the job description (a product, a technical challenge, a team structure). Then close. "Happy to walk through any of this in more detail." or "Let's discuss." Nothing else.

BANNED WORDS AND PHRASES (automated validator rejects ANY of these — do not use even once):
{all_banned}

ALSO BANNED (meta-commentary the validator catches):
{leak_banned}

BANNED PUNCTUATION: No em dashes (—) or en dashes (–). Use commas or periods.

VOICE:
- Write like a real engineer emailing someone they respect. Not formal, not casual. Just direct.
- NEVER narrate or explain what you're doing. BAD: "This demonstrates my commitment to X." GOOD: Just state the fact and move on.
- NEVER hedge. BAD: "might address some of your challenges." GOOD: "solves the same problem your team is facing."
- Every sentence should contain either a number, a tool name, or a specific outcome. If it doesn't, cut it.
- Read it out loud. If it sounds like a robot wrote it, rewrite it.

FABRICATION = INSTANT REJECTION:
The candidate's real tools are ONLY: {skills_str}.
Do NOT mention ANY tool not in this list. If the job asks for tools not listed, talk about the work you did, not the tools.

Sign off: just "{sign_off_name}"

Output ONLY the letter text. No subject lines. No "Here is the cover letter:" preamble. No notes after the sign-off.
Start DIRECTLY with "Dear Hiring Manager," and end with the name."""


# ── Helpers ──────────────────────────────────────────────────────────────

def _strip_preamble(text: str) -> str:
    """Remove LLM preamble before 'Dear Hiring Manager,' if present.

    Gemini and other models sometimes output "Here is the cover letter:" or
    similar meta-commentary before the actual letter text. Strip everything
    before the first occurrence of "Dear" so the validator's start-check passes.
    """
    dear_idx = text.lower().find("dear")
    if dear_idx > 0:
        return text[dear_idx:]
    return text


# ── Core Generation ──────────────────────────────────────────────────────

def generate_cover_letter(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
    sources=None, evidence_text: str = "", min_words: int = 120, max_words: int = 320,
) -> tuple[str, dict]:
    """Generate a cover letter with fresh context on each retry + auto-sanitize.

    Same design as tailor_resume: fresh conversation per attempt, issues noted
    in the prompt, no conversation history stacking.

    Args:
        resume_text:      The candidate's resume text (base or tailored).
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".

    Returns:
        (letter, report). ``report["status"] == "approved"`` is the only usable outcome
        (fork): a letter that still fails validation after the last attempt is returned
        as ``rejected`` and is never marked ready. Grounding (scoring.factcheck) runs in
        every mode: no unsupported technologies, certifications, metrics or promises.
    """
    from applypilot.scoring.factcheck import build_sources, cover_letter_problems
    if sources is None:
        sources = build_sources(resume_text, profile, {})
    report: dict = {"attempts": 0, "status": "pending", "errors": [], "validation_mode": validation_mode}
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    avoid_notes: list[str] = []
    letter = ""
    client = get_client()
    cl_prompt_base = _build_cover_letter_prompt(profile)

    for attempt in range(max_retries + 1):
        report["attempts"] = attempt + 1
        # Fresh conversation every attempt
        prompt = cl_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES:\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (
                f"RESUME:\n{resume_text}\n\n"
                + (f"USER-VERIFIED EVIDENCE (only these facts; personal projects are not employment):\n"
                   f"{evidence_text}\n\n" if evidence_text else "")
                + f"---\n\nTARGET JOB:\n{job_text}\n\n"
                f"Length: {min_words}-{max_words} words. Name the role and the employer. "
                "Never promise outcomes or claim anything the resume/evidence does not state.\n"
                "Write the cover letter:"
            )},
        ]

        letter = client.chat(messages, max_tokens=1024, temperature=0.7)
        letter = sanitize_text(letter)  # auto-fix em dashes, smart quotes
        letter = _strip_preamble(letter)  # remove any "Here is the letter:" prefix

        validation = validate_cover_letter(letter, mode=validation_mode)
        problems = cover_letter_problems(letter, {**job, "company": job.get("company") or ""}, sources,
                                         min_words=min_words, max_words=max_words)
        report["errors"] = list(validation["errors"]) + problems
        if validation["passed"] and not problems:
            report["status"] = "approved"
            return letter, report

        avoid_notes.extend(report["errors"])
        # Warnings never block — only hard errors trigger a retry
        log.debug(
            "Cover letter attempt %d/%d failed: %s",
            attempt + 1, max_retries + 1, validation["errors"],
        )

    report["status"] = "rejected"
    return letter, report  # kept only as a rejected artifact


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_cover_letters(min_score: int = 7, limit: int = 20,
                      validation_mode: str = "normal") -> dict:
    """Generate cover letters for high-scoring jobs that have tailored resumes.

    Args:
        min_score:       Minimum fit_score threshold.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".

    Returns:
        {"generated": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    resume_text = RESUME_PATH.read_text(encoding="utf-8")
    conn = get_connection()
    from applypilot import opsconfig
    from applypilot.scoring import documents as D
    from applypilot.scoring.candidate_evidence import format_candidate_evidence
    sources = D.truth_sources(profile, resume_text)
    evidence = D.load_evidence_safe()
    evidence_text = format_candidate_evidence(evidence) if evidence else ""
    ops = opsconfig.load()
    try:
        model = get_client().model
    except Exception:  # noqa: BLE001
        model = "unknown"

    # Jobs with a validated (hash-bound) tailored resume but no cover letter yet. Same gates as
    # tailoring: verified-eligible, posting verified active, not a duplicate (audit R3).
    from applypilot.eligibility import SQL_ELIGIBLE
    jobs = conn.execute(
        "SELECT * FROM jobs "
        "WHERE fit_score >= ? AND tailored_resume_path IS NOT NULL "
        "AND resume_validation = 'passed' AND resume_sha IS NOT NULL "
        f"AND {SQL_ELIGIBLE} AND posting_status = 'active' AND duplicate_of IS NULL "
        "AND full_description IS NOT NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        "AND COALESCE(cover_validation, '') != 'needs_review' "
        "AND COALESCE(cover_attempts, 0) < ? "
        "ORDER BY fit_score DESC",
        (min_score, MAX_ATTEMPTS),
    ).fetchall()
    from applypilot.ranking import rank
    jobs = rank([dict(r) for r in jobs])
    jobs = jobs[:limit] if limit else jobs

    if not jobs:
        log.info("No jobs needing cover letters (score >= %d).", min_score)
        return {"generated": 0, "errors": 0, "elapsed": 0.0}

    # Convert rows to dicts
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    log.info(
        "Generating cover letters for %d jobs (score >= %d)...",
        len(jobs), min_score,
    )
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    error_count = 0

    from applypilot import runctl
    stopped_early = False
    for job in jobs:
        if runctl.expired():  # daily max_runtime_minutes (audit R5)
            stopped_early = True
            break
        completed += 1
        try:
            letter, report = generate_cover_letter(
                resume_text, job, profile, validation_mode=validation_mode, sources=sources,
                evidence_text=evidence_text, min_words=int(ops.get("cover_min_words", 120)),
                max_words=int(ops.get("cover_max_words", 320)))
            report["model"] = model
            approved = report["status"] == "approved"
            cl_path = D.save_artifact(COVER_LETTER_DIR, D.safe_prefix(job), "_CL", letter, report, approved)

            result = {
                "url": job["url"],
                "path": str(cl_path),
                "sha": D.sha_file(cl_path) if approved else None,
                "for": D.fingerprint(job, profile, resume_text),
                "title": job["title"],
                "site": job["site"],
                "approved": approved,
                "errors": report.get("errors", []),
            }
            results.append(result)

            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            log.info(
                "%d/%d [OK] | %.1f jobs/min | %s",
                completed, len(jobs), rate * 60, result["title"][:40],
            )
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"], "site": job["site"],
                "path": None, "sha": None, "error": str(e),
            }
            error_count += 1
            results.append(result)
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)

    # Persist to DB: increment attempt counter for ALL, save path only for successes
    now = datetime.now(timezone.utc).isoformat()
    saved = 0
    for r in results:
        if r.get("path") and r.get("approved") and r.get("sha"):
            conn.execute(
                "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, cover_validation='passed', "
                "cover_validation_reasons=NULL, cover_model=?, cover_sha=?, cover_for=?, "
                "cover_pdf_path=NULL, cover_pdf_sha=NULL, review_artifacts=NULL, "
                "cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (r["path"], now, model, r["sha"], r["for"], r["url"]),
            )
            saved += 1
        elif r.get("path"):
            conn.execute(
                "UPDATE jobs SET cover_validation='rejected', cover_validation_reasons=?, cover_rejected_path=?, "
                "cover_model=?, cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (json.dumps(r.get("errors", [])[:15]), r["path"], model, r["url"]),
            )
        else:

            conn.execute(
                "UPDATE jobs SET cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (r["url"],),
            )
    conn.commit()

    from applypilot.scoring.pdf import render_job_document
    for r in results:
        if r.get("approved") and r.get("sha"):
            row = conn.execute("SELECT * FROM jobs WHERE url = ?", (r["url"],)).fetchone()
            try:
                render_job_document(conn, dict(row), "cover")
            except Exception:
                log.debug("PDF generation failed for %s", r["path"], exc_info=True)
    conn.commit()

    elapsed = time.time() - t0
    log.info("Cover letters done in %.1fs: %d generated, %d errors", elapsed, saved, error_count)


    return {
        "generated": saved,
        "errors": error_count,
        "elapsed": elapsed,
        "stopped_early": stopped_early,
    }
