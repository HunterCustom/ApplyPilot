# Development Roadmap

Goal: supervised, then largely autonomous, applications to eligible NC / US-remote IT roles,
with no fabricated qualifications, no duplicate submissions, and no CAPTCHA/access bypass.

## Done
- **Phase 0 / 0b** — apply-tracking safety, final-result parsing, claim ownership, stale-lock recovery.
- **Phase 1** — local Qwen3-14B config, accurate `doctor`.
- **Phase 2** — deterministic eligibility engine + gating.
- **Phase 3 (S1–S4)** — restricted agent, policy hooks, isolated browser, redaction (`fcf9e31`).
- **Phase 3b** — launcher-owned network + submission boundary (`c111464`).
- **Phase 3b audit fixes** (`66b3c45`) — all Codex findings H1–H4, M1–M6, L1–L3 addressed
  (`APPLY_AGENT_SECURITY_PLAN.md` § Audit response).
- **Phase 4 / 5 / 6 code** (`57a5b19`, `82449be`) — posting verification + freshness, cross-source dedupe,
  discovery history, scoring traceability, grounded resume/cover validation (fail-open fixed), candidate
  evidence everywhere, screening answer bank (S6), readiness + review queue, daily orchestrator, reports,
  synthetic end-to-end tests, `DEPLOYMENT.md`.

## Next — needs the user (no code blockers)
1. **Independent re-audit** of `66b3c45` (targets: `PROJECT_COMPLETION_REPORT.md` § Audit targets).
   Do not run employer browser sessions before it.
2. **Configuration**: write `screening_answers.yaml` (work auth, sponsorship, location, availability,
   salary, plus any EEO "decline" answers you explicitly authorize with `sensitive_ok: true`); review
   `candidate_evidence.yaml`; optional `pipeline.yaml` thresholds.
3. **Real-data preparation, supervised**: `applypilot run verify dedupe classify` on the dev DB (network
   GETs to postings), then `run score tailor cover pdf prepare` with `--min-score 7` in small batches; review
   every document (`applypilot review --show URL`). Tune the factcheck vocabulary only with true facts.
4. **First supervised dry runs** (after the re-audit): one approved job per run, `--dry-run --workers 1`,
   watched. Extend `STATIC_ASSET_HOSTS` only with exact provider hosts seen in `network.jsonl`.
5. **Supervised real submissions**: user approves each job and each request; verify confirmation emails.
6. **Scheduling**: import `scripts/ApplyPilotDaily.task.xml` when the daily run has been watched once.

## Later (optional)
- Job-specific receipt parsing for submission evidence (application ID on the confirmation page).
- ATS-specific adapters only where a supervised run shows a need (e.g. Greenhouse question API).
- Rendered (headless) posting verification for script-only career sites.
- Ruff cleanup of upstream files so CI can go green.
