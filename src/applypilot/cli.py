"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
    approve: str | None = typer.Option(None, "--approve", help="Approve one pending submission: the session dir printed by the policy hook."),
    nonce: str | None = typer.Option(None, "--nonce", help="Nonce of the pending submission (with --approve)."),
    allow_real_submissions: bool = typer.Option(
        False, "--allow-real-submissions",
        help="Required for any run without --dry-run. Every submit still needs a per-application human approval."),
) -> None:
    """Launch auto-apply to submit job applications."""
    if approve:
        # Human-only path: grants exactly one pending, unexpired request (matched by nonce).
        # The grant is single-use and bound to this job, session and exact action.
        from pathlib import Path

        from applypilot.apply import approval
        from applypilot.apply.policy import approve_submission
        req = approval.pending_request(Path(approve), nonce or "")
        if req:
            action = req.get("action") or {}
            console.print(f"[bold]Approving {req.get('kind')} action[/bold] for job {req.get('job_url')}")
            for key in ("method", "url", "resource_type", "body_len", "tool_action"):
                if key in action:
                    console.print(f"  {key}: {str(action[key])[:200]}")
        if not nonce or not req or not approve_submission(approve, nonce):
            console.print("[red]No matching pending (unexpired, unused) submission request.[/red]")
            raise typer.Exit(code=1)
        console.print("[green]Approved for this one request only.[/green]")
        return

    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt, BASE_CDP_PORT
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print("[yellow]For inspection only. Do not run the agent by hand: only the launcher "
                      "applies the tool restrictions and policy hooks.[/yellow]")
        return

    if not dry_run and not allow_real_submissions:
        console.print("[red]Real submissions are disabled.[/red] Use --dry-run, or pass "
                      "--allow-real-submissions for a supervised run (each submit then waits for "
                      "`applypilot apply --approve <session> --nonce <id>`).")
        raise typer.Exit(code=1)

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))
    summary.add_row("Dry runs (not submitted)", str(stats["dry_run"]))
    review_style = "[bold red]" if stats["needs_review"] else ""
    summary.add_row("Needs human review", f"{review_style}{stats['needs_review']}")

    console.print(summary)

    if stats.get("eligibility"):
        elig_table = Table(title="\nGeographic Eligibility", show_header=True, header_style="bold green")
        elig_table.add_column("Classification")
        elig_table.add_column("Count", justify="right")
        for name, count in stats["eligibility"]:
            elig_table.add_row(name, str(count))
        console.print(elig_table)
        console.print("[dim]Review undecided jobs with: applypilot eligibility[/dim]")

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command("eligibility")
def eligibility_cmd(
    status_filter: str | None = typer.Option(
        None, "--status", "-s",
        help="Show one classification (eligible_nc, eligible_us_remote, restricted_remote, unknown, "
             "ineligible, eligible_manual, ineligible_manual). Default: the review queue."),
    min_score: int = typer.Option(0, "--min-score", help="Only jobs with fit_score >= this."),
    limit: int = typer.Option(30, "--limit", "-l", help="Rows to show (0 = all)."),
    show_evidence: bool = typer.Option(False, "--evidence", "-e", help="Show supporting evidence."),
    reclassify: bool = typer.Option(False, "--reclassify",
                                    help="Re-run the classifier on all non-manual jobs."),
    approve: str | None = typer.Option(None, "--approve", help="Mark a job URL eligible (human decision)."),
    reject: str | None = typer.Option(None, "--reject", help="Mark a job URL ineligible (human decision)."),
    note: str = typer.Option("", "--note", help="Reason recorded with --approve/--reject."),
) -> None:
    """Review deterministic geographic eligibility decisions."""
    import json

    _bootstrap()
    from applypilot.database import get_connection
    from applypilot.eligibility import CLASSIFIER_VERSION, REVIEW_STATUSES, classify_jobs, set_manual_decision

    if approve or reject:
        url = approve or reject
        if set_manual_decision(url, approve=bool(approve), note=note):
            console.print(f"[green]Recorded manual {'approval' if approve else 'rejection'}:[/green] {url}")
        else:
            console.print(f"[red]No job with URL:[/red] {url}")
            raise typer.Exit(code=1)
        return

    counts = classify_jobs(force=reclassify)
    if counts:
        console.print(f"Classified {sum(counts.values())} job(s) with classifier v{CLASSIFIER_VERSION}: {counts}")

    conn = get_connection()
    statuses = (status_filter,) if status_filter else REVIEW_STATUSES
    placeholders = ",".join("?" * len(statuses))
    sql = (f"SELECT url, title, site, location, fit_score, eligibility_status, eligibility_reason, "
           f"eligibility_evidence FROM jobs WHERE eligibility_status IN ({placeholders}) "
           f"AND COALESCE(fit_score, 0) >= ? ORDER BY fit_score DESC NULLS LAST, url")
    params: list = [*statuses, min_score]
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()

    table = Table(title=f"Eligibility: {', '.join(statuses)}", show_header=True, header_style="bold cyan")
    for col, kw in (("Score", {"justify": "right"}), ("Title", {}), ("Employer", {}), ("Location", {}),
                    ("Class", {}), ("Reason", {})):
        table.add_column(col, **kw)
    for r in rows:
        table.add_row(str(r["fit_score"] if r["fit_score"] is not None else "-"), (r["title"] or "")[:40],
                      (r["site"] or "")[:18], (r["location"] or "-")[:28], r["eligibility_status"],
                      (r["eligibility_reason"] or "")[:70])
    console.print(table)
    if show_evidence:
        for r in rows:
            console.print(f"\n[bold]{(r['title'] or '')[:60]}[/bold]  {r['url']}")
            for ev in json.loads(r["eligibility_evidence"] or "[]"):
                console.print(f"  - {ev}")
    console.print("[dim]Approve: applypilot eligibility --approve URL --note '...'  |  "
                  "Reject: --reject URL[/dim]")


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, ENV_PATH, get_chrome_path,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os

    from applypilot.llm import describe_provider, list_local_models
    try:
        llm = describe_provider()
    except RuntimeError:
        results.append(("LLM provider", fail_mark,
                        "Set LLM_URL, GEMINI_API_KEY or OPENAI_API_KEY in .env (run 'applypilot init')"))
    else:
        if llm["provider"] == "Local":
            note = f"Local {llm['base_url']} model={llm['model']}"
            served = list_local_models(llm["base_url"])
            if served is None:
                results.append(("LLM provider", warn_mark, f"{note} (endpoint not reachable)"))
            elif llm["model"] not in served:
                results.append(("LLM provider", fail_mark,
                                f"{note} not served; available: {', '.join(served) or 'none'}"))
            else:
                results.append(("LLM provider", ok_mark, f"{note} (served)"))
        else:
            results.append(("LLM provider", ok_mark, f"{llm['provider']} model={llm['model']}"))
        if llm["ignored"]:
            results.append(("LLM provider", warn_mark,
                            f"Ignored (lower precedence): {', '.join(llm['ignored'])}"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CAPTCHA policy: detect and stop; solving services are never used.
    if os.environ.get("CAPSOLVER_API_KEY"):
        results.append(("CAPTCHA policy", warn_mark,
                        "CAPSOLVER_API_KEY is set but ignored: CAPTCHAs are detected and skipped"))
    else:
        results.append(("CAPTCHA policy", ok_mark, "Detect and skip (never solved or bypassed)"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
