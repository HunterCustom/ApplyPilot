# ApplyPilot — Deployment and Operating Guide (fork)

Preparation pipeline: discover → enrich → verify → dedupe → classify → score → tailor → cover → pdf →
prepare (review queue). Applying is a separate, supervised, human-approved step. Nothing in this guide
submits an application unless you run `applypilot apply` without `--dry-run` **and** approve each request.

## 1. Requirements
- Windows 10/11 (the browser process jail is Windows-only; `apply` refuses to start without it).
- Python 3.11+ (developed on 3.13), Git.
- Playwright Chromium (`playwright install chromium`, matching the installed `playwright` version): used
  offline for PDF rendering (scripts disabled, every request aborted) and by discovery/enrichment for
  script-rendered pages. A version mismatch makes PDF rendering fail, and a job without a bound PDF is
  never ready.
- Browser applying only (see section 8a): Google Chrome signed by Google LLC
  (`C:\Program Files\Google\Chrome\Application\chrome.exe` or `CHROME_PATH`; any other or unsigned
  binary is refused), Node.js 18+, Claude Code CLI, and an **attested isolated browser host**.
- A local OpenAI-compatible LLM. Verified baseline: **LM Studio, `qwen/qwen3-14b`** at
  `http://127.0.0.1:1234/v1`. Other models: set `LLM_MODEL` explicitly (never changed silently).

## 2. Install
```powershell
git clone https://github.com/HunterCustom/ApplyPilot.git; cd ApplyPilot
git checkout feature/claude-development
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pip install python-jobspy       # board discovery (1.5.0 resolves normally)
.\.venv\Scripts\python.exe -m playwright install chromium   # PDF rendering only
.\.venv\Scripts\python.exe -m pip check
```

## 3. Configure (one data directory per environment)
Always set the data directory first — `~\.applypilot` is the default and is treated as production:
```powershell
$env:APPLYPILOT_DIR = "$HOME\.applypilot-dev"        # or your production dir, deliberately
.\.venv\Scripts\python.exe -c "import applypilot.config as c; print(c.DB_PATH)"   # verify before any run
.\.venv\Scripts\applypilot.exe init                   # first time: profile, resume, searches
.\.venv\Scripts\applypilot.exe doctor                 # read-only check (LLM, Chrome, files)
```
Files in `APPLYPILOT_DIR` (never commit them):
| File | Purpose |
|---|---|
| `.env` | `LLM_URL=http://127.0.0.1:1234/v1`, `LLM_MODEL=qwen/qwen3-14b` (LLM_URL wins over cloud keys) |
| `profile.json`, `resume.txt` | Sources of truth for documents and the agent |
| `candidate_evidence.yaml` | User-verified extra facts (`enabled: true`; professional_experience / certifications / personal_projects). Mark unfinished certifications "in progress". |
| `searches.yaml` | Queries, locations, `eligibility:` rules, optional `discovery: sources: [jobspy, workday, smartextract]` |
| `pipeline.yaml` | Optional thresholds/limits (see `src/applypilot/opsconfig.py` docstring): `min_score`, `freshness_max_days`, `verify_max_age_hours`, `require_cover_letter`, `daily.*` limits |
| `screening_answers.yaml` | Verified screening answers (see `src/applypilot/screening.py`); only `reusable: true` answers reach the agent, and a sensitive one only with `sensitive_ok: true` |
| `priority.yaml` | Ordering hints; `salary:` bands and `commute:` tier points rank eligible jobs (never filter; see `src/applypilot/ranking.py`). Your own pay figures belong here, not in the repository |
| `employers.yaml` | Optional Workday watchlist (`use_packaged: false` to skip the packaged registry). Entries are crawled only after `applypilot employers --verify` |
| `sites.yaml` | Optional extra https career-site searches for smart extract; packaged block lists always apply |
| `isolated-host.json` | Only on a dedicated VM: written by `applypilot isolation --attest` (section 8a) |

## 4. Database migration and backup
- Migrations are additive and automatic (`database.ensure_columns` / `ensure_aux_tables` on every start),
  serialized in one write transaction and, for `daily` / `run` / `rescore`, run only while the run lock
  is held. Columns and tables are only added, never dropped or renamed.
- **Back up before upgrading** (consistent online copy):
  ```powershell
  .\.venv\Scripts\python.exe -c "import sqlite3,os;s=sqlite3.connect('file:'+os.environ['APPLYPILOT_DIR']+'/applypilot.db?mode=ro',uri=True);d=sqlite3.connect(os.environ['APPLYPILOT_DIR']+'/applypilot.backup.db');s.backup(d);d.close();print('ok')"
  ```
- Also copy `tailored_resumes\`, `cover_letters\`, `apply-sessions\` if you need document history.

## 5. Daily operation
```powershell
.\.venv\Scripts\applypilot.exe daily --plan          # what would run, with limits
.\.venv\Scripts\applypilot.exe daily                 # full preparation run (no submissions)
.\.venv\Scripts\applypilot.exe review                # ready applications awaiting your decision
.\.venv\Scripts\applypilot.exe report top            # also: new eligible duplicates expired prepared
                                                      # validation review applied uncertain blocked
                                                      # inference failures  (--format csv|json -o file)
```
Exit codes of `daily`: 0 ok · 1 a stage failed (others ran) · 2 configuration · 3 already running ·
4 LLM unavailable (LLM stages skipped). Logs: `APPLYPILOT_DIR\logs\daily-YYYYMMDD.jsonl`; run history in
the `pipeline_runs` / `discovery_runs` tables (`applypilot report failures`).

Individual stages: `applypilot run verify dedupe classify score tailor cover pdf prepare`.
`daily` and `run` share one OS-held lock: a second run (scheduled or manual) exits with code 3. The
lock is released by the operating system if a run crashes; the next run reports the recovery and
marks the interrupted `pipeline_runs` row `interrupted`. A stage that reports item errors or stops at
`max_runtime_minutes` counts as partial (exit 1).

Other commands:
- `applypilot rescore --limit 100` -- rescores only scores made by another model, prompt, evidence or
  resume version; the old score is kept in `score_history`.
- `applypilot employers [--verify]` -- your Workday watchlist and its verification status.
- `applypilot isolation` -- whether browser sessions are allowed on this host (and the requirements).

## 6. Scheduling (Windows Task Scheduler) — not activated by the installer
1. Edit `scripts\ApplyPilotDaily.task.xml`: replace `REPO_DIR` and `DATA_DIR`.
2. Import: Task Scheduler → *Import Task…*, or
   `schtasks /Create /TN "ApplyPilot Daily" /XML scripts\ApplyPilotDaily.task.xml`.
3. The task runs `scripts\run_daily.ps1 -DataDir <dir>` (explicit data dir, never the default), as you,
   not elevated, never overlapping. LM Studio must be running with the model loaded, or exit code 4.
4. Remove: `schtasks /Delete /TN "ApplyPilot Daily"`.

## 7. Manual review
`applypilot review --show URL` prints employer, position, location, salary, official posting URL, posting
check, fit score + explanation, eligibility evidence, readiness, outstanding screening questions and the
full resume/cover letter. Then:
- `--approve URL --note "..."` (re-checks readiness; required for any real apply),
- `--reject URL` / `--defer URL`,
- applied by hand: open the official URL, upload the files shown, then `--mark-submitted URL`
  (recorded as `applied`, `verification_confidence = human_reported`; blocks any automated/duplicate apply).
- Postings the checker could not confirm: open them yourself, then
  `applypilot verify --confirm-active URL` or `--mark-closed URL`.
- Rejected documents are under `tailored_resumes\rejected\` / `cover_letters\rejected\` with a report;
  judge rejections stay `needs_review` (`applypilot report validation`).

## 8. Supervised application (only after review)
Requirements: job `readiness = ready`, `review_status = approved` for real runs, Windows, Claude Code CLI,
and an attested isolated browser host (8a). On any other computer `apply` (dry or real) refuses to start.
```powershell
.\.venv\Scripts\applypilot.exe apply --dry-run --url <exact job URL> --workers 1   # watch it; stops at the first write
.\.venv\Scripts\applypilot.exe apply --url <exact job URL> --workers 1 --allow-real-submissions
# each state-changing request pauses; in a second terminal, after checking what is shown:
.\.venv\Scripts\applypilot.exe apply --approve <session dir> --nonce <nonce>
```
Outcomes: `applied` only with causal evidence (approved submit → released request → later confirmation);
anything uncertain → `needs_review` (`applypilot report uncertain`), never retried automatically.
Unsupported sites, CAPTCHAs, logins, account creation and sensitive questions stop the agent for you.

## 8a. Isolated browser host (required for any `apply`, including dry runs)
The Windows job object around Chrome stops programs Chrome itself starts, but the Windows shell can hand
an external link (`mailto:`, `ms-*:`, custom schemes) to another application through COM or to an app
that is already running -- outside Chrome's process tree. That cannot be prevented on a personal desktop
without changing machine-wide protocol associations, which ApplyPilot never does. Browser sessions
therefore run only on a dedicated machine:
1. A Windows VM (Hyper-V / VMware / VirtualBox) used only for ApplyPilot: no personal browser, mail or
   messaging client; default handlers left inert; no personal accounts, password managers or synced
   profiles; no shared clipboard, folders or drives with your desktop; outbound network limited to what
   applications need (snapshot before each batch).
2. Install ApplyPilot, Google Chrome, Node.js and Claude Code there; copy only the data the run needs
   (database, approved documents, profile) into a fresh `APPLYPILOT_DIR` on the VM.
3. In the VM: `$env:APPLYPILOT_ISOLATED_BROWSER_HOST = "1"` and
   `applypilot isolation --attest --statement "dedicated ApplyPilot VM, set up by <you> on <date>"`.
   Both are required; the record names the VM's computer name, so a copied data directory grants nothing.
4. Approvals are still given from a terminal (inside the VM) for every state-changing request.

## 9. Security limitations (read before applying)
- **Not certified submission-proof.** See `APPLY_AGENT_SECURITY_PLAN.md` § Audit response for residuals.
- Supervise every apply session; never leave one unattended.
- The verification-code helper is off unless `APPLYPILOT_ENABLE_VERIFY_HELPER=1` (single supervised worker).
- Trusted host only: a modified venv, Chrome binary or a local process on the CDP port defeats the guard.
- `applypilot dashboard` opens your **default** browser; use it only if that is acceptable to you.

## 10. Recovery
- Crash / power loss during `daily`: the OS releases the lock; the next run logs the recovery, marks the
  interrupted run, and continues; every stage is idempotent and resumable.
- Documents: a job is ready only while its text and PDF have the hashes recorded at validation and
  were built from the current job/resume/profile/evidence; any change (or a missing PDF) blocks it and
  withdraws an earlier human approval. `applypilot run pdf` re-renders missing PDFs.

- Crash during `apply`: stale `in_progress` claims become `needs_review` on the next `apply` start
  (never retried). Check the site/your mail, then `apply --mark-applied URL` or `--mark-failed URL`.
- Stuck lock with a live process: wait, or stop that process; never delete the lock while it runs.

## 11. Rollback
1. Stop the scheduled task. 2. Restore `applypilot.backup.db` over `applypilot.db` (with nothing running).
3. `git checkout <previous commit>` and reinstall (`pip install -e .`). Extra columns from newer versions
   are harmless to older code (it ignores them).

## 12. Troubleshooting
| Symptom | Check |
|---|---|
| `daily` exit 4 | LM Studio running? model `qwen/qwen3-14b` loaded? `applypilot doctor` |
| Everything `blocked` | `applypilot report blocked` — usually posting not verified, documents stale, or no evidence-grounded resume |
| Many `uncertain` postings | script-rendered career sites: confirm manually (`verify --confirm-active`) |
| Resumes rejected | `report validation`: an unsupported claim means the model invented it — add true facts to `candidate_evidence.yaml`, never the invention |
| `apply` refuses to start | it prints why (not ready / not approved / CLI flags / jail / guard self-test) |
| Discovery source failing | `report failures` (per-source errors; other sources still run) |
