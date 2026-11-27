# ApplyPilot-Dev — Claude Code guide

Personal fork of upstream `Pickle-Pixel/ApplyPilot` (remote `upstream`), tuned for
NC / US-remote IT support & infrastructure roles. Origin: `HunterCustom/ApplyPilot`.
Status and plan live in `PROJECT_STATUS.md`, `DEVELOPMENT_ROADMAP.md` and
`APPLY_AGENT_SECURITY_PLAN.md` — read those first.

## Hard rules
- **Never submit real applications during development.** No browser-based apply runs (dry or real)
  S1–S4 + Phase 3b are done: supervised `--dry-run` only, on a job the user picked. Live browser tests
  otherwise only against local synthetic pages (`scripts/live_policy_probe.py`), never employer forms.
  The launcher's NetworkGuard (egress proxy + CDP Fetch) blocks state-changing requests in dry runs, but
  dry runs are still NOT certified submission-proof (see PROJECT_STATUS residual limits) — never describe
  them as safe to leave unattended. Never weaken the agent command line, policy hooks, NetworkGuard,
  approvals or fail-closed checks; never run the apply agent or Chrome by hand outside the launcher.
- Apply status lifecycle is documented at the top of the DB section in `apply/launcher.py`.
  Never weaken: dry runs never set `applied`/`applied_at`; uncertain outcomes → `needs_review`, never
  auto-retried; results/releases pass `owner=job["agent_id"]`; outcome comes only from the final result event.
- Geographic eligibility is decided by `eligibility.py`, never by the LLM. Tailoring and applying
  (including `--url`) require an eligible status. Bump `CLASSIFIER_VERSION` when classification logic changes.
- Only kill processes ApplyPilot started (`chrome.is_worker_chrome`); never sweep ports blindly.
- **Always set `APPLYPILOT_DIR` to the dev dir before importing the package**:
  `$env:APPLYPILOT_DIR = "$HOME\.applypilot-dev"`. `config.APP_DIR` is resolved at import time.
  `~\.applypilot` is PRODUCTION data — never read-write it, never point tools at it.
- Verify with `python -c "import applypilot.config as c; print(c.DB_PATH)"` before any stage run.
- For ad-hoc DB inspection open SQLite read-only: `sqlite3.connect("file:...?mode=ro", uri=True)`.
- `applypilot run`, `status`, `apply` call `_bootstrap()` (creates dirs + DB). `run --dry-run` is NOT read-only. `doctor` is read-only.
- Never commit profile.json, resume.*, .env, *.db, candidate_evidence.yaml, priority.yaml, logs.
  Never print `.env` values or profile PII into chat, commits, or PRs.
- Never fabricate qualifications. Evidence comes only from user-verified `candidate_evidence.yaml`.
- Do not add CAPTCHA bypass or anti-bot evasion. CAPTCHA/login walls → mark and skip.
- Prefer upstream code paths over rewrites; keep fork diffs small so upstream can be merged.

## Commands (PowerShell, repo root)
```powershell
$env:APPLYPILOT_DIR = "$HOME\.applypilot-dev"
.\.venv\Scripts\python.exe -m pytest tests/ -q          # 190 tests (~65 s, real headless Chrome), temp DBs only
.\.venv\Scripts\python.exe -m ruff check <changed files> # repo-wide ruff has ~147 pre-existing findings; don't add new ones
.\.venv\Scripts\applypilot.exe doctor                    # read-only setup check
.\.venv\Scripts\applypilot.exe status                    # pipeline counts
.\.venv\Scripts\applypilot.exe run score --limit 20      # stages: discover enrich score tailor cover pdf
.\.venv\Scripts\applypilot.exe eligibility --evidence    # review queue; --status X, --approve/--reject URL
```
To evaluate against real data, copy the dev DB (sqlite backup API from a `mode=ro` connection) into the
scratchpad and point `database.DB_PATH` at the copy; never classify the original in place during validation.
Python 3.13 venv at `.venv`. Default LLM: local LM Studio at `http://127.0.0.1:1234/v1`, model `qwen/qwen3-14b`
(LM Studio must be running with it loaded; `doctor` verifies). Tests that touch the DB must patch
`applypilot.database.DB_PATH` to a temp file and `atexit.unregister(chrome.cleanup_on_exit)` — see `tests/test_apply_safety.py`.

## Architecture (src/applypilot)
- `cli.py` (Typer) → `pipeline.py` runs stages; `apply/launcher.py` is separate.
- `discovery/` jobspy (boards), workday (employers.yaml), smartextract (sites.yaml).
  Location filtering at discovery uses `searches.yaml: location_accept / location_reject_non_remote`.
- `enrichment/detail.py` — full description + application_url.
- `eligibility.py` (fork) — deterministic geographic classifier; config `searches.yaml: eligibility:`
  (falls back to `location_accept`); runs at the start of score/tailor/apply; `SQL_ELIGIBLE` gates stages.
- `scoring/scorer.py` — LLM fit score 1–10; failed scores are not persisted (score=0 marker).
  `scoring/priority.py` (fork) — ordering only, never filters. `scoring/candidate_evidence.py` (fork).
- `scoring/tailor.py`, `cover_letter.py`, `validator.py` (banned words + FABRICATION_WATCHLIST), `pdf.py` (Playwright Chromium).
- `apply/launcher.py` — acquires a job (SQLite `BEGIN IMMEDIATE`), launches Chrome with CDP,
  spawns a least-privilege `claude -p` via `build_agent_command` (`--tools ""`, `--setting-sources ""`,
  `dontAsk` + allowlist), verifies the init event (`verify_init_event`), audits hooks (`audit_session`),
  classifies via `classify_session` / `parse_agent_result`. `apply/policy.py` + `apply/policy_hook.py`
  are the per-session policy and hooks (dry-run guard included from `dry_run_guard.py`);
  `apply/network_guard.py` (+ `network_policy`, `egress_proxy`, `cdp`) the launcher-owned network and
  submission boundary; `apply/approval.py` single-use expiring approvals;
  `apply/verification_mcp.py` the optional readonly code helper; `apply/redact.py` log redaction;
  `apply/prompt.py` builds the agent prompt. Session files: `APP_DIR/apply-sessions/<id>/`.
- `database.py` — single `jobs` table, `url` is the PK; stage state is columns (fit_score, tailored_resume_path, apply_status…).
- LLM provider precedence (`llm.py`): `LLM_URL` wins over GEMINI/OPENAI keys; `LLM_MODEL` overrides model.

## Token-efficient workflow
1. Navigate with `serena-applypilot` symbol tools (`get_symbols_overview`, `find_symbol` with
   `include_body`, `find_referencing_symbols`) before reading whole files.
2. Use `graphify-applypilot` (`query_graph`, `get_neighbors`, `shortest_path`) for cross-module questions.
   Rebuild after structural changes: `graphify update .` (code only, no LLM; scope in `.graphifyignore`).
3. Noisy shell commands go through RTK (automatic); `rtk bypass <cmd>` only when output is unusable.
4. Large outputs/logs/data: `ctx_execute` and print only the answer; `Read` with `offset/limit` for source.
5. Don't reread files that haven't changed; don't repeat tool calls whose result is already known.
6. Targeted tests while implementing (`pytest tests/test_x.py -k ...`); full suite + ruff diff before commit.
7. Keep `PROJECT_STATUS.md` / `DEVELOPMENT_ROADMAP.md` / `APPLY_AGENT_SECURITY_PLAN.md` concise and current.
8. No subagents unless the user asks; no redundant tool calls.
9. Check context quality (`measure.py quick` / Token Optimizer) at task boundaries.
10. Below 65/100: finish the current atomic task, test, commit, update docs, hand off and restart.
Compression must never hide what correctness needs: for security reviews, debugging, test failures and
diffs, read the full output (`rtk bypass`, `Read` ranges, `ctx_search` on the archived result).

Dev tools are for this trusted environment only. Never give the apply agent dev MCP servers, shell,
repository or personal-file access: it runs with `--tools ""`, `--setting-sources ""` and
`--strict-mcp-config`, and `verify_init_event` kills any session that sees more.

## Tooling notes
- Quality checks: `.venv\Scripts\pyright.exe <files>` (basic mode, `[tool.pyright]`; ~85 pre-existing
  repo errors — don't add new ones), `pytest --cov=applypilot.apply --cov-report=term`.
- Output from shell/Read is condensed by RTK/context-mode. Use `ctx_execute` to compute answers
  over large data; use `Read` with `offset/limit` for source files; `rtk bypass` only if output is unusable.
- User-scope Serena/Graphify servers belong to other projects (F:\ drive) — never modify them.
  Project-scoped `serena-applypilot` and `graphify-applypilot` are defined in `.mcp.json` (gitignored)
  and enabled in `.claude/settings.local.json`. `.serena/` and `graphify-out/` are gitignored.
- Commit messages: write to a scratchpad file and `git commit -F <file>` (PowerShell here-strings piped to `-F -` fail).
- `gh` CLI is not installed; use git + the GitHub web UI for PRs.
