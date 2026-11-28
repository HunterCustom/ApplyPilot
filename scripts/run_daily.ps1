<#
.SYNOPSIS
  Daily ApplyPilot preparation run for Windows Task Scheduler. Never submits applications.

.DESCRIPTION
  Sets APPLYPILOT_DIR explicitly (never relies on the default ~\.applypilot), runs
  `applypilot daily`, appends a one-line summary to <APPLYPILOT_DIR>\logs\scheduler.log and
  exits with applypilot's exit code (0 ok, 1 stage failure, 2 config, 3 already running,
  4 LLM unavailable). Registering the task is a manual step: see DEPLOYMENT.md.

.PARAMETER DataDir
  The ApplyPilot data directory to use (required; no default on purpose).

.PARAMETER RepoDir
  Repository root containing .venv (defaults to this script's parent folder).
#>
param(
    [Parameter(Mandatory = $true)][string]$DataDir,
    [string]$RepoDir = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $DataDir)) {
    Write-Error "Data directory not found: $DataDir"
    exit 2
}
$env:APPLYPILOT_DIR = (Resolve-Path -LiteralPath $DataDir).Path
$env:PYTHONIOENCODING = "utf-8"
$exe = Join-Path $RepoDir ".venv\Scripts\applypilot.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    Write-Error "applypilot not installed in $RepoDir\.venv"
    exit 2
}
$logDir = Join-Path $env:APPLYPILOT_DIR "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$started = Get-Date -Format "yyyy-MM-ddTHH:mm:ss"
& $exe daily *>> (Join-Path $logDir "daily-console.log")
$code = $LASTEXITCODE
Add-Content -LiteralPath (Join-Path $logDir "scheduler.log") -Encoding utf8 -Value "$started exit=$code"
exit $code
