"""Narrow email-verification helper for the apply agent (S3).

A minimal MCP stdio server exposing exactly one tool, ``get_verification_code``.
It replaces the general Gmail MCP: the agent cannot search, read, list or send
mail. The search is built here, never by the agent:

    from:(<session sender domains>) after:<session start - 2 min>

and only the extracted code or verification link is returned. Every From
header is re-checked against the session's sender domains, links must pass the
session navigation policy, and returned values are written to the session's
``secrets.txt`` so the launcher can redact them from logs.

Credentials: a JSON file outside Git (``APPLYPILOT_GMAIL_CREDENTIALS`` or
``<APP_DIR>/secrets/gmail_readonly.json``) with ``client_id``,
``client_secret`` and ``refresh_token`` for an OAuth grant whose scope is
exactly ``gmail.readonly``. Any broader scope is refused (fail closed).

Run: python -m applypilot.apply.verification_mcp <policy.json>
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import sys
from email.utils import parseaddr
from pathlib import Path

from applypilot.apply import policy as P

READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TOOL_NAME = "get_verification_code"
MAX_CALLS = 5
_GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"

_CODE_RE = re.compile(r"(?:code|passcode|pin|one[- ]time|verification)\D{0,40}?\b(\d{4,8})\b", re.IGNORECASE)
# Alphanumeric codes only directly after "code:" and only if they contain a digit.
_ALNUM_CODE_RE = re.compile(r"\bcode\s*[:=]?\s*\b((?=[A-Z0-9]*\d)[A-Z0-9]{6,8})\b")
_LINK_RE = re.compile(r"https://[^\s\"'<>]+")
_LINK_WORDS = re.compile(r"verif|confirm|activat|validate", re.IGNORECASE)


class VerificationError(Exception):
    pass


def credentials_path() -> Path:
    env = os.environ.get("APPLYPILOT_GMAIL_CREDENTIALS")
    if env:
        return Path(env)
    from applypilot import config
    return config.APP_DIR / "secrets" / "gmail_readonly.json"


def build_query(sender_domains: list[str], started_at: float) -> str:
    doms = [d for d in sender_domains if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", d or "")]
    if not doms:
        raise VerificationError("no sender domains configured for this session")
    return f"from:({' OR '.join(doms)}) after:{int(started_at) - 120}"


def sender_allowed(from_header: str, sender_domains: list[str]) -> bool:
    addr = parseaddr(from_header or "")[1].lower()
    if "@" not in addr:
        return False
    dom = addr.rsplit("@", 1)[1]
    return any(dom == d or dom.endswith("." + d) for d in sender_domains)


def _decode_body(payload: dict) -> str:
    texts: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime in ("text/plain", "text/html"):
            raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            if mime == "text/html":
                links = re.findall(r'href="(https://[^"]+)"', raw)
                raw = re.sub(r"<[^>]+>", " ", raw) + " " + " ".join(links)
            texts.append(html.unescape(raw))
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload or {})
    return "\n".join(texts)


def extract(text: str, policy: P.SessionPolicy) -> dict | None:
    m = _CODE_RE.search(text or "") or _ALNUM_CODE_RE.search(text or "")
    if m:
        return {"code": m.group(1)}
    for url in _LINK_RE.findall(text or ""):
        url = url.rstrip(").,;")
        if _LINK_WORDS.search(url) and P.url_block_reason(url, policy.allowed_hosts, policy.trusted_hosts) is None:
            return {"link": url}
    return None


class GmailReadonly:
    """Tiny Gmail REST client. ``http`` must provide get/post like httpx.Client."""

    def __init__(self, creds: dict, http):
        self.http = http
        resp = http.post("https://oauth2.googleapis.com/token", data={
            "client_id": creds["client_id"], "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"], "grant_type": "refresh_token",
        })
        if resp.status_code != 200:
            raise VerificationError("could not refresh the Gmail token")
        tok = resp.json()
        scopes = set((tok.get("scope") or "").split())
        if scopes != {READONLY_SCOPE}:
            raise VerificationError("Gmail token scope must be exactly gmail.readonly; refusing")
        self.headers = {"Authorization": f"Bearer {tok['access_token']}"}

    def search(self, query: str) -> list[dict]:
        r = self.http.get(f"{_GMAIL}/messages", params={"q": query, "maxResults": 5}, headers=self.headers)
        r.raise_for_status()
        out = []
        for m in r.json().get("messages", [])[:5]:
            mr = self.http.get(f"{_GMAIL}/messages/{m['id']}", params={"format": "full"}, headers=self.headers)
            mr.raise_for_status()
            out.append(mr.json())
        return out


def lookup(policy: P.SessionPolicy, client) -> dict:
    """Return {'code'|'link': value} or {'status': 'not_found'}. Never returns mail content."""
    query = build_query(policy.verify_sender_domains, policy.started_at)
    for msg in client.search(query):
        payload = msg.get("payload") or {}
        headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}
        if not sender_allowed(headers.get("from", ""), policy.verify_sender_domains):
            continue
        if int(msg.get("internalDate", "0")) / 1000 < policy.started_at - 120:
            continue
        found = extract(_decode_body(payload), policy)
        if found:
            with open(policy.dir / "secrets.txt", "a", encoding="utf-8") as f:
                f.write(next(iter(found.values())) + "\n")
            return found
    return {"status": "not_found"}


_TOOL = {
    "name": TOOL_NAME,
    "description": ("Fetch the verification code or link that this employer's application site just "
                    "emailed. Takes no query; returns only the code/link, or not_found. Call again "
                    "after waiting if not_found (max 5 calls)."),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}


class Server:
    def __init__(self, policy: P.SessionPolicy, client_factory):
        self.policy = policy
        self.client_factory = client_factory
        self.calls = 0

    def handle(self, req: dict) -> dict | None:
        method, rid = req.get("method"), req.get("id")
        if rid is None:
            return None  # notification
        if method == "initialize":
            ver = (req.get("params") or {}).get("protocolVersion", "2025-06-18")
            result = {"protocolVersion": ver, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "applypilot_verify", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [_TOOL]}
        elif method == "tools/call":
            result = self._call((req.get("params") or {}).get("name"))
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _call(self, name: str) -> dict:
        if name != TOOL_NAME:
            return {"content": [{"type": "text", "text": "unknown tool"}], "isError": True}
        self.calls += 1
        if self.calls > MAX_CALLS:
            out = {"status": "limit_reached"}
        else:
            try:
                out = lookup(self.policy, self.client_factory())
            except VerificationError as e:
                out = {"status": "unavailable", "reason": str(e)}
            except Exception:  # noqa: BLE001 -- never leak errors/mail content to the agent
                out = {"status": "unavailable", "reason": "lookup failed"}
        return {"content": [{"type": "text", "text": json.dumps(out)}]}


def _default_client():
    import httpx
    path = credentials_path()
    if not path.exists():
        raise VerificationError("verification email access is not configured")
    creds = json.loads(path.read_text(encoding="utf-8"))
    return GmailReadonly(creds, httpx.Client(timeout=20))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    server = Server(P.SessionPolicy.load(argv[0]), _default_client)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            resp = server.handle(json.loads(line))
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
