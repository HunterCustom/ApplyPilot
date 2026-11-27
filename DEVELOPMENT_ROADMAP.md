# Development Roadmap

Goal: supervised, then largely autonomous, applications to eligible NC / US-remote IT roles,
with no fabricated qualifications, no duplicate submissions, and no CAPTCHA/access bypass.
Each phase ends with tests passing and a review checkpoint. Estimates are focused working sessions.

## Phase 0 — Apply-tracking safety ✅ DONE 2026-11-27 (`021d9a4`)
Dry-run status, target-URL fix, applied/review never re-acquired, application-URL duplicate guard,
manual-ATS continue, uncertain → `needs_review`, stale-lock recovery, watchdog. 22 regression tests.

## Phase 1 — Config hygiene ✅ DONE 2026-11-27 (`b811031`)
- Dev `.env` resolved to local `qwen/qwen3-14b`; `doctor` reports effective provider/model and checks it is served.
- Still open (fold into Phase 4): record the model name with each score for traceability.

## Phase 2 — Hard eligibility gate (≈1 session) ← NEXT
- Prerequisite quick fix: apply prompt reads `location_accept` (current key) and remote is only eligible when not restricted to states excluding NC.
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
- Prerequisite: replace the apply prompt's CAPTCHA "solve" / CapSolver instructions with detect → `RESULT:CAPTCHA` → stop.
- Add `applypilot apply --review` (list `needs_review` jobs with error + log path).
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

**Estimate to supervised real submission (end of Phase 5): ~3.5–4 working sessions remaining after Phase 0/1.**
