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

## Phase 3 — Apply-agent security S1–S4 (≈2 sessions) ← NEXT (dev)
See `APPLY_AGENT_SECURITY_PLAN.md`: least-privilege tools (`--tools ""`, `dontAsk` + allow-list), hook
browsing policy + `--allowed-origins`, narrow verification-code helper instead of Gmail MCP, no
password in prompts, redacted logs, stop cloning the real Chrome profile, pinned MCP versions.

## Phase 4 — Tailoring + cover letters on real data (≈0.5–1 session)
- `applypilot run tailor cover pdf --min-score 7` on the 11 eligible high scorers with the local model.
- Human review of outputs against resume + evidence; fill `profile.skills_boundary`; validator `strict`.
- Test that tailored output contains no skill outside resume ∪ evidence; record model name with scores.

## Phase 5 — Liveness + dedupe (≈1 session)
- Pre-apply liveness check (HTTP status + closed-posting phrases) → `expired` without an agent run.
- Normalised dedupe key (company + normalised title + location) and URL canonicalisation.
- `applypilot apply --review` listing `needs_review` jobs with reason and log path.

## Phase 6 — Supervised dry-run applications (≈1 session)
- S5/S6 prompt changes (injection resistance, stop-and-ask screening questions).
- `apply --dry-run --url` on 3–5 jobs across ATS types, visible Chrome, watching every step.

## Phase 7 — Supervised real submissions (user-run), then autonomy
- User approves each job; `apply --url --workers 1`; verify confirmation email. Then small batches.
- NC employer list for discovery; scheduled discover→enrich→score→tailor; daily review digest.
- Ruff cleanup in touched files so CI can go green.

**Estimate to supervised real submission (end of Phase 6): ~4.5–5 working sessions** (security plan
added ~2 sessions that were previously implicit).
