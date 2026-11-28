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

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # access denied: exists
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    """Exclusive lock file. A lock whose owner process is gone is taken over (logged)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False
        self.took_over: dict | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    info = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    info = {}
                if _pid_alive(int(info.get("pid", 0) or 0)):
                    return False
                self.took_over = info
                self.path.unlink(missing_ok=True)  # stale: previous run crashed
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "started": datetime.now(UTC).isoformat()}, f)
            self.held = True
            return True
        return False

    def release(self) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False


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


def run_daily(*, stages: list[str] | None = None, plan_only: bool = False,
              stage_fns: dict[str, Callable[[], dict]] | None = None,
              check_model: Callable[[], tuple[bool, str]] = model_available) -> int:
    """Run the daily preparation pipeline. Returns the process exit code."""
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    try:
        config.load_env()
        config.ensure_dirs()
        from applypilot.database import init_db
        conn = init_db()
        cfg = opsconfig.load()
    except Exception as e:  # noqa: BLE001
        log.error("daily bootstrap failed: %s", e)
        return EXIT_CONFIG
    chosen = [s for s in (stages or cfg["daily"]["stages"]) if s in opsconfig.DAILY_STAGES]
    if not chosen:
        return EXIT_CONFIG
    if plan_only:
        print("Daily plan (nothing executed): " + " -> ".join(chosen))
        print(f"Limits: {json.dumps(cfg['daily'])}  min_score={cfg['min_score']}")
        return EXIT_OK

    lock = RunLock(config.APP_DIR / LOCK_NAME)
    if not lock.acquire():
        print("Another daily run is in progress; exiting.")
        return EXIT_LOCKED
    rl = RunLog(run_id)
    started = datetime.now(UTC).isoformat()
    conn.execute("INSERT INTO pipeline_runs (run_id, kind, started_at, status) VALUES (?, 'daily', ?, 'running')",
                 (run_id, started))
    conn.commit()
    if lock.took_over:
        rl.event("stale_lock_recovered", previous=lock.took_over)
    exit_code = EXIT_OK
    summary: dict = {}
    try:
        ok, model_msg = check_model() if any(s in LLM_STAGES for s in chosen) else (True, "not needed")
        rl.event("start", stages=chosen, model=model_msg, model_ok=ok, limits=cfg["daily"])
        fns = stage_fns or _stage_fns(cfg, int(cfg["min_score"]))
        deadline = time.monotonic() + float(cfg["daily"]["max_runtime_minutes"]) * 60
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
                if name == "discover" and any(str(v).startswith("error") for v in result.values()):
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
        conn.execute("UPDATE pipeline_runs SET finished_at = ?, status = ?, exit_code = ?, summary = ? WHERE run_id = ?",
                     (datetime.now(UTC).isoformat(), "ok" if exit_code == EXIT_OK else "attention", exit_code,
                      json.dumps(summary), run_id))
        conn.commit()
        rl.event("end", exit_code=exit_code, summary=summary)
        lock.release()
    return exit_code
