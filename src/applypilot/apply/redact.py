"""Redaction of personal data, credentials and verification codes before anything reaches disk (S4).

Apply-session logs (``logs/worker-N.log``, ``logs/claude_*.txt``) must never
contain the applicant's contact details, passwords, EEO answers or one-time
codes. ``Redactor`` is built from the profile and masks every known value,
plus generic patterns (email addresses, phone numbers, 6-8 digit codes and
token-bearing URL query strings) so values the agent re-formats are caught too.
"""
from __future__ import annotations

import re
from pathlib import Path

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_CODE_RE = re.compile(r"(?<![\d/.-])\d{6,8}(?![\d/.-])")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_TOKEN_QS_RE = re.compile(r"([?&](?:token|code|key|sig|signature|auth|otp|password|pwd|session|t)=)[^&\s\"'<>]+",
                          re.IGNORECASE)

# Profile fields whose values are masked wherever they appear.
_SENSITIVE_KEYS = {
    "password", "email", "phone", "address", "street", "address_line1", "address_line2",
    "postal_code", "zip", "zip_code", "date_of_birth", "dob", "ssn", "full_name",
    "gender", "race", "ethnicity", "race_ethnicity", "veteran_status", "disability_status",
    "sexual_orientation", "linkedin_url", "github_url", "portfolio_url", "website", "website_url",
}


_TOKENISH_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9_\-.=~%+]{20,}$|^[A-Fa-f0-9-]{24,}$")


def safe_url(url: str, limit: int = 200) -> str:
    """URL safe to persist or display (audit M4): scheme, host and port only from the authority
    (no userinfo), no query or fragment, and long token-like path segments masked."""
    from urllib.parse import urlsplit, urlunsplit
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        port = f":{p.port}" if p.port else ""
    except ValueError:
        return "(unparseable)"
    segs = ["[token]" if _TOKENISH_RE.match(s) or _EMAIL_RE.search(s) else s for s in p.path.split("/")]
    return urlunsplit((p.scheme, host + port, "/".join(segs), "", ""))[:limit]


def _collect(obj, out: list[tuple[str, str]], key: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _collect(v, out, str(k).lower())
    elif isinstance(obj, list):
        for v in obj:
            _collect(v, out, key)
    elif isinstance(obj, (str, int)) and key in _SENSITIVE_KEYS:
        s = str(obj).strip()
        if len(s) >= 3:
            out.append((key, s))


class Redactor:
    def __init__(self, profile: dict | None = None, extra_values: list[str] | None = None,
                 secrets_file: Path | None = None):
        pairs: list[tuple[str, str]] = []
        _collect(profile or {}, pairs)
        self._values = sorted({(v, k) for k, v in pairs}, key=lambda x: -len(x[0]))
        self._extra = [v for v in (extra_values or []) if v and len(v) >= 3]
        self._secrets_file = secrets_file
        self._secrets_mtime = None
        self._secrets: list[str] = []

    def _load_secrets(self) -> None:
        f = self._secrets_file
        if not f or not f.exists():
            return
        mtime = f.stat().st_mtime
        if mtime != self._secrets_mtime:
            self._secrets_mtime = mtime
            self._secrets = [s.strip() for s in f.read_text(encoding="utf-8").splitlines() if len(s.strip()) >= 3]

    def __call__(self, text: str) -> str:
        if not text:
            return text
        self._load_secrets()
        for value in sorted(self._secrets + self._extra, key=len, reverse=True):
            text = text.replace(value, "[REDACTED:secret]")
        for value, key in self._values:
            text = re.sub(re.escape(value), f"[REDACTED:{key}]", text, flags=re.IGNORECASE)
            digits = re.sub(r"\D", "", value)
            if key == "phone" and len(digits) >= 7:
                text = text.replace(digits, "[REDACTED:phone]")
        text = _TOKEN_QS_RE.sub(r"\1[REDACTED]", text)
        text = _EMAIL_RE.sub("[REDACTED:email]", text)
        text = _SSN_RE.sub("[REDACTED:ssn]", text)
        text = _PHONE_RE.sub("[REDACTED:phone]", text)
        text = _CODE_RE.sub("[REDACTED:code]", text)
        return text
