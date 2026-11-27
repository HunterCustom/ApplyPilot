# Development Roadmap

Goal: supervised, then largely autonomous, applications to eligible NC / US-remote IT roles,
with no fabricated qualifications, no duplicate submissions, and no CAPTCHA/access bypass.
Each phase ends with tests passing and a review checkpoint. Estimates are focused working sessions.

## Phase 0 — Apply-tracking safety (≈0.5 session) ← START HERE
- Dry run: agent emits `RESULT:DRY_RUN` (or launcher maps APPLIED→`dry_run` when `dry_run=True`); store `apply_status='dry_run'`, never `applied`, never touch `applied_at`. Dry-run rows stay re-queueable.
- Fix `acquire_job(target_url)`: `(apply_status IS NULL OR apply_status NOT IN ('in_progress','applied'))`.
- Manual-ATS skip continues to the next job instead of ending the worker.
- Unit tests with a temp `APPLYPILOT_DIR` SQLite DB (no browser, no LLM).

## Phase 1 — Config hygiene (≈0.25 session, user + Claude)
- User fixes dev `.env`: single `LLM_MODEL` (local `qwen/qwen3-14b` or Gemini without `LLM_URL`).
- `doctor` reports the *effective* provider/model (LLM_URL precedence).
- Record model name with each score (e.g. in `score_reasoning` header) for traceability.

## Phase 2 — Hard eligibility gate (≈1 session)
- Deterministic `eligibility.py`: classify each job `eligible_nc | eligible_us_remote | restricted_remote | ineligible | unknown` from location + description (state lists, "must reside in", "Remote – <state>", international, title-embedded cities).
- Config from `priority.yaml` / `searches.yaml` (`location_accept`); fix prompt to read `location_accept`.
- Gate tailoring and apply on `eligible_*`; `unknown` → manual review list, not auto-apply.
- Tighten apply prompt: remote is eligible only if not restricted to states excluding NC.
- Tests from real (anonymised) location strings in the dev DB.

## Phase 3 — Liveness + dedupe (≈1 session)
- Pre-apply liveness check: HTTP status + closed-posting phrases on `application_url`; mark `expired` without spending an agent run. Re-check if `detail_scraped_at` > N days.
- Normalised dedupe key (company + normalised title + location) and `application_url` canonicalisation; block apply if a sibling row is `applied`/`in_progress`.
- `applypilot status` shows dedupe groups and stale jobs.

## Phase 4 — Tailoring + cover letters on real data (≈0.5–1 session)
- Run `run tailor cover pdf --min-score 7` on the eligible subset with the local model.
- Human review of 3–5 outputs against resume + evidence; fill `profile.skills_boundary`; use validator `strict`.
- Add a test that tailored output contains no skill outside resume ∪ evidence.

## Phase 5 — Supervised dry-run applications (≈1 session)
- `applypilot apply --dry-run --url <job>` on 3–5 jobs across ATS types (Workday, Ashby, ADP, Greenhouse, PeopleAdmin), visible Chrome, watching.
- Review agent logs; tune prompt for screening questions; confirm `dry_run` status and no submission.
- Redact profile password from saved logs.

## Phase 6 — Supervised real submissions (user-run)
- User reviews each job, runs `apply --url` with `--workers 1`, verifies confirmation email.
- Then small batches (`--limit 3`), review, expand.

## Phase 7 — Autonomy & discovery quality (ongoing)
- Replace default `employers.yaml` with NC employers (Duke, UNC, Lenovo, Cisco RTP, Red Hat, SAS, Wells Fargo, Truist, LabCorp, state/county gov) and add NC Workday tenants.
- Scheduled discover→enrich→score→tailor; daily review digest; follow-up tracking.
- Ruff cleanup so CI is green (low priority; do incrementally in touched files).

**Estimate to supervised real submission (end of Phase 5): ~4–5 working sessions.**
