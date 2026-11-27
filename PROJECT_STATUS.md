# Project Status

_Updated 2026-11-27 · Branch `feature/claude-development` · Audit baseline `81c13b8` · Fork is level with
`upstream/main` (4a8d521) plus fork commits. See `DEVELOPMENT_ROADMAP.md` for next steps and
`APPLY_AGENT_SECURITY_PLAN.md` for the apply-agent hardening plan._

## Current state
| Area | State |
|---|---|
| Discovery / enrichment | Working (one run, 896 jobs, 43 enrichment errors). Discovery targets are mostly irrelevant upstream employers. |
| LLM | Local LM Studio `qwen/qwen3-14b` at `http://127.0.0.1:1234/v1`; verified inference; `doctor` reports it accurately. |
| Scoring | Working; evidence profile + NC priority ordering. Ineligible jobs are now skipped before the LLM. |
| Geographic eligibility | **New**, deterministic, v1.0.0. Gates tailoring and applying (queue and `--url`). |
| Tailoring / cover / PDF | Never run on this data. 11 eligible high scorers (on the evaluation copy). **Blocked on Phase 4 prerequisites** (judge-rejected output is accepted; no freshness check) — see roadmap. |
| Apply | S1–S4 (`fcf9e31`) + **Phase 3b network/submission boundary** (launcher-owned egress proxy + CDP Fetch controller) verified with Chrome-driven tests and live probes on local synthetic pages. **No apply run yet** (dry or real). Phase 3b awaits review before Phase 4. |
| Tests | 190 pass (+128 subtests, ~65 s; 15 real-Chrome tests skip without Chrome or with `APPLYPILOT_SKIP_BROWSER_TESTS=1`). |

## Safety model (apply)
- **Statuses**: NULL/`failed` (retryable), `dry_run`, `in_progress`, `applied`, `needs_review`, `manual`.
  Lifecycle documented at the top of the DB section in `apply/launcher.py`.
- **Outcome**: read only from the agent's final stream-json `result` event; only standalone `RESULT:` lines
  count. Missing / malformed / conflicting results, agent errors, watchdog kills, interruptions after the
  agent started, policy violations, and any non-APPLIED result after a submit-like action → `needs_review`.
- **APPLIED needs evidence** (real runs): an observed submit action **and** a human grant for the click
  **and** a human grant for the request the network guard released **and** confirmation text afterwards;
  otherwise `uncertain:applied_without_evidence`.
- **Dry runs** never become `applied`; and since the guard is best-effort, every *uncertain* dry run (incl.
  interrupted after agent start, stale claims, agent claiming APPLIED) → `needs_review`, never requeued.
- **Ownership**: claims carry `<session>:worker-N[:dry_run]`; results/releases apply only to the caller's
  own claim. Stale claims (real or dry) → `needs_review`. A per-worker OS file lock stops a second
  launcher from using (and killing) the same worker's Chrome/profile.
- **Agent tool surface (S1)**: `--tools ""`, `--setting-sources ""`, `dontAsk` + `--allowedTools`
  (13 browser tools), `--disallowedTools`, `--strict-mcp-config`, pinned `@playwright/mcp@0.0.86`.
  Fail-closed: CLI flag check at start, init-event check (kills session), SessionStart canary, and a
  hook audit record required for every tool call.
- **Policy hooks (S2, all runs)** — `apply/policy.py`, `apply/policy_hook.py`: navigation only to the
  job's domains + ATS list (no localhost/IPs/private/file:); uploads only the session's two PDFs;
  SSN/card-like values blocked; PostToolUse tripwire halts the session if the page URL leaves the
  allowlist. Dry-run submit guard included. Session files: `APP_DIR/apply-sessions/<id>/`.
- **Network + submission boundary (Phase 3b)** — `apply/network_guard.py`, `network_policy.py`,
  `egress_proxy.py`, `cdp.py`. One `NetworkGuard` per job, in the launcher process:
  - *Egress proxy* (started before Chrome): Chrome's only route (`--proxy-server`, `<-loopback>`,
    `--host-resolver-rules` making Chrome's own DNS fail, QUIC off, WebRTC UDP off). HTTPS `CONNECT :443`
    to allowlisted hosts only; resolves DNS itself, refuses if any address is non-public (incl. IPv6,
    IPv4-mapped/NAT64/6to4/Teredo), connects to the vetted IP (no rebinding). Plain HTTP never forwarded.
  - *CDP controller*: auto-attaches to every target paused (pages, popups, OOPIFs, workers, service
    workers), enables `Fetch` before it runs, holds **every request incl. each redirect hop** until
    decided: forbidden destinations/schemes (`file:` too) failed before sending; **dry runs: any non
    GET/HEAD/OPTIONS request or GET form submission is failed and ends the run as DRY_RUN** (label-free);
    real runs: each such request waits for a human grant. Static asset hosts are a fixed GET-only list for
    subresource types; page content never widens any allowlist.
  - *Fail closed*: no agent until a self-test proves Fetch interception and proxy routing (loopback and
    names); `run_job` refuses without a guard; lost CDP connection, an un-interceptable target, a
    WebSocket/WebTransport, `mailto:`/external schemes → guard failed / terminal violation, agent killed,
    hooks deny every tool, `needs_review`. Hooks also deny all tools unless the guard status is verified.
- **Submission authority (Task E + 3b)**: real runs need `--allow-real-submissions`; each submit-like click
  *and* each state-changing request needs `applypilot apply --approve <session> --nonce <id>`. Grants are
  single-use, expire with the request (10 min), and are bound to job URL + session + action digest
  (`apply/approval.py`); replayed, stale, mismatched or malformed grants are refused.
- **Email (S3)**: no Gmail MCP. Optional `verification_mcp` (one argument-less tool, gmail.readonly only,
  query fixed to session sender domains + start time) — enabled only if
  `APP_DIR/secrets/gmail_readonly.json` exists. **Not live-tested** (no credentials configured).
- **Secrets (S4)**: no password in prompts (account → `needs_review human:account_required`); empty
  isolated worker profiles (never cloned; legacy unmarked profiles refused); logs redacted; agent env
  stripped of API keys/tokens.
- **CAPTCHA**: detect, `RESULT:CAPTCHA`, stop. Never solved.
- **Live probe** (`scripts/live_policy_probe.py`, local synthetic pages only, real restricted agent,
  2026-11-27): S1/S2 as before; Phase 3b — `redirect`: forbidden internal server **0 requests** (was 1
  GET before 3b), hop held at the CDP layer; `continue` (button labelled Continue that POSTs): **0 POSTs**,
  run ends DRY_RUN; `jsnext` (Next button, JS `fetch` POST): **0 POSTs**, DRY_RUN.
- **Chrome-driven tests** (`tests/test_network_boundary.py`, no model): 0 requests at the forbidden
  server for redirects, img/script/css/iframe/fetch/XHR/beacon, popups, integer-IP URLs; 0 POSTs for
  Continue/Next/Finish buttons, GET forms, JS fetch/XHR/beacon, Enter key, auto-submit, dedicated and
  service workers; real-run POST released once per grant, expired request never sent.

Resolving `needs_review`: check the portal/email, then `applypilot apply --mark-applied URL` or
`--mark-failed URL` (then `--reset-failed` to requeue).

## Eligibility engine (`src/applypilot/eligibility.py`)
- Classes: `eligible_nc`, `eligible_us_remote`, `restricted_remote`, `ineligible`, `unknown`,
  plus human `eligible_manual` / `ineligible_manual` (never overwritten).
- Evidence: location field (multi-location, truncation), title-embedded locations, Workday URL slug and
  `Location:` lines when the field is empty, description residency / state-list / exclusion /
  nationwide-remote / onsite patterns. Stored: status, reason, JSON evidence, tier, version, timestamp.
- Precedence: NC exclusion > residency list without NC > NC commute tier > remote (nationwide needs
  positive US evidence) > other NC (config, default review) > out-of-state onsite > foreign > unknown.
- Config: `searches.yaml` → `eligibility:` (dev data has the Burlington/Graham/Mebane/Whitsett >
  Greensboro > High Point/Durham/Raleigh tiers); legacy `location_accept` still honoured.
- Pipeline: score stage classifies first and skips ineligible; tailor and apply require eligible from the
  current classifier version (or manual approval). Apply queue order: commute tier, then fit score.
- CLI: `applypilot eligibility` (review queue, `--status`, `--evidence`, `--reclassify`,
  `--approve/--reject URL --note`); `applypilot status` shows counts.

## Evaluation on a copy of the dev DB (original verified unchanged by SHA-256)
| Classification | All 896 | Scored 853 |
|---|---|---|
| ineligible | 613 | 613 |
| eligible_us_remote | 90 | 90 |
| eligible_nc | 60 | 60 |
| unknown (review) | 45 | 45 |
| restricted_remote (review) | 45 | 45 |
| unclassified (no description, enrichment errors) | 43 | 0 |

- 613 LLM scoring calls (72% of the previous run) would have been avoided.
- Ineligible reasons: 273 out-of-state onsite, 191 outside US, 98 foreign remote, 13 residency
  requirements without NC, ~30 state-limited remote.
- Missing location (485 rows): 470 recovered from Workday URL slugs, 15 without evidence. Of 472 scored:
  463 ineligible (mostly MA/MN/NY/TX offices and non-US), 7 eligible_us_remote (description says remote
  within the US), 2 unknown.

### The 20 jobs scoring ≥ 7
| Score | Title | Employer | Location | Eligibility | Reason |
|---|---|---|---|---|---|
| 8 | Technical Services Technician II | indeed | Greensboro, NC | eligible_nc (high) | NC commute tier |
| 8 | Business Technician, Level 2 | indeed | Raleigh, NC | eligible_nc (acceptable) | NC commute tier |
| 8 | IT Network Engineer | indeed | Raleigh, NC | eligible_nc (acceptable) | NC commute tier |
| 8 | IT Support Specialist | indeed | Remote, USA | eligible_us_remote | nationwide US remote |
| 8 | Customer Support Engineer | indeed | Remote, USA | eligible_us_remote | nationwide US remote |
| 8 | Field Service Engineer - US Remote | Motorola | Colorado Remote Work, More… | eligible_us_remote | title says "US Remote" |
| 8 | Advisory Technical Support Engineer | indeed | Morrisville, NC | unknown | NC outside commute tiers |
| 8 | IT Infrastructure & Network Specialist (Onsite) | indeed | Timberlake, NC | unknown | NC outside commute tiers |
| 8 | Technology Support Specialist | indeed | Chapel Hill, NC | unknown | NC outside commute tiers |
| 8 | Customer Support Technician I | Motorola | Utah Remote Work, More… | restricted_remote | state list truncated, NC unconfirmed |
| 8 | Engineer, IT Lab Support | Moderna | (none → Workday: Norwood, MA) | ineligible | MA, "100% onsite" |
| 7 | Product Support Specialist Tech 1 | indeed | Greensboro, NC | eligible_nc (high) | NC commute tier |
| 7 | IT Support Manager | indeed | High Point, NC | eligible_nc (acceptable) | NC commute tier |
| 7 | Service Desk Analyst II | indeed | Remote, USA | eligible_us_remote | nationwide US remote |
| 7 | Service Desk Engineer II (The Woodlands, TX or Remote) | indeed | Remote, USA | eligible_us_remote | remote option |
| 7 | Services Project Manager | Ciena | Remote-US | eligible_us_remote | nationwide US remote |
| 7 | Desktop Support Analyst I | indeed | Clayton, NC | unknown | NC outside commute tiers |
| 7 | IT Help Desk Technician | indeed | Trinity, NC | unknown | NC outside commute tiers |
| 7 | Network Administrator | indeed | Lillington, NC | unknown | NC outside commute tiers |
| 7 | System Technologist | Motorola | Utah Remote Work, More… | restricted_remote | state list truncated |

Totals: 5 eligible_nc + 6 eligible_us_remote ready to tailor; 6 NC-commute reviews; 2 restricted; 1 ineligible.

### False-positive / false-negative risks
- **FP:** Indeed's "Remote, USA" label is trusted when the description has no state list; 30/90 remote-
  eligible descriptions mention onsite/hybrid wording that was judged benign (e.g. "onsite support",
  "hybrid cloud"), and "strong preference for candidates in VA/MD/DC" is a preference, not a requirement.
  Title "US Remote" overrides a truncated state list (Motorola).
- **FN:** Workday slugs give only the primary location — multi-site postings with an NC site may be
  marked ineligible. Residency patterns are English-phrase based; unusual wording is missed (→ the job
  keeps its location-based class). Unlisted NC towns default to review, not eligible.
- Mitigations: review queue, `--evidence`, manual approve/reject, and the apply agent re-checks the
  live posting and stops on contradictions (`RESULT:FAILED:not_eligible_location` / `location_unclear`).

## Dev environment
- `~/.applypilot-dev`: `.env` (backup `.env.backup-20261127-132213`), `searches.yaml`
  (backup `searches.yaml.backup-20261127-134858`, `eligibility:` section added). Production
  `~/.applypilot` untouched.
- MCP: project-scoped `serena-applypilot` in `.mcp.json` (gitignored); Claude Code shows it *pending
  approval* — restart Claude Code in this repo and approve it. User-scope VIctory/xcl/fxceless servers untouched.
- Tooling: Claude Code 2.1.332, Node 24, Chrome, uv, ripgrep, git; `gh` not installed.
  `python-jobspy` 1.5.0 installed in `.venv` with normal dependency resolution (it no longer pins numpy;
  `pip check` clean, numpy 2.5.5 / pandas 3.1.1 unchanged); `doctor` reports it OK. context-mode/RTK
  condense output (use `ctx_execute` for large data, `Read` with offset/limit for source).

## Known remaining issues
- **Phase 3b residual limits** (details: `APPLY_AGENT_SECURITY_PLAN.md` § Phase 3b):
  - Dry runs block state-changing *requests*, not side effects of plain GETs: a site whose API changes
    state via JS GET with query data is not recognised (non-standard; GET form submissions are caught).
  - WebSocket/WebTransport traffic cannot be held by Fetch: the session is stopped when one opens, but a
    few frames could leave before that (to allowlisted hosts only; the proxy still enforces hosts).
  - Real runs ask for one approval per state-changing request; multi-step ATS (Workday autosave, S3
    resume upload) will need several approvals per application. Conservative by design.
  - Strict allowlists may break pages that need unlisted CDNs; such sessions fail → `needs_review`. Extend
    `network_policy.STATIC_ASSET_HOSTS` (exact provider hosts only) after reviewing a blocked log.
  - Shared workers are untested (fail closed if Fetch cannot attach). Trusted host only: a tampered venv,
    Chrome binary or a local process using the CDP port defeats the guard.
  - The label-based click guard remains as an extra layer; the network boundary does not depend on it.
- Dry runs are now label-free at the network layer but still **supervised** (first runs on real ATS pages
  will reveal compatibility gaps).
- Hooks run as `sys.executable -m applypilot...`; tampering with the venv defeats them (trusted host).
- User-global `~/.claude/CLAUDE.md` may still be loaded into the agent's context (no secrets in it).
- No pre-apply liveness/freshness check; scores/descriptions are from 2026-11-23.
- Duplicate detection is application-URL only (same role under different URLs not caught).
- No `needs_review` listing command yet (`status` shows the count).
- Repo-wide ruff has ~147 pre-existing upstream findings (CI red); touched files add none.
