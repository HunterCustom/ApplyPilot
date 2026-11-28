"""Single-use, expiring, action-bound human approvals for real-run submissions.

Flow (all files inside the session directory, which the agent cannot reach):

1. A trusted enforcement point (policy hook for submit-like clicks, network guard
   for state-changing requests) calls :func:`create_request` with the exact
   action it is holding. The request records the job URL, the session id, a
   digest of the action and an expiry.
2. The human runs ``applypilot apply --approve <session> --nonce <nonce>``
   (:func:`grant`), which only succeeds for a pending, unexpired, unused request.
3. The enforcement point calls :func:`consume`, which succeeds exactly once and
   only if the grant matches the same nonce, session, job and action digest and
   was made before the request expired. The nonce is then recorded as used and
   both files are deleted, so a grant can never be replayed.

Every state transition is an atomic exclusive file creation (``O_EXCL`` marker or a
hard link, which never replaces an existing file on Windows or POSIX), so two concurrent
consumers or granters cannot both succeed, and withdrawal (expiry / cancellation) is
irreversible: a withdrawn nonce can never be granted or consumed afterwards.

Audit R7: the exclusive ``used`` marker is the single commit point of a consumption. Deleting
request/grant files and appending to the ledger happen afterwards and are best-effort: a
Windows sharing violation (another process still reading a file) can neither raise through
the winner nor turn a committed consumption into a lost authorization. Leftover files are
inert because every reader checks the markers first.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

APPROVAL_DIR = "approvals"
USED_FILE = "approvals-used.jsonl"
_STATE_DIR = "state"  # approvals/state/<nonce>.used | .cancelled -- exclusive markers
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


def action_digest(action: dict) -> str:
    return hashlib.sha256(json.dumps(action, sort_keys=True, default=str).encode()).hexdigest()


def _job_key(job_url: str) -> str:
    return hashlib.sha256((job_url or "").encode()).hexdigest()


def _dir(session_dir: Path) -> Path:
    return Path(session_dir) / APPROVAL_DIR


def _req_path(session_dir: Path, nonce: str) -> Path:
    return _dir(session_dir) / f"req-{nonce}.json"


def _grant_path(session_dir: Path, nonce: str) -> Path:
    return _dir(session_dir) / f"grant-{nonce}.json"


def _marker(session_dir: Path, nonce: str, what: str) -> Path:
    return _dir(session_dir) / _STATE_DIR / f"{nonce}.{what}"


def _claim(path: Path) -> bool:
    """Atomically create ``path``; False if it already exists (someone else won)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def _unlink_quietly(path: Path) -> None:
    """Best-effort cleanup: never raises (WinError 32 while another process reads the file)."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _closed(session_dir: Path, nonce: str) -> bool:
    return (_marker(session_dir, nonce, "used").exists() or _marker(session_dir, nonce, "cancelled").exists()
            or nonce in _used(session_dir))


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _used(session_dir: Path) -> set[str]:
    path = Path(session_dir) / USED_FILE
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.add(json.loads(line)["nonce"])
        except (ValueError, KeyError, TypeError):
            continue
    return out


def valid_nonce(nonce: str) -> bool:
    return isinstance(nonce, str) and bool(_NONCE_RE.match(nonce))


def display_action(action: dict) -> dict:
    """What a human reviewer sees (and what is written to disk): no query strings, credentials,
    typed values or body bytes -- only the method, a sanitized URL, sizes and labels (audit M4)."""
    from applypilot.apply.redact import safe_url
    out: dict = {}
    for k in ("method", "resource_type", "body_len", "body_complete", "tool_action"):
        if k in action:
            out[k] = action[k]
    if action.get("url"):
        out["url"] = safe_url(str(action["url"]))
    inp = action.get("input")
    if isinstance(inp, dict) and inp.get("element"):
        out["element"] = _safe_label(str(inp["element"]))
    return out


_LABEL_PII_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|\+?\d[\d\s().-]{6,}\d|\b(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{6,}\b")


def _safe_label(label: str) -> str:
    """Page-derived element label for display: emails, phone-like digit runs and token-like words
    are masked (audit R10); the human still sees the control's wording."""
    return _LABEL_PII_RE.sub("[masked]", label)[:80]


def create_request(session_dir: Path, *, kind: str, job_url: str, action: dict,
                   ttl_s: float, now: float | None = None) -> dict:
    """Open a request. The returned dict carries the full ``action`` for the caller's in-memory
    use; the file on disk holds only its digest and a sanitized display copy."""
    now = time.time() if now is None else now
    session_dir = Path(session_dir)
    nonce = secrets.token_hex(16)
    from applypilot.apply.redact import safe_url
    req = {"nonce": nonce, "kind": kind, "session_id": session_dir.name, "job": _job_key(job_url),
           "display": display_action(action), "digest": action_digest(action), "created": now,
           "expires": now + ttl_s, "job_display": safe_url(job_url)}
    _dir(session_dir).mkdir(parents=True, exist_ok=True)
    _req_path(session_dir, nonce).write_text(json.dumps(req), encoding="utf-8")
    return {**req, "action": action, "job_url": job_url}


def pending_request(session_dir: Path, nonce: str) -> dict | None:
    if not valid_nonce(nonce):
        return None
    return _load(_req_path(Path(session_dir), nonce))


def grant(session_dir: Path, nonce: str, now: float | None = None, expected_digest: str | None = None) -> bool:
    """Human grant. False for unknown, malformed, expired, used or already granted nonces.

    ``expected_digest``: the digest of the request the human was shown; the grant is refused if
    the request on disk no longer matches it."""
    now = time.time() if now is None else now
    session_dir = Path(session_dir)
    req = pending_request(session_dir, nonce)
    if expected_digest is not None and (req is None or req.get("digest") != expected_digest):
        return False
    if (req is None or req.get("nonce") != nonce or _closed(session_dir, nonce)
            or req.get("session_id") != session_dir.name or not now <= float(req.get("expires", 0))
            or _grant_path(session_dir, nonce).exists()):
        return False
    tmp = _dir(session_dir) / f".grant-{nonce}-{secrets.token_hex(4)}.tmp"
    tmp.write_text(json.dumps({
        "nonce": nonce, "session_id": req["session_id"], "job": req.get("job"),
        "digest": req.get("digest"), "granted_at": now}), encoding="utf-8")
    try:
        # A hard link publishes the complete file atomically and fails if a grant already exists
        # (os.rename would silently replace it on POSIX).
        os.link(tmp, _grant_path(session_dir, nonce))
    except OSError:
        return False
    finally:
        _unlink_quietly(tmp)
    if _closed(session_dir, nonce):  # withdrawn while we were granting: the grant is void
        _unlink_quietly(_grant_path(session_dir, nonce))
        return False
    return True


def consume(session_dir: Path, nonce: str, *, job_url: str, digest: str,
            now: float | None = None) -> bool:
    """True exactly once, for a matching, timely grant of this exact action."""
    now = time.time() if now is None else now
    session_dir = Path(session_dir)
    if not valid_nonce(nonce) or _closed(session_dir, nonce):
        return False
    req, grt = _load(_req_path(session_dir, nonce)), _load(_grant_path(session_dir, nonce))
    if req is None or grt is None:
        return False
    try:
        created, expires, granted = float(req["created"]), float(req["expires"]), float(grt["granted_at"])
    except (KeyError, TypeError, ValueError):
        return False
    ok = (req.get("nonce") == grt.get("nonce") == nonce
          and req.get("session_id") == grt.get("session_id") == session_dir.name
          and req.get("job") == grt.get("job") == _job_key(job_url)
          and req.get("digest") == grt.get("digest") == digest

          and created <= granted <= expires and now <= expires)
    if not ok:
        return False
    if not _claim(_marker(session_dir, nonce, "used")):
        return False  # a concurrent consumer won, or the request was withdrawn
    if _marker(session_dir, nonce, "cancelled").exists():
        return False  # withdrawn in the same instant: fail closed (never a double outcome)
    # Committed. Everything below is bookkeeping and must not change the outcome.
    try:
        with open(session_dir / USED_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"nonce": nonce, "kind": req.get("kind"), "t": now}) + "\n")
    except OSError:
        pass
    withdraw(session_dir, nonce)
    return True


def withdraw(session_dir: Path, nonce: str) -> None:
    """Close a request for good (expiry / cancellation / after use) and delete its files.

    The marker is the committed state; file deletion is best-effort and never raises."""
    if valid_nonce(nonce):
        if not _marker(Path(session_dir), nonce, "used").exists():
            _claim(_marker(Path(session_dir), nonce, "cancelled"))
        _unlink_quietly(_req_path(Path(session_dir), nonce))
        _unlink_quietly(_grant_path(Path(session_dir), nonce))


def cancel_all(session_dir: Path) -> int:
    """Withdraw every open request of a session (end of session / crash cleanup)."""
    d = _dir(Path(session_dir))
    n = 0
    for p in d.glob("req-*.json") if d.exists() else []:
        nonce = p.stem[4:]
        if valid_nonce(nonce):
            withdraw(Path(session_dir), nonce)
            n += 1
    for p in d.glob("grant-*.json") if d.exists() else []:
        _unlink_quietly(p)
    return n



def wait_for_grant(session_dir: Path, req: dict, *, poll_s: float = 1.0,

                   sleep=time.sleep, clock=time.time) -> bool:
    """Block until ``req`` is granted and consumed, or it expires (then it is withdrawn)."""
    try:
        while clock() <= req["expires"]:
            if consume(session_dir, req["nonce"], job_url=req["job_url"], digest=req["digest"], now=clock()):
                return True
            sleep(poll_s)
        return False
    finally:
        withdraw(session_dir, req["nonce"])
