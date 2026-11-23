# Optional evidence-based scoring

This feature supplements the resume with additional **user-confirmed** facts.
It does not edit the resume, tailor a document, or modify the application data model.

1. Select the isolated development data directory in PowerShell:

   ```powershell
   cd "$HOME\ApplyPilot-Dev"
   $env:APPLYPILOT_DIR = "$HOME\.applypilot-dev"
   ```

2. Copy the template into the local user-data folder, **not into the Git repository**:

   ```powershell
   Copy-Item ".\src\applypilot\config\candidate_evidence.example.yaml" "$env:APPLYPILOT_DIR\candidate_evidence.yaml"
   notepad "$env:APPLYPILOT_DIR\candidate_evidence.yaml"
   ```

3. Replace all examples with accurate, verified facts, then set `enabled: true`.
   Keep paid professional experience, certifications and personal projects separate.
   Only claim a credential you actually earned. Do not claim years of professional
   experience from home projects. If a fact isn't supported, leave it out.

4. Preview the text the model will see:

   ```powershell
   .\.venv\Scripts\python.exe -c "from applypilot.scoring.candidate_evidence import load_candidate_evidence,format_candidate_evidence; print(format_candidate_evidence(load_candidate_evidence()))"
   ```

5. Run tests:

   ```powershell
   .\.venv\Scripts\python.exe -m unittest discover -s tests -v
   ```

6. Score one job only after confirming the isolated database path:

   ```powershell
   .\.venv\Scripts\python.exe -c "from applypilot.config import DB_PATH; print(DB_PATH)"
   .\.venv\Scripts\python.exe -c "from applypilot.config import load_env; load_env(); from applypilot.scoring.scorer import run_scoring; print(run_scoring(limit=1))"
   ```

Existing scores are **not** automatically recalculated when evidence changes.
Use explicit rescoring only after reviewing the new profile.

Personal evidence remains on your PC under APPLYPILOT_DIR; never commit it.
The model is instructed to distinguish paid experience from hobby projects.
Job descriptions may contain inaccurate requirements: verify mandatory criteria
and geographic eligibility against official listings before applying.
