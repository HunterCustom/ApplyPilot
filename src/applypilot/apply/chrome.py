"""Chrome lifecycle management for apply workers.

Handles launching an isolated Chrome instance with remote debugging,
isolated (empty) worker profiles, per-worker slot locks, and
cross-platform process cleanup.
"""

import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
import typing
from pathlib import Path

from applypilot import config

logger = logging.getLogger(__name__)

# CDP port base — each worker uses BASE_CDP_PORT + worker_id
BASE_CDP_PORT = 9222

# Track Chrome processes per worker for cleanup
_chrome_procs: dict[int, subprocess.Popen] = {}
_jails: dict[int, typing.Any] = {}  # worker_id -> proc_jail.ProcessJail


_chrome_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cross-platform process helpers
# ---------------------------------------------------------------------------

def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children.

    On Windows, Chrome spawns 10+ child processes (GPU, renderer, etc.),
    so taskkill /T is needed to kill the entire tree. On Unix, os.killpg
    handles the process group.
    """
    import signal as _signal

    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            # Unix: kill entire process group
            import os
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Process already gone or owned by another user
                try:
                    os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        logger.debug("Failed to kill process tree for PID %d", pid, exc_info=True)


def parse_netstat_listeners(netstat_output: str, port: int) -> list[int]:
    """PIDs LISTENING on exactly ``port`` (local address column) in `netstat -ano` output."""
    pids = []
    for line in netstat_output.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3] == "LISTENING"
                and parts[1].rsplit(":", 1)[-1] == str(port) and parts[4].isdigit()):
            pids.append(int(parts[4]))
    return sorted(set(pids))


def _listening_pids(port: int) -> list[int]:
    if platform.system() == "Windows":
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=10, check=False).stdout
        return parse_netstat_listeners(out, port)
    out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                         capture_output=True, text=True, timeout=10, check=False).stdout
    return sorted({int(p) for p in out.split() if p.strip().isdigit()})


def _process_command_line(pid: int) -> str:
    try:
        if platform.system() == "Windows":
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                   f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"]
        else:
            cmd = ["ps", "-o", "args=", "-p", str(int(pid))]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False).stdout.strip()
    except Exception:
        logger.debug("Could not read command line for PID %d", pid, exc_info=True)
        return ""


def is_worker_chrome(command_line: str) -> bool:
    """True only for a Chrome started by ApplyPilot (its isolated worker profile dir)."""
    norm = command_line.replace("\\", "/").lower()
    worker_dir = str(config.CHROME_WORKER_DIR).replace("\\", "/").lower()
    return bool(worker_dir) and worker_dir in norm and "--remote-debugging-port" in norm


def _kill_on_port(port: int) -> bool:
    """Kill leftover ApplyPilot worker Chrome processes listening on ``port``.

    Processes that are not ApplyPilot worker Chrome (the user's own browser,
    another tool using the port) are never touched. Returns True if the port
    is free afterwards as far as we know, False if a foreign process holds it.
    """
    try:
        pids = _listening_pids(port)
    except FileNotFoundError:
        logger.debug("Port tool not found (netstat/lsof) for port %d", port)
        return True
    except Exception:
        logger.debug("Failed to list listeners on port %d", port, exc_info=True)
        return True
    foreign = False
    for pid in pids:
        if is_worker_chrome(_process_command_line(pid)):
            _kill_process_tree(pid)
        else:
            foreign = True
            logger.warning("Port %d is held by PID %d, which is not ApplyPilot Chrome; leaving it alone",
                           port, pid)
    return not foreign


# ---------------------------------------------------------------------------
# Worker profile management
# ---------------------------------------------------------------------------

PROFILE_MARKER = ".applypilot-isolated"

# Per-worker slot locks held by this process (OS file locks, released on process exit).
_slot_handles: dict[int, typing.BinaryIO] = {}


def acquire_worker_slot(worker_id: int) -> None:
    """Take an exclusive OS lock on worker slot ``worker_id`` for this process.

    Two launcher processes using the same worker id would share a CDP port and
    a Chrome profile, and one would kill the other's Chrome mid-application.
    The lock makes the second launcher refuse instead. It is released
    automatically when the process exits, so it can never go stale.
    """
    if worker_id in _slot_handles:
        return
    path = config.CHROME_WORKER_DIR / f"worker-{worker_id}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")  # noqa: SIM115 -- the handle must stay open to hold the lock
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise RuntimeError(
            f"worker slot {worker_id} is in use by another ApplyPilot launcher; "
            "use a different worker id or wait for it to finish"
        ) from None
    _slot_handles[worker_id] = fh


def release_worker_slots() -> None:
    for wid, fh in list(_slot_handles.items()):
        try:
            fh.close()  # closing the handle releases the OS lock
        finally:
            _slot_handles.pop(wid, None)


def setup_worker_profile(worker_id: int) -> Path:
    """Create (or reuse) an isolated, initially EMPTY Chrome profile for a worker.

    The user's personal Chrome profile is never copied: it holds sessions for
    email, banking and every other site. Profiles are marked on creation; a
    profile without the marker (made by an older version that cloned the
    personal profile) is refused until the user deletes it.
    """
    profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}"
    marker = profile_dir / PROFILE_MARKER
    if profile_dir.exists() and any(profile_dir.iterdir()) and not marker.exists():
        raise RuntimeError(
            f"Chrome worker profile {profile_dir} was not created as an isolated profile "
            "(older versions cloned your personal Chrome profile). Delete that folder and retry."
        )
    profile_dir.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text("created empty by ApplyPilot; never cloned\n", encoding="utf-8")
        logger.info("[worker-%d] Created empty isolated Chrome profile", worker_id)
    return profile_dir


def _suppress_restore_nag(profile_dir: Path) -> None:
    """Clear Chrome's 'restore pages' nag by fixing Preferences.

    Chrome writes exit_type=Crashed when killed, which triggers a
    'Restore pages?' prompt on next launch. This patches it out.
    """
    prefs_file = profile_dir / "Default" / "Preferences"
    if not prefs_file.exists():
        return

    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs.setdefault("session", {})["restore_on_startup"] = 4  # 4 = open blank
        prefs.setdefault("session", {}).pop("startup_urls", None)
        prefs["credentials_enable_service"] = False
        prefs.setdefault("password_manager", {})["saving_enabled"] = False
        prefs.setdefault("autofill", {})["profile_enabled"] = False
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception:
        logger.debug("Could not patch Chrome preferences", exc_info=True)


# ---------------------------------------------------------------------------
# Chrome launch / kill
# ---------------------------------------------------------------------------

def chrome_command(chrome_exe: str, port: int, profile_dir: Path, headless: bool,
                   proxy_port: int | None) -> list[str]:
    """Worker Chrome command line. ``proxy_port`` (the launcher's egress proxy) is
    mandatory for apply sessions; ``None`` is only for callers that never browse."""
    cmd = [
        chrome_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1024,768",
        "--disable-session-crashed-bubble",
        "--disable-features=InfiniteSessionRestore,PasswordManagerOnboarding,AutofillServerCommunication",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
        "--password-store=basic",
        "--disable-save-password-bubble",
        "--disable-popup-blocking",
        # Isolated application browser: no sign-in/sync, no extensions, no autofill of other data
        "--disable-sync",
        "--disable-extensions",
        # Block dangerous permissions at browser level
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--deny-permission-prompts",
        "--disable-notifications",
    ]
    if proxy_port is not None:
        from applypilot.apply.network_guard import chrome_network_args
        cmd += chrome_network_args(proxy_port)
    if headless:
        cmd.append("--headless=new")
    # Start on about:blank, never the New Tab Page: its browser-owned chrome-untrusted:// frames
    # come and go during start-up and only add targets the guard must race to intercept.
    cmd.append("about:blank")
    return cmd


# Apply sessions only ever run Google Chrome / Chromium. A CHROME_PATH pointing at another
# browser (a personal browser such as Vivaldi, Edge, Brave ...) is refused, never launched.
CHROME_BINARY_NAMES = frozenset({"chrome.exe", "chromium.exe", "chrome", "chromium", "chromium-browser",
                                 "google-chrome", "google-chrome-stable", "google chrome"})


def require_chrome_binary(path: str, verify_signature: bool = True) -> str:
    """The resolved path if it is a Chrome/Chromium executable, else RuntimeError.

    On Windows the file must also carry a valid Authenticode signature from Google LLC (audit R6):
    a renamed or replaced ``chrome.exe`` is refused, not just a wrongly named binary."""
    import os
    real = os.path.realpath(path)
    if os.path.basename(real).lower() not in CHROME_BINARY_NAMES or not os.path.isfile(real):
        raise RuntimeError(f"refusing to launch {os.path.basename(real)!r}: ApplyPilot runs only Google Chrome or "
                           "Chromium (set CHROME_PATH to chrome.exe)")
    if verify_signature and platform.system() == "Windows":
        problem = chrome_signature_problem(real)
        if problem:
            raise RuntimeError(f"refusing to launch {real}: {problem}")
    return real


_SIGNATURE_CACHE: dict[tuple[str, int, int], str | None] = {}
GOOGLE_SIGNER = "O=Google LLC"


def chrome_signature_problem(path: str) -> str | None:
    """None if ``path`` has a valid Authenticode signature by Google LLC (Windows), else why not."""
    import os
    try:
        st = os.stat(path)
    except OSError as e:
        return f"cannot read the browser binary ({type(e).__name__})"
    key = (os.path.normcase(path), st.st_size, int(st.st_mtime))
    if key in _SIGNATURE_CACHE:
        return _SIGNATURE_CACHE[key]
    quoted = path.replace("'", "''")
    script = (f"$s = Get-AuthenticodeSignature -LiteralPath '{quoted}'; "
              "[pscustomobject]@{status=[string]$s.Status; subject=[string]$s.SignerCertificate.Subject} "
              "| ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=60, check=False).stdout
        info = json.loads(out.strip() or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return f"could not verify the browser's signature ({type(e).__name__})"
    status, subject = str(info.get("status") or ""), str(info.get("subject") or "")
    if status != "Valid":
        problem = f"browser binary signature is {status or 'missing'} (expected a valid Google LLC signature)"
    elif GOOGLE_SIGNER not in subject:
        problem = "browser binary is not signed by Google LLC"
    else:
        problem = None
    _SIGNATURE_CACHE[key] = problem
    return problem


def launch_chrome(worker_id: int, port: int | None = None,

                  headless: bool = False, proxy_port: int | None = None,
                  on_escape=None) -> subprocess.Popen:
    """Launch a Chrome instance with remote debugging for a worker.

    Args:
        worker_id: Numeric worker identifier.
        port: CDP port. Defaults to BASE_CDP_PORT + worker_id.
        headless: Run Chrome in headless mode (no visible window).
        proxy_port: The launcher's egress proxy (``NetworkGuard.proxy_port``); all
            browser traffic is routed through it.
        on_escape: Called (with a reason) when the browser starts any program that is
            not Chrome; that program is terminated by the process jail (proc_jail).
            Apply sessions must pass it; on Windows the jail is then mandatory.

    Returns:
        subprocess.Popen handle for the Chrome process.
    """
    if port is None:
        port = BASE_CDP_PORT + worker_id

    # Audit R6: no ApplyPilot browser on a host that is not an attested isolated browser host.
    from applypilot.apply.isolation import browser_host_problem
    problem = browser_host_problem()
    if problem:
        raise RuntimeError(problem)

    # Exclusive slot first: only then is a Chrome on this port/profile provably a leftover of ours.
    acquire_worker_slot(worker_id)
    profile_dir = setup_worker_profile(worker_id)

    # Kill a leftover ApplyPilot Chrome from a previous run on this port (never anything else)
    if not _kill_on_port(port):
        raise RuntimeError(
            f"CDP port {port} is in use by a process that is not ApplyPilot Chrome; "
            "refusing to terminate it. Free the port and retry."
        )

    # Patch preferences to suppress restore nag
    _suppress_restore_nag(profile_dir)

    chrome_exe = require_chrome_binary(config.get_chrome_path())

    from applypilot.apply.network_guard import disable_external_protocols
    disable_external_protocols(profile_dir)
    cmd = chrome_command(chrome_exe, port, profile_dir, headless, proxy_port)

    # On Unix, start in a new process group so we can kill the whole tree
    kwargs: dict = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if platform.system() != "Windows":
        import os
        kwargs["preexec_fn"] = os.setsid

    from applypilot.apply import proc_jail
    if on_escape is not None and not proc_jail.jail_supported():
        raise RuntimeError("the browser process jail is only available on Windows; "
                           "refusing to start an apply browser without it")
    if proc_jail.jail_supported():
        # Every ApplyPilot Chrome is jailed on Windows (audit H4), with or without a caller callback:
        # an external program it starts is terminated before it runs, and the browser is stopped.
        def _default_escape(why: str, wid: int = worker_id) -> None:
            logger.error("[worker-%d] %s; stopping the browser", wid, why)
            p = _chrome_procs.get(wid)
            if p is not None:
                _kill_process_tree(p.pid)
        jail = proc_jail.ProcessJail([chrome_exe], on_escape or _default_escape)
        try:
            proc = jail.spawn(cmd, **kwargs)
        except Exception:
            jail.close()
            raise
        with _chrome_lock:
            _jails[worker_id] = jail
    else:
        proc = subprocess.Popen(cmd, **kwargs)
    with _chrome_lock:
        _chrome_procs[worker_id] = proc

    # Give Chrome time to start and open the debug port
    time.sleep(3)
    if proc.poll() is not None:
        # rc 21: another Chrome already owns this profile and took over the launch (process
        # singleton), so this one never opens its debugger -- one cause of "debugger unreachable".
        rc = proc.returncode
        cleanup_worker(worker_id, proc)  # audit R12: close the jail and forget the handles now
        raise RuntimeError(f"Chrome exited during start-up (rc={rc}); "
                           + ("its profile directory is in use by another Chrome" if rc == 21
                              else "the debugger never opened (profile/policy problem; see the Chrome log)"))

    logger.info("[worker-%d] Chrome started on port %d (pid %d)",
                worker_id, port, proc.pid)
    return proc


def cleanup_worker(worker_id: int, process: subprocess.Popen | None) -> None:
    """Kill a worker's Chrome instance and remove it from tracking.

    Args:
        worker_id: Numeric worker identifier.
        process: The Popen handle returned by launch_chrome.
    """
    if process and process.poll() is None:
        _kill_process_tree(process.pid)
    with _chrome_lock:
        jail = _jails.pop(worker_id, None)
    if jail is not None:
        jail.close()  # terminates anything left in the browser's job
    with _chrome_lock:
        _chrome_procs.pop(worker_id, None)
    logger.info("[worker-%d] Chrome cleaned up", worker_id)


def kill_all_chrome() -> None:
    """Kill all Chrome instances and any port zombies.

    Called during graceful shutdown to ensure no orphan Chrome processes.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)  # ownership-checked: only ApplyPilot Chrome


def reset_worker_dir(worker_id: int) -> Path:
    """Wipe and recreate a worker's isolated working directory.

    Each job gets a fresh working directory so that file conflicts
    (resume PDFs, MCP configs) don't bleed between jobs.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the clean worker directory.
    """
    worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    if worker_dir.exists():
        shutil.rmtree(str(worker_dir), ignore_errors=True)
    worker_dir.mkdir(parents=True, exist_ok=True)
    return worker_dir


def cleanup_on_exit() -> None:
    """Atexit handler: kill all Chrome processes and sweep CDP ports.

    Register this with atexit.register() at application startup.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)  # ownership-checked: only ApplyPilot Chrome
    release_worker_slots()
