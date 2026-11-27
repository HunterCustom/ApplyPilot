# Development Roadmap

Goal: supervised, then largely autonomous, applications to eligible NC / US-remote IT roles,
with no fabricated qualifications, no duplicate submissions, and no CAPTCHA/access bypass.
Each phase ends with tests passing and a review checkpoint. Estimates are focused working sessions.

## Done
- **Phase 0 — Apply-tracking safety** (`021d9a4`): dry-run status, target-URL fix, duplicate guard,
  uncertain → `needs_review`, stale-lock recovery, watchdog.
- **Phase 1 — Config hygiene** (`b811031`): local Qwen3-14B, accurate `doctor`.
- **Phase 0 follow-up — Safety review** (`5c0bc4d`): final-result-only parsing, claim ownership,
  dry-run guard hook + email block, `--strict-mcp-config`, owned-process Chrome cleanup, CAPTCHA
  detect-and-stop.
- **Phase 2 — Eligibility engine + pipeline gating** (`26aa8f9`, `0b5bb8a`): deterministic classifier,
  gating of scoring/tailoring/applying, `applypilot eligibility` review CLI.

## Phase 2b — Eligibility review on real data (user, ~15 min) ← NEXT (user)
- Decide commute for the 6 NC jobs outside tiers (Chapel Hill, Morrisville, Timberlake, Trinity,
  Clayton, Lillington): add towns to `eligibility.commute_tiers` in dev `searches.yaml`, or
  `applypilot eligibility --approve/--reject URL`.
- Skim `applypilot eligibility --status eligible_us_remote --evidence --min-score 7`.

## Phase 3 — Apply-agent security S1–S4 — DONE (`fcf9e31`)
Restricted tool surface with runtime verification, policy hooks (navigation/upload/value, redirect
tripwire, real-run submit approval), no Gmail MCP (optional readonly verification helper), no password
in prompts, empty isolated Chrome profiles, redacted logs, lifecycle/concurrency fixes. Live probe on
local synthetic pages. Details: `PROJECT_STATUS.md` § Safety model.

## Phase 3b — Launcher-owned network + submission boundary — DONE, AWAITING REVIEW
Egress proxy (only network route, own DNS + IP pinning) + CDP `Fetch` controller (every target, every
request incl. redirect hops, held before sending) + self-test before the agent starts; label-free dry-run
boundary; single-use, expiring, action-bound approvals for real runs. Verified: live probe `redirect`
0 hits at the forbidden target, `continue`/`jsnext` 0 POSTs; Chrome-driven tests for subresources,
popups, workers, service workers, keyboard/auto submits, WebSocket stop, fail-closed paths.
Residual limits: `PROJECT_STATUS.md` § Known remaining issues. **Do not start Phase 4 before review.**

## Phase 3c — First supervised dry runs (after 3b review)
- One user-picked job per run, `--dry-run`, watched. Review `network.jsonl` for blocked asset hosts and
  extend `STATIC_ASSET_HOSTS` only with exact provider hosts. Expect runs to end at the first
  state-changing request (by design).

## Phase 4 — Tailoring + cover letters on real data (≈0.5–1 session)
Prerequisites (code, before any bulk generation):
- Judge-rejected resumes must not count as success (`tailor.py` accepts `approved_with_judge_warning`
  and sets `tailored_resume_path`); route them to human review instead. Same for cover letters, which
  have no judge and return the last attempt even when validation failed (`cover_letter.py:183`).
- Deterministic claim check: every skill/tool/cert/metric in the output must appear in the base resume,
  `profile.skills_boundary`/`resume_facts`, or user-verified `candidate_evidence.yaml` (today the
  validator uses `skills_boundary` only and tailoring never reads the evidence file).
- Freshness: store `date_posted` (jobspy provides it) and re-check the posting (HTTP fetch, closed
  phrases, still eligible) right before tailoring; skip postings older than N days (configurable).
- Run eligibility on the real dev DB (it has no eligibility columns yet; the 11 came from a copy).
Then: `applypilot run tailor cover pdf --min-score 7` one job at a time with human review of each output
against resume + evidence; validator `strict`; record model name with outputs.

## Phase 5 — Liveness + dedupe (≈1 session)
- Pre-apply liveness check (HTTP status + closed-posting phrases) → `expired` without an agent run.
- Normalised dedupe key (company + normalised title + location) and URL canonicalisation.
- `applypilot apply --review` listing `needs_review` jobs with reason and log path.

## Phase 6 — Supervised dry-run applications (≈1 session)
- S5 done (trust-boundary prompt). S6 stop-and-ask screening questions still to do.
- `apply --dry-run --url` on 3–5 jobs across ATS types, visible Chrome, watching every step.

## Phase 7 — Supervised real submissions (user-run), then autonomy
- User approves each job; `apply --url --workers 1`; verify confirmation email. Then small batches.
- NC employer list for discovery; scheduled discover→enrich→score→tailor; daily review digest.
- Ruff cleanup in touched files so CI can go green.

**Estimate to supervised real submission (end of Phase 6): ~3.5–4 working sessions** (Phase 3b,
4, 5, 6).
