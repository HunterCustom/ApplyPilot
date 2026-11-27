# ApplyPilot-Dev — Claude Code guide

Personal fork of upstream `Pickle-Pixel/ApplyPilot` (remote `upstream`), tuned for
NC / US-remote IT support & infrastructure roles. Origin: `HunterCustom/ApplyPilot`.
Status and plan live in `PROJECT_STATUS.md` and `DEVELOPMENT_ROADMAP.md` — read those first.

## Hard rules
- **Never submit real applications during development.** Only `applypilot apply --dry-run`,
  and no browser-based apply tests until the CAPTCHA prompt fix (roadmap Phase 5 prerequisite).
- Apply status lifecycle is documented at the top of the DB section in `apply/launcher.py`.
  Never weaken: dry runs never set `applied`/`applied_at`; uncertain outcomes → `needs_review`, never auto-retried.
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
.\.venv\Scripts\python.exe -m pytest tests/ -q          # 35 tests, unittest-style, temp DBs only
.\.venv\Scripts\python.exe -m ruff check <changed files> # repo-wide ruff has ~147 pre-existing findings; don't add new ones
.\.venv\Scripts\applypilot.exe doctor                    # read-only setup check
.\.venv\Scripts\applypilot.exe status                    # pipeline counts
.\.venv\Scripts\applypilot.exe run score --limit 20      # stages: discover enrich score tailor cover pdf
```
Python 3.13 venv at `.venv`. Default LLM: local LM Studio at `http://127.0.0.1:1234/v1`, model `qwen/qwen3-14b`
(LM Studio must be running with it loaded; `doctor` verifies). Tests that touch the DB must patch
`applypilot.database.DB_PATH` to a temp file and `atexit.unregister(chrome.cleanup_on_exit)` — see `tests/test_apply_safety.py`.

## Architecture (src/applypilot)
- `cli.py` (Typer) → `pipeline.py` runs stages; `apply/launcher.py` is separate.
- `discovery/` jobspy (boards), workday (employers.yaml), smartextract (sites.yaml).
  Location filtering at discovery uses `searches.yaml: location_accept / location_reject_non_remote`.
- `enrichment/detail.py` — full description + application_url.
- `scoring/scorer.py` — LLM fit score 1–10; failed scores are not persisted (score=0 marker).
  `scoring/priority.py` (fork) — ordering only, never filters. `scoring/candidate_evidence.py` (fork).
- `scoring/tailor.py`, `cover_letter.py`, `validator.py` (banned words + FABRICATION_WATCHLIST), `pdf.py` (Playwright Chromium).
- `apply/launcher.py` — acquires a job (SQLite `BEGIN IMMEDIATE`), launches Chrome with CDP,
  spawns `claude -p --mcp-config <playwright MCP> --permission-mode bypassPermissions`, parses `RESULT:` lines.
  `apply/prompt.py` builds the agent prompt (profile, location check, submit rules).
- `database.py` — single `jobs` table, `url` is the PK; stage state is columns (fit_score, tailored_resume_path, apply_status…).
- LLM provider precedence (`llm.py`): `LLM_URL` wins over GEMINI/OPENAI keys; `LLM_MODEL` overrides model.

## Tooling notes
- Output from shell/Read is condensed by RTK/context-mode. Use `ctx_execute` to compute answers
  over large data; use `Read` with `offset/limit` for source files; `rtk bypass` only if output is unusable.
- User-scope Serena/Graphify servers belong to other projects (F:\ drive) — never modify them.
  A project-scoped `serena-applypilot` config is in `PROJECT_STATUS.md`; until enabled use Grep/Read.
  `.serena/` is local-only and gitignored.
- `gh` CLI is not installed; use git + the GitHub web UI for PRs.
