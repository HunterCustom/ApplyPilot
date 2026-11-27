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
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from pathlib import Path

APPROVAL_DIR = "approvals"
USED_FILE = "approvals-used.jsonl"
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


def action_digest(action: dict) -> str:
    return hashlib.sha256(json.dumps(action, sort_keys=True, default=str).encode()).hexdigest()


def _dir(session_dir: Path) -> Path:
    return Path(session_dir) / APPROVAL_DIR


def _req_path(session_dir: Path, nonce: str) -> Path:
    return _dir(session_dir) / f"req-{nonce}.json"


def _grant_path(session_dir: Path, nonce: str) -> Path:
    return _dir(session_dir) / f"grant-{nonce}.json"


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


def create_request(session_dir: Path, *, kind: str, job_url: str, action: dict,
                   ttl_s: float, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    session_dir = Path(session_dir)
    nonce = secrets.token_hex(16)
    req = {"nonce": nonce, "kind": kind, "session_id": session_dir.name, "job_url": job_url,
           "action": action, "digest": action_digest(action), "created": now, "expires": now + ttl_s}
    _dir(session_dir).mkdir(parents=True, exist_ok=True)
    _req_path(session_dir, nonce).write_text(json.dumps(req), encoding="utf-8")
    return req


def pending_request(session_dir: Path, nonce: str) -> dict | None:
    if not valid_nonce(nonce):
        return None
    return _load(_req_path(Path(session_dir), nonce))


def grant(session_dir: Path, nonce: str, now: float | None = None) -> bool:
    """Human grant. False for unknown, malformed, expired, used or already granted nonces."""
    now = time.time() if now is None else now
    session_dir = Path(session_dir)
    req = pending_request(session_dir, nonce)
    if (req is None or req.get("nonce") != nonce or nonce in _used(session_dir)
            or req.get("session_id") != session_dir.name or not now <= float(req.get("expires", 0))
            or _grant_path(session_dir, nonce).exists()):
        return False
    _grant_path(session_dir, nonce).write_text(json.dumps({
        "nonce": nonce, "session_id": req["session_id"], "job_url": req.get("job_url"),
        "digest": req.get("digest"), "granted_at": now}), encoding="utf-8")
    return True


def consume(session_dir: Path, nonce: str, *, job_url: str, digest: str,
            now: float | None = None) -> bool:
    """True exactly once, for a matching, timely grant of this exact action."""
    now = time.time() if now is None else now
    session_dir = Path(session_dir)
    if not valid_nonce(nonce) or nonce in _used(session_dir):
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
          and req.get("job_url") == grt.get("job_url") == job_url
          and req.get("digest") == grt.get("digest") == digest == action_digest(req.get("action") or {})
          and created <= granted <= expires and now <= expires)
    if not ok:
        return False
    with open(session_dir / USED_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"nonce": nonce, "kind": req.get("kind"), "t": now}) + "\n")
    withdraw(session_dir, nonce)
    return True


def withdraw(session_dir: Path, nonce: str) -> None:
    """Remove a request (and any grant). Used when it expires or is consumed."""
    if valid_nonce(nonce):
        _req_path(Path(session_dir), nonce).unlink(missing_ok=True)
        _grant_path(Path(session_dir), nonce).unlink(missing_ok=True)


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
