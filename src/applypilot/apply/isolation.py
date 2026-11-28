"""Where an ApplyPilot browser may run at all (fork; audit R6).

The Windows Job Object (``proc_jail``) contains Chrome's *descendants*, but an external
protocol (``mailto:``, ``ms-*:``, custom schemes) can be dispatched by the Windows shell
through COM / ``DelegateExecute`` or to an application that is already running -- outside the
job, possibly before any notification. That cannot be prevented on a personal desktop
without changing machine-wide protocol associations, which ApplyPilot never does.

So browser sessions run only on a host that has been set up and attested as an isolated
execution host: a dedicated Windows VM with no personal browser or mail client, no shared
protocol handlers, no personal accounts or files (see DEPLOYMENT.md, "Isolated browser host").

Attestation needs both:
* ``APPLYPILOT_ISOLATED_BROWSER_HOST=1`` in the environment, and
* ``APP_DIR/isolated-host.json`` naming this computer, written by
  ``applypilot isolation --attest`` on that host.

A data directory copied from the VM to another computer does not carry the permission (the
file names the computer), and the environment flag alone is not enough.
"""
from __future__ import annotations

import json
import os
import platform
import socket
from datetime import UTC, datetime
from pathlib import Path

from applypilot import config

ENV_FLAG = "APPLYPILOT_ISOLATED_BROWSER_HOST"
ATTEST_FILE = "isolated-host.json"
REQUIREMENTS = (
    "dedicated virtual machine (or separate physical machine) used only for ApplyPilot",
    "no personal browser, mail client or messaging app installed; default handlers are inert",
    "no personal accounts, password managers, synced profiles or personal files",
    "no shared clipboard, folders or drives with the personal desktop",
    "outbound network limited to what job applications need",
)


def _computer() -> str:
    return (os.environ.get("COMPUTERNAME") or platform.node() or socket.gethostname() or "").strip().lower()


def attestation_path() -> Path:
    return config.APP_DIR / ATTEST_FILE


def browser_host_problem() -> str | None:
    """Why an ApplyPilot browser must not start on this host, or None when it is attested."""
    if os.environ.get(ENV_FLAG) != "1":
        return (f"browser sessions are disabled on this host: it is not attested as an isolated browser host "
                f"({ENV_FLAG}=1 and `applypilot isolation --attest` on a dedicated VM; see DEPLOYMENT.md)")
    try:
        rec = json.loads(attestation_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "isolated-host attestation missing or unreadable (run `applypilot isolation --attest` on the VM)"
    if not isinstance(rec, dict) or str(rec.get("computer", "")).lower() != _computer() or not _computer():
        return "isolated-host attestation was made on a different computer"
    return None


def attest(statement: str) -> Path:
    """Record that this computer is a dedicated isolated browser host (run on that host only)."""
    if os.environ.get(ENV_FLAG) != "1":
        raise RuntimeError(f"set {ENV_FLAG}=1 on the isolated host before attesting")
    rec = {"computer": _computer(), "attested_at": datetime.now(UTC).isoformat(), "statement": statement[:500],
           "requirements": list(REQUIREMENTS)}
    path = attestation_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return path
