# Project Status

_Audit date: 2026-11-27 · Branch `feature/claude-development` @ `81c13b8` · Fork is level with `upstream/main` (4a8d521) plus 18 fork commits (~500 lines, scoring only)._

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
