"""Daily preparation pipeline (fork): discover -> ... -> review queue. Never applies.

Designed for Windows Task Scheduler (see DEPLOYMENT.md): non-interactive, one instance
at a time, bounded work and runtime, structured JSON-lines log, meaningful exit codes.

Exit codes::

    0  every stage succeeded
    1  one or more stages failed (others still ran; see the log)
    2  bootstrap/configuration error (nothing ran)
    3  another daily run is in progress (nothing ran)
    4  the configured LLM is unavailable: LLM stages were skipped, the rest ran

No stage submits anything: ``apply`` is not a daily stage and cannot be configured as one.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from applypilot import config, opsconfig

log = logging.getLogger(__name__)

EXIT_OK, EXIT_STAGE_FAILED, EXIT_CONFIG, EXIT_LOCKED, EXIT_NO_MODEL = 0, 1, 2, 3, 4
LLM_STAGES = ("score", "tailor", "cover")
LOCK_NAME = "daily.lock"


# --- single instance ---------------------------------------------------------------------------------

_LOCK_OFFSET = 1 << 30  # byte locked in the lock file (beyond the owner record, so it stays readable)


class RunLock:
    """OS-held exclusive lock (audit R5).

    Ownership is an exclusive byte-range lock on ``path`` (``msvcrt.locking`` on Windows,
    ``fcntl.flock`` elsewhere) held for the lifetime of the open handle. The operating system
    releases it when the owner exits or crashes, so there is no stale-owner detection, no
    unlink and no takeover race: two processes can never both hold it, and a released or dead
    owner can never remove a newer owner's lock. The file itself is never deleted; it carries
    a diagnostic owner record (pid, token, started, released). ``took_over`` reports a previous
    owner that ended without releasing (crash / power loss).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False
        self.took_over: dict | None = None
        self.token = uuid.uuid4().hex
        self._fd: int | None = None

    def _os_lock(self, fd: int) -> bool:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            return True
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _os_unlock(self, fd: int) -> None:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)

    def _read_record(self, fd: int) -> dict:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 4096)
        try:
            return json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except ValueError:
            return {"unreadable": True}

    def _write_record(self, fd: int, record: dict) -> None:
        data = json.dumps(record).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, data)
        os.fsync(fd)

    def acquire(self) -> bool:
        if self.held:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        if not self._os_lock(fd):
            os.close(fd)
            return False
        previous = self._read_record(fd)
        if previous and not previous.get("released"):
            self.took_over = previous  # the previous owner ended without releasing (crash)
        self._write_record(fd, {"pid": os.getpid(), "token": self.token,
                                "started": datetime.now(UTC).isoformat()})
        self._fd = fd
        self.held = True
        return True

    def release(self) -> None:
        if not self.held or self._fd is None:
            return
        fd, self._fd, self.held = self._fd, None, False
        try:
            rec = self._read_record(fd)
            if rec.get("token") == self.token:
                rec["released"] = datetime.now(UTC).isoformat()
                self._write_record(fd, rec)
        finally:
            self._os_unlock(fd)
            os.close(fd)


# --- structured log ----------------------------------------------------------------------------------

class RunLog:
    def __init__(self, run_id: str) -> None:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = config.LOG_DIR / f"daily-{datetime.now(UTC):%Y%m%d}.jsonl"
        self.run_id = run_id

    def event(self, kind: str, **data) -> None:
        rec = {"t": datetime.now(UTC).isoformat(), "run": self.run_id, "event": kind, **data}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")


# --- model check -------------------------------------------------------------------------------------

def model_available() -> tuple[bool, str]:
    """True when the configured LLM answers. Local servers must list the configured model."""
    from applypilot import llm
    try:
        info = llm.describe_provider()
    except RuntimeError as e:
        return False, str(e)
    if info["provider"] != "Local":
        return True, f"{info['provider']} {info['model']} (remote; not probed)"
    models = llm.list_local_models(info["base_url"])
    if models is None:
        return False, f"local LLM server unreachable at {info['base_url']}"
    if info["model"] not in models and info["model"] != "local-model":
        return False, f"model {info['model']} is not loaded (served: {', '.join(models[:5]) or 'none'})"
    return True, f"{info['model']} at {info['base_url']}"


# --- stages ------------------------------------------------------------------------------------------

def _stage_fns(cfg: dict, min_score: int) -> dict[str, Callable[[], dict]]:
    d = cfg["daily"]

    def discover():
        from applypilot.pipeline import _run_discover
        return _run_discover()

    def enrich():
        from applypilot.enrichment.detail import run_enrichment
        run_enrichment()
        return {"status": "ok"}

    def verify():
        from applypilot.verify import run_verification
        return run_verification(limit=int(d["max_verify"]))

    def dedupe():
        from applypilot.dedupe import run_dedupe
        return run_dedupe()

    def classify():
        from applypilot.eligibility import classify_jobs
        return {"status": "ok", "classified": classify_jobs()}

    def score():
        from applypilot.scoring.scorer import run_scoring
        r = run_scoring(limit=int(d["max_score"]))
        return {"status": "ok", **{k: v for k, v in r.items() if k != "distribution"}}

    def tailor():
        from applypilot.scoring.tailor import run_tailoring
        return {"status": "ok", **run_tailoring(min_score=min_score, limit=int(d["max_tailor"]),
                                                validation_mode="strict")}

    def cover():
        from applypilot.scoring.cover_letter import run_cover_letters
        return {"status": "ok", **run_cover_letters(min_score=min_score, limit=int(d["max_cover"]),
                                                    validation_mode="strict")}

    def pdf():
        from applypilot.scoring.pdf import batch_convert
        batch_convert()
        return {"status": "ok"}

    def prepare():
        from applypilot.readiness import run_prepare
        return run_prepare()

    return {"discover": discover, "enrich": enrich, "verify": verify, "dedupe": dedupe, "classify": classify,
            "score": score, "tailor": tailor, "cover": cover, "pdf": pdf, "prepare": prepare}


def _partial(name: str, result: dict) -> bool:
    """A stage that reported per-item errors did not fully succeed (audit R5)."""
    if name == "discover" and any(str(v).startswith("error") for v in result.values()):
        return True
    try:
        return int(result.get("errors") or 0) > 0 or bool(result.get("stopped_early"))
    except (TypeError, ValueError):
        return True


def run_daily(*, stages: list[str] | None = None, plan_only: bool = False,
              stage_fns: dict[str, Callable[[], dict]] | None = None,
              check_model: Callable[[], tuple[bool, str]] = model_available) -> int:
    """Run the daily preparation pipeline. Returns the process exit code.

    The OS-held run lock is taken *before* the database is opened or migrated, so two
    invocations (scheduled, manual or ``applypilot run``) never bootstrap or work concurrently."""
    from applypilot import runctl
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    try:
        config.load_env()
        cfg = opsconfig.load()
    except Exception as e:  # noqa: BLE001
        log.error("daily configuration failed: %s", e)
        return EXIT_CONFIG
    chosen = [s for s in (stages or cfg["daily"]["stages"]) if s in opsconfig.DAILY_STAGES]
    if not chosen:
        return EXIT_CONFIG
    if plan_only:
        print("Daily plan (nothing executed): " + " -> ".join(chosen))
        print(f"Limits: {json.dumps(cfg['daily'])}  min_score={cfg['min_score']}")
        return EXIT_OK

    try:
        config.ensure_dirs()
    except OSError as e:
        log.error("daily bootstrap failed: %s", e)
        return EXIT_CONFIG
    lock = RunLock(config.APP_DIR / LOCK_NAME)
    if not lock.acquire():
        print("Another daily run is in progress; exiting.")
        return EXIT_LOCKED
    try:
        from applypilot.database import init_db
        conn = init_db()  # migrations run only while holding the lock
    except Exception as e:  # noqa: BLE001
        log.error("daily bootstrap failed: %s", e)
        lock.release()
        return EXIT_CONFIG
    rl = RunLog(run_id)
    started = datetime.now(UTC).isoformat()
    # We hold the exclusive lock, so any run still marked 'running' was interrupted.
    stale = conn.execute("UPDATE pipeline_runs SET status = 'interrupted', finished_at = COALESCE(finished_at, ?)"
                         " WHERE kind = 'daily' AND status = 'running'", (started,)).rowcount
    conn.execute("INSERT INTO pipeline_runs (run_id, kind, started_at, status) VALUES (?, 'daily', ?, 'running')",
                 (run_id, started))
    conn.commit()
    if lock.took_over or stale:
        rl.event("stale_lock_recovered", previous=lock.took_over, interrupted_runs=stale)
    exit_code = EXIT_OK
    summary: dict = {}
    try:
        ok, model_msg = check_model() if any(s in LLM_STAGES for s in chosen) else (True, "not needed")
        rl.event("start", stages=chosen, model=model_msg, model_ok=ok, limits=cfg["daily"])
        fns = stage_fns or _stage_fns(cfg, int(cfg["min_score"]))
        deadline = time.monotonic() + float(cfg["daily"]["max_runtime_minutes"]) * 60
        runctl.set_deadline(deadline)
        for name in chosen:
            if time.monotonic() > deadline:
                rl.event("stage_skipped", stage=name, reason="max_runtime_minutes reached")
                summary[name] = "skipped: runtime limit"
                exit_code = exit_code or EXIT_STAGE_FAILED
                continue
            if name in LLM_STAGES and not ok:
                rl.event("stage_skipped", stage=name, reason=model_msg)
                summary[name] = "skipped: model unavailable"
                exit_code = EXIT_NO_MODEL if exit_code in (EXIT_OK, EXIT_NO_MODEL) else exit_code
                continue
            t0 = time.monotonic()
            rl.event("stage_start", stage=name)
            try:
                result = fns[name]() or {}
                status = str(result.get("status", "ok"))
                if status == "ok" and _partial(name, result):
                    status = "partial"
            except Exception as e:
                log.exception("daily stage %s failed", name)
                result, status = {"error": f"{type(e).__name__}: {str(e)[:300]}"}, "error"
            dur = round(time.monotonic() - t0, 1)
            rl.event("stage_end", stage=name, status=status, seconds=dur, result=result)
            summary[name] = status
            if status not in ("ok",):
                exit_code = EXIT_STAGE_FAILED
    finally:
        runctl.clear()
        try:
            conn.execute("UPDATE pipeline_runs SET finished_at = ?, status = ?, exit_code = ?, summary = ?"
                         " WHERE run_id = ?", (datetime.now(UTC).isoformat(),
                                               "ok" if exit_code == EXIT_OK else "attention", exit_code,
                                               json.dumps(summary), run_id))
            conn.commit()
            rl.event("end", exit_code=exit_code, summary=summary)
        finally:
            lock.release()
    return exit_code

