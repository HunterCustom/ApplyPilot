# Project Completion Report — ApplyPilot-Dev

_2026-11-27 · branch `feature/claude-development` · baseline `3ac05b2` · not merged into `main`._

## 1. Summary
The preparation platform is complete in code and verified on synthetic data: discovery with history,
posting verification, cross-source dedupe, deterministic eligibility, traceable scoring, evidence-grounded
resumes and cover letters (fail-open fixed), screening-answer bank, readiness gate + human review queue,
daily orchestrator, reports, and deployment documentation. Every finding of the independent Phase 3b audit
was addressed in code with regression tests. **Browser-based applying remains supervised-only and is not
certified submission-proof**; no apply session (dry or real) was run and no employer form was touched.

## 2. Commits
| Commit | Content |
|---|---|
| `66b3c45` | Apply-boundary fixes for audit H1–H4, M1–M6, L1–L3 (proxy kill switch + write gate, process jail, causal evidence, atomic approvals, tenant scoping, tool surface, persistence sanitising, CDP client limits, worker scope) |
| `57a5b19` | Preparation pipeline: migrations, discovery history, verify, dedupe, scoring traceability, factcheck, tailor/cover fail-closed, screening, readiness/review, daily, reports, CLI, synthetic E2E tests |
| `82449be` | Dedupe tightened after real-data validation (cross-source similarity only; Workday requisition IDs) |
| (docs commit) | `DEPLOYMENT.md`, this report, status/roadmap/security plan, scheduler wrapper + template |

## 3. Test and quality results (final run)
- `pytest tests/`: **258 passed, 144 subtests, 0 skipped, 96.9 s**, including 20 real-Chrome tests run
  against the installed `C:\Program Files\Google\Chrome\Application\chrome.exe` (headless, temp profiles,
  inside the process jail, loopback synthetic servers only). Baseline was 190.
- New: `tests/test_security_hardening.py` (24), `tests/test_preparation_pipeline.py` (36) + browser tests
  (`test_held_write_never_sent_when_controller_is_lost`, `test_released_write_is_recorded_as_evidence`,
  `test_page_websocket_and_webrtc_removed`, `test_worker_websocket_stops_the_session`,
  `test_browser_runs_inside_the_process_jail`). The old `mailto:` browser test was removed (see §7).
- Ruff on the 37 changed Python files: 100 → 94 findings, **no new finding in any file**.
- Pyright on changed source files: 29 → 25 errors (remaining are pre-existing upstream ones); new modules 0.
- Secret/PII scan of every file changed since the baseline (dev profile + `.env` values compared in
  process, only key names reported): no secrets, no profile values introduced. One pre-existing mention of
  the candidate's home city in `PROJECT_STATUS.md` (present at baseline, unchanged). Synthetic test
  domains only (`example.org`, `acme-corp.com`, `.test`).
- Real-data validation on a **copy** of the dev DB (backup API from a read-only connection; original
  untouched; no network, no LLM): additive migration of 896 rows OK; dedupe found 22 → after the fix 2
  merges, both true duplicates (same direct application URL).

## 3b. Browser-containment follow-up (after the updated audit)
- **Only Chrome is ever launched.** `chrome.require_chrome_binary` refuses any executable not named
  chrome/chromium (Vivaldi, Edge, Brave, … even via `CHROME_PATH`). Every ApplyPilot Chrome on Windows is
  jailed, with or without a caller callback; `scripts/live_policy_probe.py` was previously unjailed.
- **Tests prove prevention, not logging.** Browser tests use exactly
  `C:\Program Files\Google\Chrome\Application\chrome.exe` and a fresh temp profile. The jail test lists
  every process in the browser's job and requires all of them to be that Chrome. The `file:` test checks
  the page never received the file's contents. A repository test fails if any test or script fixture
  could open/click an OS-handoff scheme. Windows defaults and protocol associations were not modified.
- **Vivaldi check.** Process metadata only (no command lines): the only top-level Vivaldi process was
  started by Explorer at 11:34 (your session); every Vivaldi process created during the test runs was
  a child of that session. Nothing in this work launched Vivaldi.
- **Debugger failure Codex saw.** Not reproducible here: installed Chrome 158.0.8127.88 opened its debugger
  in ~0.7 s with a fresh profile (headless, jailed, with and without a fixed port; no Chrome policies set).
  One cause was reproduced: launching on a profile directory already used by another Chrome makes the
  new process hand off and exit (rc 21) without a debugger. The launcher now reports that case. The
  other likely cause is Codex's own sandbox (it reported loopback socket stalls). A real OS handoff
  end-to-end cannot be tested without changing protocol associations, which you ruled out.

## 4. Implemented and verified (synthetic tests)
| Area | What |
|---|---|
| Security (§1) | All audit findings; see `APPLY_AGENT_SECURITY_PLAN.md` § Audit response for fix ↔ test mapping |
| Discovery (§2) | Per-source isolation + `discovery_runs`; `job_sources` / first/last seen / seen count; exact `date_posted` only; Workday req IDs + `startDate`; configurable sources. Existing retries/timeouts/pagination kept |
| Verification (§3) | active/closed/removed/inaccessible/uncertain; robots.txt; per-host delay; official URL preferred; freshness gate; human overrides |
| Dedupe (§4) | URL canonicalisation, requisition IDs, cross-source similarity; never deletes; duplicate groups block re-application |
| Eligibility/scoring (§5) | `classify` stage before LLM; migrations; model + prompt version + config per score; malformed/timeouts retried, errors persisted, attempts capped, history kept; duplicates/closed skipped. Qwen3-14B default unchanged |
| Evidence (§6) | `candidate_evidence.yaml` feeds scoring, tailoring, cover letters and the grounding checks (earned vs in-progress certifications, projects ≠ employment) |
| Resumes (§7) | Judge rejection never approved; grounding + ATS checks in every mode; employment dates/employers preserved; rejected attempts separate; validation status/reasons/model stored; stale-document fingerprint |
| Cover letters (§8) | Grounding, promises, tone, length (configurable), role + employer named; failed letters never ready |
| Screening (§9) | Verified bank, reuse opt-in, sensitive categories always human; invented prompt answers removed |
| Readiness/review (§10) | All listed conditions; review sheet; approve/reject/defer/manual submission |
| Execution (§11) | Only ready + approved jobs claimed; readiness re-checked after claim; approved documents only; duplicate prevention by group; uncertain → `needs_review` |
| Daily (§12) | Lock + stale takeover, limits, runtime cap, model check, JSONL log, exit codes, never applies |
| Reports (§13) | 11 tabular reports + inference + failures; filter/sort; CSV (formula-safe) / JSON |
| E2E (§14) | Discover → … → review → claim → simulated submission record → duplicate prevention → crash recovery |

## 5. Implemented but not externally validated
- Posting verification against real career sites (never run on real postings in this pass).
- Real LLM output through the new grounding checks (factcheck vocabulary may reject true-but-unlisted
  phrasing or miss terms outside it). Run small batches and review.
- Process jail against a real OS protocol handoff (deliberately never triggered; mechanism verified with a
  `cmd.exe` child). Installed-Chrome compatibility verified only with synthetic pages.
- Any apply session on a real ATS (dry or real). Approval UX for multi-request ATS flows.
- Task Scheduler import (template + wrapper written, not registered).

## 6. Requires human authorization or configuration
- `screening_answers.yaml` (none exist yet → every application stops at standard questions).
- Reviewing/approving each job (`applypilot review`), each state-changing request during a real run,
  `--allow-real-submissions` for real runs, `APPLYPILOT_ENABLE_VERIFY_HELPER=1` for the mail helper.
- Confirming postings the checker marks `uncertain`/`inaccessible`; scheduling the daily task.
- An independent re-audit before any employer browser session (Codex's hold still applies until then).

## 7. Unsupported functionality (by design)
CAPTCHA/bot-wall/login bypass; account creation; unattended or automatic real submission; ATS-specific
adapters (unsupported flows stop for manual handling); non-Windows apply (no process jail); email
applications (`mailto:` is blocked); inferred answers to consequential questions; rendered-JS verification.

## 8. Known defects and limitations
- **H4 test incident (pre-existing, from the audit):** the removed `mailto:` test made Windows start the
  user's default handler (Vivaldi). No external-scheme fixtures remain; containment now exists, but
  shell handoffs via COM/`DelegateExecute` or IPC to an already-running app are outside the job object.
- JS-initiated GETs that change server state without carrying applicant values are not recognisable.
- Worker-context WebSockets are a tripwire (revoke on creation), not prevented.
- Approval binds request identity/digest, not the DOM the human saw; no job-specific receipt parsing.
- The write gate stalls all browser→server traffic while an approval is pending (by design).
- Path-tenant ATSs (Greenhouse, Lever) are scoped by host family, not tenant path.
- Script-rendered postings verify as `uncertain`; freshness falls back to first-seen when no exact date.
- Stale-claim recovery is age-based (safe: → `needs_review`, never retried).
- Repo-wide ruff (~140 upstream findings) and pyright (~80 upstream errors) are still red outside touched code.

## 9. Security and data-handling assessment
- Production data (`~\.applypilot`) never read or written; all runs used `APPLYPILOT_DIR=~\.applypilot-dev`
  or temp dirs; real-data checks used a DB copy in the scratchpad. No real Gmail, no employer pages, no
  submissions, Vivaldi never opened by this work. No LLM configuration changed.
- On disk: network logs and approval files hold sanitised URLs / digests only; job URLs in approvals are
  hashed; approvals are cancelled at session end. Still sensitive by necessity: tailored documents,
  `profile.json`, session `uploads/`, `--gen` prompt files (opt-in) — keep `APPLYPILOT_DIR` private.
- Exports contain posting data only (no profile fields); CSV cells are formula-escaped.
- The apply agent's surface is unchanged or narrower (screenshot removed, file-output args denied, env
  overrides scrubbed); no dev MCP servers, shell or file tools reach it.

## 10. Recommended independent (Codex) audit targets
1. `apply/egress_proxy.py` revoke + write gate under real TLS/HTTP2 timing; `network_guard.fail` ordering.
2. `apply/proc_jail.py`: suspended-start race, breakaway, Chrome child images, handler paths outside the job.
3. `network_guard._await_approval` / `approval.py` atomic markers; `request_body` completeness rules.
4. `launcher.submission_evidence` / `classify_session` ordering logic and `record_outcome` mapping.
5. `network_policy.decide_request` (preflight detection, `carries_sensitive`, asset bounds, tenant rule).
6. `PAGE_GUARD_SCRIPT` as defence in depth only (bypassable; must not be relied on).
7. `scoring/factcheck.py` false-negative paths (claims outside the vocabulary) and `tailor.py` statuses.
8. `readiness.evaluate` + launcher `_ready_clause` / `_still_ready`: any path to claim a non-ready job.
9. `screening.decide` sensitive-category coverage; `prompt.py` for remaining inferred answers.
10. `dedupe.same_opening` false-merge risk on other ATS URL shapes.
