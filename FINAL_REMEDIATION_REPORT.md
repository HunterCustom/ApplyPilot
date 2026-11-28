# Final Remediation Report — ApplyPilot-Dev

_2026-11-28 · branch `feature/claude-development` · audited baseline `6ba9d90` · remediation
`6b990c5`..HEAD · not merged, not deployed. Responds to `FINAL_RELEASE_AUDIT.md` (verdict FAIL)._

**Verdict of this pass: every confirmed finding (R1–R12) is fixed in code and covered by a
regression test that reproduces the audited failure and asserts prevention. Production readiness is
NOT claimed.** Browser applying stays disabled on this PC by design (R6) until a dedicated isolated VM
exists and is independently validated; document preparation is ready for supervised real-data use.

## 1. Findings and resolution
Tests are in `tests/test_release_remediation.py` unless named otherwise.

| ID | Status | Fix (references) | Regression evidence |
|---|---|---|---|
| R1 fabricated / rejected material reaches the queue | **Fixed** | Strict judge verdict `tailor.parse_judge_verdict`; no-tolerance judge prompt; factcheck adds degrees, institutions, leadership, invented headings/seniority, unsupported numbers, numbers in another context or counting another noun (`scoring/factcheck.py` `_check_education`, `_check_leadership`, `_check_number_context`, `_check_history`). Content-hashed artifacts (`resume_sha`, `resume_pdf_sha`, `cover_*`, `cover_for`) via `scoring/documents.artifact_problems/record_pdf/manifest`; PDF bound only to text with the recorded hash; approved save removes an older same-stem PDF. Readiness verifies bytes, PDF and input fingerprints (`readiness.evaluate`); human approval stores the manifest and is withdrawn on any change. Prompt uses only hash-verified bytes, never a legacy/unvalidated cover, and tells the agent to stop rather than compose a letter (`apply/prompt.py`). | `JudgeParserTests`; `GroundingTests` (invented leadership + degree with passing judge, moved number, invented senior title, FAIL quoting PASS, malformed judge); `ArtifactBindingTests` (replaced text / PDF, missing PDF, legacy cover, unvalidated cover never offered, agent receives exact bytes, failed regeneration leaves no stale PDF, approval bound to artifacts) |
| R2 PDF renderer treats prose as HTML | **Fixed** | Every field escaped (`scoring/pdf._escaped`); JS disabled, offline context, every request aborted and counted (`RenderGuard`); dedicated letter template; atomic write; DB-driven `batch_convert` | `RenderEscapingTests`; `RenderSafetyTests` (real Playwright: loopback canary server receives 0 requests from img/css/iframe/script; rendered text equals the reviewed source) |
| R3 verification fetches untrusted destinations / blesses wrong posting | **Fixed** | `verify.check_destination` (scheme, userinfo, ports 80/443, every resolved address public) + `safe_fetch` (pinned dialing to the checked IP, manual redirects ≤5, per-hop policy + robots); final URL persisted; same-site/ATS-tenant, whole-word title and requisition checks; verification bound to the URL checked (`is_fresh`); human confirmation bound too (`cli_ops verify`). Eligibility bound to an input hash (`eligibility.input_hash/decision_is_current`, reclassified on change; manual decisions bound to text); cover selection gated like tailoring. | `DestinationPolicyTests`, `SafeFetchTests` (real loopback server: pinning, internal redirect refused before connect, IP-literal/port, loop bound, robots per hop, final URL), `PostingMatchTests`, `InputBindingTests` |
| R4 time-ordered evidence permits a false submission | **Fixed** | Guard journals `release_attempt` before continuing, then `released`/`release_unconfirmed`, then captures the server `response` (status) for that request id via Fetch `interceptResponse` (`apply/network_guard.py`); `launcher.submission_evidence` correlates by request id (approval requested after the click, 2xx/3xx, confirmation after the response, exactly one write in the window); unresolved attempts / releases without response are ambiguous; confirmation counted only from page-observation tools after a 2xx/3xx (`policy_hook.handle_post`); unconfirmed seal → uncertain (`NetworkGuard.seal`, `run_job`) | `CausalEvidenceTests` (audit case: click + other request + unrelated confirmation), `HookConfirmationTests`, `SealFailureTests`; real Chrome `test_released_write_is_recorded_as_evidence` asserts `release_attempt → released → response` for one id |
| R5 scheduled runs overlap after interruption | **Fixed** | `daily.RunLock` = OS byte-range lock (`msvcrt.locking` / `flock`), never unlinked; taken before DB open/migration; shared by `daily`, `run`, `rescore`; interrupted `pipeline_runs` reconciled; `runctl` deadline inside verify/score/tailor/cover; partial stages exit 1; migrations serialized in one `BEGIN IMMEDIATE`; WAL switch retried (found by the new test) | `SchedulerLockTests` (4 real processes on a stale lock → exactly 1 owner; crash recovery; late release; torn record; 4 processes bootstrapping an old schema, data preserved); `DailyRunTests` |
| R6 job object does not stop OS-brokered handoffs | **Mitigated by isolation; residual documented** | Robust prevention is not possible on a personal desktop without changing protocol associations (forbidden). `apply/isolation.py`: browser sessions (dry or real) only on an attested isolated host (env flag **and** host-bound `isolated-host.json`); checked in `launch_chrome` before any side effect and early in `apply`. Chrome must carry a valid Google LLC Authenticode signature (`chrome.chrome_signature_problem`). VM architecture: `DEPLOYMENT.md` § 8a. | `IsolatedHostGateTests` (refused before slot/port/jail/process), `ChromeProvenanceTests` (empty and signed-non-Google `chrome.exe` refused; installed Chrome accepted), CLI `test_no_browser_session_on_a_non_isolated_host` |
| R7 approval lost by the ledger before release | **Fixed** | `used` marker is the single commit point; ledger and unlinks best-effort (`_unlink_quietly`); grants via `os.link` (no replace on POSIX); click approval re-checks policy, guard and session after the wait | `ApprovalCommitTests` (WinError 32 reproduced against the pre-fix code, now passes; 6 concurrent processes → exactly 1 winner, 0 exceptions; forged grant; grant/cancel race; binding; expiry), `ClickApprovalRecheckTests` |
| R8 review/recovery leave a submitted job selectable | **Fixed** | `_still_ready` checks ownership, `applied_at`, live approval, continues with the fresh row, inside the release block; claims exclude `applied_at`; `mark_job`/`reset_failed`/`review` transactional with row counts, refuse live claims, never erase an application; append-only `application_events` | `ArtifactBindingTests` (deferred after claim, ownership, manual-submitted in flight, history), `test_apply_safety` (`test_mark_failed_never_erases_an_application`, updated recovery test) |
| R9 dedupe over/under-merges | **Fixed** | Requisition conflict beats URL equality; job-specific URLs only, cross-field; path case + ports kept; exact identities without cutoff; conflicting groups refused + flagged; history-sticky links; canonical row prefers the employer posting (`dedupe.py`) | `DedupeRemediationTests`; real-data copy: 896 rows → same 2 true duplicates, 0 review flags |
| R10 screening/log controls rely on model compliance | **Fixed (pattern-based residual)** | Sensitive reusable answers withheld without `sensitive_ok` (`screening.entry_is_sensitive`); display masks short tokens and label PII (`redact._SHORT_TOKEN_RE`, `approval._safe_label`); stale verification secrets purged at launcher start; agent env allowlist (`launcher.agent_env`) | `SensitiveLeakTests` |
| R11 scoring accepts conflicting output | **Fixed** | Exactly one in-range SCORE; rescore uses the ordinary filters and only stale scores | `ScoreParsingTests`, updated `ScoringTests` |
| R12 interrupted launch / docs | **Fixed** | Start-up failure closes the jail and clears registries; clearer error; DEPLOYMENT documents Playwright Chromium for rendering/discovery, signature check, OS-broker limit | `StartupFailureCleanupTests` |

Earlier-audit items marked "partially resolved" (H1 TLS/pre-hold timing, H2 path tenants / data in GETs,
M2 worker sockets, M5 CDP heartbeat, L1 helper scope, L3 GET signal expiry) were **not changed** in this
pass; they matter only for browser sessions, which are now disabled outside an isolated host. They
remain audit targets (§ 7).

## 2. Operational improvements (P5)
- `ranking.py`: order = fit×20 + salary band + commute tier; never a filter; hourly pay annualized and
  flagged. Code defaults are neutral; the candidate's bands are in the private `APP_DIR/priority.yaml`.
- Workday watchlist `APP_DIR/employers.yaml` (validated host pattern), crawled only after
  `applypilot employers --verify`; extra career sites `APP_DIR/sites.yaml`. **No employer endpoint was
  added**: none was verified in this pass (verification needs network lookups per employer).
- Job boards limited to supported, unblocked ones (Glassdoor skipped by site rules).
- Score provenance: model + prompt + evidence hash + resume hash; `applypilot rescore` replaces only
  stale scores and keeps history (authorized as a future step; not run).

## 3. Test results
- `pytest tests/`: **361 passed, 0 skipped, 172 subtests, 110.6 s**, including the real-Chrome network
  boundary suite (installed Chrome 158, jailed, loopback only) and real Playwright render tests.
- New `tests/test_release_remediation.py`: 99 tests (+ 2 Windows/browser-conditional ones on other hosts).
- Ruff on the 34 Python files changed since `6ba9d90`: 86 → 85, no new finding. Pyright on all new and
  remediated modules: 0 errors (repo-wide pre-existing upstream errors unchanged).
- Secret/PII scan of every changed file: no secrets; hits are synthetic test emails, the state code
  "NC", and the pre-existing city mention in `PROJECT_STATUS.md`. A first scan caught the candidate's
  salary bands in code defaults; they were removed (`319fa82`).
- Not run (by constraint): any apply session, employer form, real posting check, LLM generation on real
  data, Gmail helper, scheduled task, OS protocol handler.

## 4. Remaining blockers and limitations
1. **No isolated browser host exists yet**; until one is built and audited, no apply session can run.
2. Job object + page guard remain defence in depth; the isolation gate is an attestation, not a proof.
3. Submission evidence is the HTTP answer + page confirmation, not an ATS receipt/application ID.
4. Factcheck is deterministic but vocabulary/heuristic-based; it is conservative (rejects unsupported
   numbers/degrees/leadership) and may reject true phrasing absent from the sources — add true facts,
   never loosen checks. Real-LLM behaviour against it is untested.
5. DNS pinning covers each hop's connection; robots.txt is fetched through the same policy.
6. Candidate evidence is unverified (certification dates/status, employment dates, education, years:
   the resume and the stated years differ) — required before generating documents.
7. Backups: `~\.applypilot-dev\backups\pre-migration-20261128-000216\` holds `.env` (API key names:
   LLM_API_KEY, GEMINI_API_KEY) and a `profile.json` that still contains the applicant password removed
   from the live profile afterwards. Kept private, outside Git; not deleted (needs your authorization).

## 5. Deployment readiness by subsystem
| Subsystem | Readiness |
|---|---|
| Discovery (boards, Workday registry) | Supervised use; employer watchlist empty until endpoints are verified |
| Posting verification | Ready for supervised real use (never run on real postings yet) |
| Eligibility / dedupe / scoring | Ready for supervised use; rescoring of historical scores recommended |
| Documents (resume, cover, PDF) | Ready for supervised generation **after** evidence verification; every document human-reviewed |
| Readiness / review queue | Ready |
| Daily scheduler | Code ready; enable only after one observed cycle (not activated) |
| Browser application | **Blocked** on this PC; requires the isolated VM + re-audit |
| Unattended submission | Unsupported by design |

## 6. Commits (all pushed to `origin/feature/claude-development`, not merged)
`6b990c5` R1–R3, R8, R11 · `44043a8` R5 · `0c44349` R4, R7, R10 · `2229b7f` R6, R12 · `594a17d` R9 ·
`2157207` P5 · `319fa82` neutral salary defaults + type fixes · docs/report commit (this file).

## 7. Audit targets for the next independent review
1. `verify.safe_fetch` / `check_destination` (pinning, IPv6, redirect edge cases).
2. `scoring/documents` + `readiness.evaluate` + `apply/prompt.py`: any path to use bytes other than the
   hashed ones; manifest/approval invalidation.
3. `scoring/factcheck` false negatives (claims outside its patterns) and false positives on real resumes.
4. `network_guard` response-stage handling (`interceptResponse`) and `submission_evidence` correlation.
5. `daily.RunLock` semantics on network drives / other filesystems.
6. `apply/isolation.py` gate placement (every browser entry point) and the VM procedure.
7. Unchanged H1/H2/M2/M5/L1/L3 residuals before any isolated-host session.
