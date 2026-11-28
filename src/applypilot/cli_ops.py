"""Operational CLI commands (fork): verify, dedupe, review, screening, report, daily.

Registered on the main Typer app by ``cli.py`` (kept separate to keep the upstream diff
small). Page-derived text is printed with Rich markup escaped.
"""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

console = Console()


def _bootstrap() -> None:
    from applypilot.cli import _bootstrap as boot
    boot()


def _e(v) -> str:
    return escape("" if v is None else str(v))


def register(app: typer.Typer) -> None:
    @app.command("verify")
    def verify_cmd(
        limit: int = typer.Option(50, "--limit", help="Maximum postings to check."),
        confirm_active: str | None = typer.Option(None, "--confirm-active",
                                                     help="Human check: mark this exact job URL's posting active."),
        mark_closed: str | None = typer.Option(None, "--mark-closed", help="Human check: mark the posting closed."),
    ) -> None:
        """Check that postings still exist and accept applications (robots.txt + per-host delay)."""
        _bootstrap()
        from datetime import UTC, datetime

        from applypilot.database import get_connection
        conn = get_connection()
        from applypilot.verify import preferred_url
        for url, status in ((confirm_active, "active"), (mark_closed, "closed")):
            if url:
                row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
                checked = preferred_url(dict(row)) if row else ""
                if status == "active" and row:
                    console.print(f"Confirming the posting at: {_e(checked)}")
                # The human confirmation is bound to the URL that was confirmed (audit R3).
                n = conn.execute("UPDATE jobs SET posting_status = ?, posting_reason = 'confirmed by a human',"
                                 " posting_checked_at = ?, posting_verified_url = ?, posting_final_url = NULL"
                                 " WHERE url = ?",
                                 (status, datetime.now(UTC).isoformat(), checked, url)).rowcount
                conn.commit()
                console.print(f"{'Recorded' if n else '[red]No job with that exact URL[/red]'}: {_e(url)} -> {status}")
                raise typer.Exit(code=0 if n else 1)
        from applypilot.verify import run_verification
        console.print(run_verification(limit=limit))

    @app.command("dedupe")
    def dedupe_cmd() -> None:
        """Detect duplicates across boards and employer sites (rows are never deleted)."""
        _bootstrap()
        from applypilot.dedupe import run_dedupe
        console.print(run_dedupe())

    @app.command("review")
    def review_cmd(
        status: str = typer.Option("ready", "--status", help="ready | approved | blocked | deferred | rejected"),
        show: str | None = typer.Option(None, "--show", help="Full review sheet for one exact job URL."),
        approve: str | None = typer.Option(None, "--approve", help="Approve this job URL for (supervised) applying."),
        reject: str | None = typer.Option(None, "--reject", help="Reject this job URL."),
        defer: str | None = typer.Option(None, "--defer", help="Defer this job URL."),
        submitted: str | None = typer.Option(None, "--mark-submitted",
                                                help="I applied to this job URL by hand: record it (no automation)."),
        reset: str | None = typer.Option(None, "--reset", help="Clear the review decision for this job URL."),
        note: str = typer.Option("", "--note", help="Reason recorded with the decision."),
        limit: int = typer.Option(30, "--limit"),
    ) -> None:
        """Human review queue: inspect prepared applications and approve / reject / defer them."""
        _bootstrap()
        from applypilot import readiness
        for action, url in (("approve", approve), ("reject", reject), ("defer", defer),
                            ("manual_submitted", submitted), ("reset", reset)):
            if url:
                ok, msg = readiness.review(url, action, note)
                console.print(("[green]" if ok else "[red]") + _e(msg) + ("[/green]" if ok else "[/red]"))
                raise typer.Exit(code=0 if ok else 1)
        if show:
            _review_sheet(show)
            return
        rows = readiness.queue(status=status, limit=limit)
        t = Table(title=f"Review queue: {status} ({len(rows)})", show_lines=False)
        for c in ("score", "employer", "position", "location", "salary", "screening", "url"):
            t.add_column(c, overflow="fold")
        for r in rows:
            t.add_row(_e(r.get("fit_score")), _e(r.get("company") or r.get("site")), _e(r.get("title")),
                      _e(r.get("location")), _e(r.get("salary") or "-"), _e(r.get("screening_status") or "-"),
                      _e(r.get("url")))
        console.print(t)
        console.print("[dim]Details: applypilot review --show URL | decide: --approve/--reject/--defer URL "
                      "--note '...' | applied by hand: --mark-submitted URL[/dim]")

    def _review_sheet(url: str) -> None:
        from applypilot import readiness, screening
        from applypilot.database import get_connection
        conn = get_connection()
        row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
        if not row:
            console.print("[red]No job with that exact URL.[/red]")
            raise typer.Exit(code=1)
        job = dict(row)
        ok, reasons, info = readiness.evaluate(job, conn)
        from applypilot import ranking
        _pts, pref_note = ranking.preference_points(job, ranking.load_preferences())
        console.rule(f"{_e(job.get('title'))} @ {_e(job.get('company') or job.get('site'))}")
        for k, v in (("Location", job.get("location")), ("Salary", job.get("salary") or "not stated"),
                     ("Preference", pref_note),
                     ("Final posting URL", job.get("posting_final_url") or "-"),
                     ("Resume hash", f"{(job.get('resume_sha') or '-')[:16]} / PDF {(job.get('resume_pdf_sha') or '-')[:16]}"),
                     ("Official posting", job.get("posting_verified_url") or job.get("application_url") or url),
                     ("Posting", (f"{job.get('posting_status')} - {job.get('posting_reason')} "
                                  f"(checked {job.get('posting_checked_at')})")),
                     ("Posted", job.get("date_posted") or "unknown (never guessed)"),
                     ("Fit score", f"{job.get('fit_score')} ({job.get('score_model') or 'model unrecorded'})"),
                     ("Fit explanation", job.get("score_reasoning")),
                     ("Eligibility", (f"{job.get('eligibility_status')} / {job.get('eligibility_tier')}: "
                                      f"{job.get('eligibility_reason')}")),
                     ("Eligibility evidence", job.get("eligibility_evidence")),
                     ("Resume", f"{job.get('tailored_resume_path')} [{job.get('resume_validation')}]"),
                     ("Cover letter", f"{job.get('cover_letter_path') or '-'} [{job.get('cover_validation') or '-'}]"),
                     ("Duplicate of", job.get("duplicate_of") or "-"),
                     ("Review", f"{job.get('review_status') or 'undecided'} {job.get('review_note') or ''}")):
            console.print(f"[bold]{k}:[/bold] {_e(v)}")
        console.print(f"[bold]Readiness:[/bold] {'[green]READY[/green]' if ok else '[red]BLOCKED[/red]'}")
        for r in reasons:
            console.print(f"  - {_e(r)}")
        missing = info.get("screening_missing") or []
        console.print("[bold]Outstanding screening questions:[/bold] " +
                      (_e(", ".join(missing)) if missing else "none for the standard set"))
        if missing:
            console.print(f"  [dim]Add verified answers in {_e(screening.answers_path())} (see `applypilot screening`).[/dim]")
        for label, p in (("RESUME", job.get("tailored_resume_path")), ("COVER LETTER", job.get("cover_letter_path"))):
            if p and Path(p).exists():
                console.rule(label)
                console.print(_e(Path(p).read_text(encoding="utf-8")[:6000]))
        console.print("[dim]Manual submission: open the official posting above, upload the files shown, then "
                      "`applypilot review --mark-submitted URL`.[/dim]")

    @app.command("screening")
    def screening_cmd(
        test: str | None = typer.Option(None, "--test", help="Show how a question text would be handled."),
    ) -> None:
        """Verified screening answers: coverage and how a question would be handled."""
        _bootstrap()
        from applypilot import screening
        try:
            bank = screening.load_bank()
        except ValueError as e:
            console.print(f"[red]{_e(e)}[/red]")
            raise typer.Exit(code=1) from e
        if test:
            d = screening.decide(test, bank)
            console.print(f"{d.status}: {_e(d.reason)}" + (f" -> {_e(d.answer)}" if d.answer and
                                                           d.status == "answered" else ""))
            return
        cov = screening.coverage(bank)
        console.print(f"File: {_e(screening.answers_path())} ({len(bank)} answers, "
                      f"{sum(e.reusable for e in bank)} reusable)")
        console.print(f"Standard questions covered: {', '.join(cov['covered']) or 'none'}")
        console.print(f"Missing (agent will stop for you): {', '.join(cov['missing']) or 'none'}")

    @app.command("report")
    def report_cmd(
        name: str = typer.Argument("top", help="new | eligible | top | duplicates | expired | prepared | validation |"
                                               " review | applied | uncertain | blocked | inference | failures"),
        since: int = typer.Option(7, "--since", help="Days back (for 'new')."),
        min_score: int = typer.Option(7, "--min-score"),
        sort: str | None = typer.Option(None, "--sort", help="Column to sort by."),
        asc: bool = typer.Option(False, "--asc", help="Ascending order."),
        filter_text: str | None = typer.Option(None, "--filter", help="Substring of title/company/site/location."),
        limit: int = typer.Option(100, "--limit"),
        fmt: str = typer.Option("table", "--format", help="table | csv | json"),
        output: Path | None = typer.Option(None, "--output", "-o", help="Write to this file instead of stdout."),  # noqa: B008
    ) -> None:
        """Operational reports with filtering, sorting and CSV/JSON export (no profile data)."""
        _bootstrap()
        from applypilot import reports
        if name in ("inference", "failures"):
            data = reports.inference_report() if name == "inference" else reports.failures_report(limit=limit)
            text = reports.to_json(data)
            if output:
                output.write_text(text, encoding="utf-8")
                console.print(f"Wrote {output}")
            else:
                console.print_json(text)
            return
        if name not in reports.REPORTS:
            console.print(f"[red]Unknown report {name!r}[/red]")
            raise typer.Exit(code=1)
        rows = reports.run_report(name, since_days=since, min_score=min_score, sort=sort, desc=not asc,
                                  limit=limit, filter_text=filter_text)
        if fmt in ("csv", "json"):
            text = reports.to_csv(rows) if fmt == "csv" else reports.to_json(rows)
            if output:
                output.write_text(text, encoding="utf-8")
                console.print(f"Wrote {len(rows)} rows to {output}")
            else:
                print(text)
            return
        t = Table(title=f"{reports.REPORTS[name]['desc']} ({len(rows)})")
        cols = list(rows[0].keys()) if rows else reports.REPORTS[name]["cols"]
        for c in cols:
            t.add_column(c, overflow="fold", max_width=48)
        for r in rows:
            t.add_row(*[_e(r.get(c))[:300] for c in cols])
        console.print(t)

    @app.command("employers")
    def employers_cmd(
        verify: bool = typer.Option(False, "--verify", help="Probe each APP_DIR employers.yaml entry with one "
                                                            "read-only Workday job search and record the result."),
    ) -> None:
        """Your Workday employer watchlist (APP_DIR/employers.yaml): entries and verification status."""
        from applypilot.discovery import workday
        user, use_packaged = workday.load_user_employers()
        if not user:
            console.print("No entries in APP_DIR/employers.yaml (see DEPLOYMENT.md, 'Employer watchlist').")
            return
        results = workday.verify_user_employers() if verify else workday.verified_identities()
        for key, e in user.items():
            rec = results.get(key) or {}
            current = rec.get("identity") == workday._employer_identity(e)
            state = ("[green]verified[/green]" if rec.get("ok") and current else
                     "[yellow]not verified[/yellow]" if not rec else
                     f"[red]failed[/red] ({_e(rec.get('reason'))})" if current else "[yellow]changed since check[/yellow]")
            console.print(f"{_e(key)}: {_e(e.get('name') or key)} -- {_e(e['base_url'])} -- {state}")
        console.print(f"Packaged registry also crawled: {'yes' if use_packaged else 'no'}")

    @app.command("rescore")
    def rescore_cmd(
        limit: int = typer.Option(100, "--limit", help="Maximum jobs to rescore in this run."),
    ) -> None:
        """Rescore jobs whose score came from another model/prompt/evidence/resume (old scores kept)."""
        from applypilot.config import APP_DIR
        from applypilot.daily import EXIT_LOCKED, LOCK_NAME, RunLock
        APP_DIR.mkdir(parents=True, exist_ok=True)
        lock = RunLock(APP_DIR / LOCK_NAME)
        if not lock.acquire():
            console.print("[red]Another ApplyPilot run is in progress; exiting.[/red]")
            raise typer.Exit(code=EXIT_LOCKED)
        try:
            _bootstrap()
            from applypilot.scoring.scorer import run_scoring
            out = run_scoring(limit=limit, rescore=True)
            console.print({k: v for k, v in out.items() if k != "distribution"})
        finally:
            lock.release()

    @app.command("isolation")
    def isolation_cmd(
        attest: bool = typer.Option(False, "--attest", help="Record THIS computer as a dedicated isolated browser host."),
        statement: str = typer.Option("", "--statement", help="Who set the host up, and how (stored with the record)."),
    ) -> None:
        """Isolated browser host status (browser sessions run only on an attested dedicated VM)."""
        from applypilot.apply import isolation
        if attest:
            if not statement.strip():
                console.print("[red]--statement is required (describe the dedicated VM and who set it up).[/red]")
                raise typer.Exit(code=2)
            try:
                path = isolation.attest(statement)
            except RuntimeError as e:
                console.print(f"[red]{_e(e)}[/red]")
                raise typer.Exit(code=2) from e
            console.print(f"Attested: {_e(path)}")
        problem = isolation.browser_host_problem()
        console.print("Browser sessions: " + ("[green]allowed on this host[/green]" if problem is None
                                              else f"[yellow]disabled[/yellow] -- {_e(problem)}"))
        console.print("Requirements for an isolated browser host:")
        for r in isolation.REQUIREMENTS:
            console.print(f"  - {_e(r)}")

    @app.command("daily")
    def daily_cmd(
        plan: bool = typer.Option(False, "--plan", help="Show what would run; execute nothing."),
        stages: list[str] | None = typer.Argument(None, help="Subset of daily stages (default: pipeline.yaml)."),  # noqa: B008
    ) -> None:
        """Daily preparation run (Task Scheduler friendly). Never submits applications."""
        from applypilot.daily import run_daily
        code = run_daily(stages=stages, plan_only=plan)
        raise typer.Exit(code=code)

