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

- **Final release remediation** (`6b990c5`..`319fa82`) — every `FINAL_RELEASE_AUDIT.md` finding R1–R12
  fixed with regression tests (`FINAL_REMEDIATION_REPORT.md`); salary/commute ranking, data-dir
  employer registries, permitted boards, stale-score rescoring.

## Next — needs the user (no code blockers)
1. **Independent re-audit** of the remediation commits (targets: `FINAL_REMEDIATION_REPORT.md` § Audit targets).
2. **Candidate evidence**: confirm certification status/dates, employment dates, education and years of
   experience (resume and stated figure differ) in `candidate_evidence.yaml` / `resume.txt`.
3. **Real-data preparation, supervised**: `applypilot daily verify dedupe classify`, then
   `applypilot rescore --limit 100` (historical scores came from another model name), then
   `daily tailor cover pdf prepare` (5 resumes / 5 letters per run); review every document.
4. **Employer watchlist**: add real Workday endpoints to `APP_DIR/employers.yaml`, run
   `applypilot employers --verify` (none were added: no endpoint was verified in this pass).
5. **Isolated browser host**: build the dedicated VM (`DEPLOYMENT.md` § 8a) before any apply session;
   then first supervised dry run, one approved job, watched.
6. **Scheduling**: import `scripts/ApplyPilotDaily.task.xml` only after an observed preparation cycle.


## Later (optional)
- Job-specific receipt parsing for submission evidence (application ID on the confirmation page).
- ATS-specific adapters only where a supervised run shows a need (e.g. Greenhouse question API).
- Rendered (headless) posting verification for script-only career sites.
- Ruff cleanup of upstream files so CI can go green.
