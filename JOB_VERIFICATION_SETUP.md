# Job verification review queue

This first version is **human-reviewed verification tracking**, not an automatic
proof that a vacancy is open. A reachable URL, an Indeed page, and a listing
described as Remote are **not** sufficient evidence of an open, NC-eligible job.

The feature stores review notes and evidence in a separate SQLite table in
the currently selected `APPLYPILOT_DIR` database. It doesn't change scores,
application statuses, or existing discovery records, and never submits applications.

## Windows development usage

```powershell
cd "$HOME\ApplyPilot-Dev"
$env:APPLYPILOT_DIR = "$HOME\.applypilot-dev"
.\.venv\Scripts\python.exe -c "from applypilot.config import DB_PATH; print(DB_PATH)"
.\.venv\Scripts\python.exe -m applypilot.verification --list --min-score 7 --limit 25
```

Open each promising link, locate its official employer posting, and confirm
location, eligibility, salary, required qualifications and open status.
Then mark it with the **exact stored listing URL**:

```powershell
.\.venv\Scripts\python.exe -m applypilot.verification --mark open --url "https://www.indeed.com/viewjob?jk=EXAMPLE" --official-url "https://employer.example/jobs/123" --employer "Employer Name" --eligibility eligible --notes "Reviewed employer posting; North Carolina eligible"
```

Other options: `--mark needs_review`, `--mark closed`, and
`--eligibility restricted` or `unverified`. A marked `open` status
with eligibility `unverified` is still **not** an approval to auto-apply.

This initial review queue is intentionally conservative; it does not
perform employer-site crawling, infer open status from HTTP 200, or
turn verified candidates into auto-application submissions.
