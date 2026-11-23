# NC IT prioritization (optional)

The new prioritization only changes which pending jobs are sent to the LLM first.
It does not delete jobs, decide geographic eligibility, or auto-apply.

In the editable Windows development environment:

```powershell
cd "$HOME\ApplyPilot-Dev"
$env:APPLYPILOT_DIR = "$HOME\.applypilot-dev"
Copy-Item ".\src\applypilot\config\priority.nc-it.example.yaml" "$env:APPLYPILOT_DIR\priority.yaml"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Confirm the development database before running any scoring:

```powershell
.\.venv\Scripts\python.exe -c "from applypilot.config import DB_PATH; print(DB_PATH)"
```

Review the first 20 prioritized jobs without calling LM Studio:

```powershell
.\.venv\Scripts\python.exe -c "from applypilot.database import get_jobs_by_stage; from applypilot.scoring.priority import load_priority_config, prioritize_jobs; j=get_jobs_by_stage(stage='pending_score',limit=0); j=prioritize_jobs(j,load_priority_config()); [print(x['title'],'|',x.get('location')) for x in j[:20]]"
```

A location saying Remote is NOT proof that the job accepts NC residents.
Review the original listing before applying. Missing locations remain visible.
