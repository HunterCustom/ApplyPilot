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
| Tailoring / cover / PDF | Never run on this data. 11 high-scoring jobs are now eligible to tailor. |
| Apply | Safety-hardened but **not yet run**. Browser tests blocked on `APPLY_AGENT_SECURITY_PLAN.md` S1–S4. |
| Tests | 96 pass (+18 subtests): apply safety, eligibility, LLM provider, scoring. |

## Safety model (apply)
- **Statuses**: NULL/`failed` (retryable), `dry_run`, `in_progress`, `applied`, `needs_review`, `manual`.
  Lifecycle documented at the top of the DB section in `apply/launcher.py`.
- **Outcome**: read only from the agent's final stream-json `result` event; only standalone `RESULT:` lines
  count. Missing / malformed / conflicting results, agent errors, watchdog kills, interruptions after the
  agent started, and any non-APPLIED result after a submit-like action → `needs_review` (never auto-retried).
- **Ownership**: claims carry `<session>:worker-N[:dry_run]`; results and releases only apply to the
  caller's own in-progress claim, so late results never overwrite another worker, a stale-lock recovery
  or a manual decision. Stale locks are recovered at start (dry → released, real → needs_review), never
  the current process's own claims.
- **Dry runs**: never recorded as applied (launcher-enforced). Email sending disabled via
  `--disallowedTools` (enforced by Claude Code). A PreToolUse hook (`apply/dry_run_guard.py`) blocks
  submit-like clicks, Enter, `submit=true` typing and click/submit scripts — **verified live** that hooks
  block under `bypassPermissions`. A submit-like action the guard did not block → `needs_review`.
  **Limitation:** the guard judges the tool input's description; a button that submits but is labelled
  e.g. "Continue" cannot be recognised. Dry runs are therefore *not* submission-proof and must be supervised.
- **CAPTCHA**: solving/CapSolver removed everywhere. Agent detects CAPTCHA / access restrictions, outputs
  `RESULT:CAPTCHA` and stops; the worker moves on.
- **Chrome**: only processes whose command line shows the ApplyPilot worker profile are ever killed; a
  foreign listener on the CDP port makes launch refuse. No import-time exit hooks.
- **Agent isolation**: `--strict-mcp-config` (user's MCP servers excluded). Still `bypassPermissions` with
  built-in tools available — see security plan S1.

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
- Tooling: Claude Code 2.1.332, Node 24, Chrome, uv, ripgrep, git; `gh` not installed. context-mode/RTK
  condense output (use `ctx_execute` for large data, `Read` with offset/limit for source).

## Known remaining issues
- Apply-agent exposures E1–E7 (`APPLY_AGENT_SECURITY_PLAN.md`), notably full built-in tools under
  bypassPermissions and the cloned real Chrome profile.
- No pre-apply liveness check; scores/descriptions are from 2026-11-23.
- Duplicate detection is application-URL only (same role under different URLs not caught).
- No `needs_review` listing command yet (`status` shows the count).
- Repo-wide ruff has ~147 pre-existing upstream findings (CI red); touched files add none.
