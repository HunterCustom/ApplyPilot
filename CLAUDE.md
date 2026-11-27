# ApplyPilot-Dev — Claude Code guide

Personal fork of upstream `Pickle-Pixel/ApplyPilot` (remote `upstream`), tuned for
NC / US-remote IT support & infrastructure roles. Origin: `HunterCustom/ApplyPilot`.
Status and plan live in `PROJECT_STATUS.md`, `DEVELOPMENT_ROADMAP.md` and
`APPLY_AGENT_SECURITY_PLAN.md` — read those first.

## Hard rules
- **Never submit real applications during development.** No browser-based apply runs (dry or real)
  until `APPLY_AGENT_SECURITY_PLAN.md` S1–S4 are done; then supervised `--dry-run` only.
  Dry runs are NOT submission-proof (guard hook is best-effort) — never describe them as safe to leave unattended.
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
.\.venv\Scripts\python.exe -m pytest tests/ -q          # 96 tests, unittest-style, temp DBs only
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
  spawns `claude -p` via `build_agent_command` (`--strict-mcp-config`; dry runs add the guard hook
  settings and block `send_email`), classifies via `classify_session` / `parse_agent_result`.
  `apply/dry_run_guard.py` is the PreToolUse hook. `apply/prompt.py` builds the agent prompt.
- `database.py` — single `jobs` table, `url` is the PK; stage state is columns (fit_score, tailored_resume_path, apply_status…).
- LLM provider precedence (`llm.py`): `LLM_URL` wins over GEMINI/OPENAI keys; `LLM_MODEL` overrides model.

## Tooling notes
- Output from shell/Read is condensed by RTK/context-mode. Use `ctx_execute` to compute answers
  over large data; use `Read` with `offset/limit` for source files; `rtk bypass` only if output is unusable.
- User-scope Serena/Graphify servers belong to other projects (F:\ drive) — never modify them.
  Project-scoped `serena-applypilot` is defined in `.mcp.json` (gitignored; needs approval on first start).
  Prefer its symbol tools for navigation once approved; otherwise Grep/Read. `.serena/` is gitignored.
- Commit messages: write to a scratchpad file and `git commit -F <file>` (PowerShell here-strings piped to `-F -` fail).
- `gh` CLI is not installed; use git + the GitHub web UI for PRs.
