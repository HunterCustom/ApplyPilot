# Project Status

_Audit date: 2026-11-27 · Branch `feature/claude-development` · Audit baseline `81c13b8` · Fork is level with `upstream/main` (4a8d521) plus fork commits._

## Phase 0 result (2026-11-27) — DONE, verified by tests
| Item | Status |
|---|---|
| Dry run never records `applied` / `applied_at`; launcher enforces even if agent says APPLIED | Fixed (`021d9a4`) |
| Dry run status `dry_run`, no retry attempts consumed, job re-eligible for real runs | Fixed |
| `apply --url` with NULL status; applied / in_progress / needs_review never acquired | Fixed |
| Duplicate guard on shared `application_url` | Added (URL-level only; company/title dedupe is Phase 3) |
| Concurrent acquisition (8 threads, 6 jobs → 6 unique claims) | Verified |
| Manual-ATS skip continues to next job | Fixed |
| Uncertain outcomes (no/conflicting RESULT, timeout, watchdog, Ctrl+C after agent start, launcher error) → `needs_review`, not retried; `--reset-failed` leaves them alone | Added |
| Stale `in_progress` locks recovered at `apply` start (dry → released, real → needs_review) | Added |
| Hung agent watchdog (`APPLYPILOT_APPLY_MAX_SECONDS`, default 1200 s) | Added |
| `status` shows dry-run and needs-review counts | Added |
| Dev `.env` duplicate `LLM_MODEL` | Resolved (backup `.env.backup-20261127-132213` in dev dir) |
| Effective LLM: Local `http://127.0.0.1:1234/v1`, model `qwen/qwen3-14b` (confirmed in `/v1/models`, test inference returned `OK` in 8.2 s) | Verified |
| `doctor` reports effective provider/model, checks model is served, warns on ignored keys | Fixed (`b811031`) |
| Tests | 35/35 pass (22 apply-safety + 4 provider + 9 existing); new test files lint-clean; no new ruff findings in modified source files |

Resolving a `needs_review` job: check the employer portal / email, then `applypilot apply --mark-applied URL`
or `--mark-failed URL` (then `--reset-failed` to requeue).

_The audit findings below are kept as the original baseline; items 1, 2, 4, 5 are resolved by Phase 0._

## Dev data snapshot (`~/.applypilot-dev`, read-only inspection)
| Metric | Value |
|---|---|
| Jobs discovered | 896 (one discovery run, 2026-11-23) |
| With full description | 853 (43 enrichment errors) |
| Scored | 853 — distribution 8:11, 7:9, 6:54, 5:66, 4:44, 3:109, 2:292, 1:268 |
| Score ≥ 7 (apply candidates) | 20 — 13 NC, 4 "Remote, USA", 3 state-specific remote / unknown |
| Tailored / cover letters / applied | 0 / 0 / 0 |
| Scored jobs with no location | 472 (Thomson Reuters 312, Moderna 165 — Workday employers) |

Source mix is dominated by upstream's default `employers.yaml` (Thomson Reuters, Moderna, several Canadian banks), which is mostly noise for NC IT roles.

## Verified working
- `doctor`: profile, resume, searches, Claude Code CLI, Chrome, Node/npx present → Tier 3 (full auto-apply) unlocked. JobSpy shows WARN in doctor but discovery produced 116 Indeed jobs.
- Local LLM server (LM Studio) reachable; qwen3-14b loaded. Qwen `/no_think` injection covered by tests.
- Scoring: retries malformed/empty responses, never persists failures, prompt forbids crediting unsupported claims and treats remote ≠ eligible.
- Candidate evidence: validated YAML (enabled, 4 professional / 15 certs / 6 personal projects), bounded and labelled in prompt.
- Priority ordering: NC titles/locations ranked first before `--limit`; ordering only, nothing filtered.
- Apply infrastructure exists upstream: atomic job acquisition, per-worker Chrome + Playwright MCP, `RESULT:` parsing, permanent-failure classification, blocked sites, manual-ATS skip, `--dry-run`, dashboard, `--mark-applied/--mark-failed/--reset-failed` utilities.
- Tailoring/cover validator exists (banned words, fabrication watchlist, strict/normal/lenient).
- Playwright Chromium installed (needed for PDF stage).
- Tests: 9/9 pass (pytest and unittest).

## Broken / missing (ranked by impact)
1. **CRITICAL — dry-run records jobs as applied.** `apply/prompt.py:512` tells the agent to output `RESULT:APPLIED` in dry-run; `launcher.worker_loop` then calls `mark_result(..., "applied")`. A dry run would permanently remove jobs from the queue and corrupt duplicate tracking. Must fix before any apply testing.
2. **Targeted apply is broken and unsafe.** `acquire_job(target_url=...)` uses `apply_status != 'in_progress'`, which is false for NULL (never-attempted jobs never match) and true for `applied` (re-application allowed).
3. **No hard eligibility gate.** Priority only reorders. Apply prompt says `"Remote" → ELIGIBLE`, ignoring state-restricted remote roles (e.g. "Colorado Remote Work, More…"). Apply prompt reads `searches.location.accept_patterns`, but the config uses `location_accept` → only the profile city is treated as acceptable for onsite/hybrid (other NC cities rejected by the agent).
4. **LLM config ambiguous.** Dev `.env` defines `LLM_MODEL` twice; effective config sends model name `gemini-3.5-flash-lite` to the local LM Studio endpoint. `doctor` mislabels it as Gemini. Which model produced the existing scores is unrecorded.
5. **Manual-ATS skip ends the run.** `acquire_job` returns `None` after marking a manual ATS, which the worker treats as "queue empty".
6. **No liveness re-check before apply.** Enrichment runs once; expiry is only discovered by the browser agent mid-application (costly). Scores are already 4 days old.
7. **Duplicate prevention is URL-only.** Same role from Indeed and the employer's Workday = two rows (46 title+location duplicate groups; 2 duplicate application_urls). No company-normalised dedupe, no "applied to this company/role recently" guard.
8. **Tailoring never run.** Validator's skill boundary uses `profile.skills_boundary` where `programming_languages`/`frameworks` are empty — behaviour unverified for this profile.
9. **Credential exposure in logs.** Apply prompt includes profile password for account creation; full agent output is written to `logs/claude_*.txt`. Keep logs out of git (already ignored) and consider redacting.
10. **Lint:** `ruff check src/` fails with 147 findings (pre-existing upstream; 9 in fork-modified scoring files) → CI is red. Mojibake in a comment banner in `scorer.py:23`.
11. **Discovery targeting:** default employers list is irrelevant to NC; 472 scored jobs have no location, so the eligibility of 54% of the corpus is unknown.

## Tooling available
- Claude Code 2.1.332 (also the apply agent), Node 24 / npx (Playwright MCP), Chrome, uv, ripgrep, git. `gh` not installed.
- MCP: context-mode (`ctx_execute`/`ctx_search` for large outputs), RTK output condensing, Serena ×3 + graphify ×3 (all bound to unrelated projects), Claude in Chrome / built-in browser (for manual verification only).
- `.serena/` — local Serena cache + Codex-era memories with machine paths → now gitignored.

### Dedicated Serena for ApplyPilot (investigated, not yet enabled)
Existing user-scope servers (`serena-victory`, `serena-xcl`, `serena-fxceless`, `graphify-*`) are pinned to
`F:\` projects and must stay untouched. ApplyPilot is already registered in `~/.serena/serena_config.yml`
and has `.serena/project.yml`. Add a **project-scoped** server in `ApplyPilot-Dev/.mcp.json`
(already gitignored by `.mcp*.json`); Claude Code asks for approval on next start:
```json
{"mcpServers": {"serena-applypilot": {
  "command": "C:\\Users\\Hunter\\.local\\bin\\serena.exe",
  "args": ["start-mcp-server", "--project", "C:\\Users\\Hunter\\ApplyPilot-Dev", "--context", "claude-code",
           "--agent-interface", "tools", "--transport", "stdio", "--open-web-dashboard", "false"]}}}
```
Graphify: optional, low value for a ~10k-line repo. It would need a graph built into `graphify-out/`
(via the graphify skill), then `graphify-mcp.exe --graph <repo>\graphify-out\graph.json` in the same `.mcp.json`;
`graphify-out/` would need gitignoring.

## Remaining safety risks (post Phase 0)
- **No hard eligibility gate** (Phase 2): the apply prompt still treats any "Remote" as eligible and ignores `location_accept`.
- **CAPTCHA wording in apply prompt**: the prompt tells the agent to "solve" CAPTCHAs and includes a CapSolver path when `CAPSOLVER_API_KEY` is set (it is not set). This conflicts with the no-bypass constraint; must be changed to detect-and-stop before any browser test (Phase 5 prerequisite).
- Agent runs with `--permission-mode bypassPermissions` and a Gmail MCP (read access to the mailbox for verification codes); destructive Gmail tools are disallowed.
- Profile password in the apply prompt and full agent transcripts written to `logs/`.
- Duplicate detection is URL-based only; the same role under different URLs can still be applied to twice (Phase 3).
- `needs_review` resolution is manual and there is no dedicated CLI listing yet (`status` shows the count).
