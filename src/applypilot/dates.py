"""Strict date helpers (fork). Posting dates are stored only when a source states one exactly.

Relative phrases ("Posted 3 days ago", "30+ days") are never converted into dates: a
wrong date is worse than none, and freshness then falls back to first-seen time.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def iso_date(value) -> str | None:
    """YYYY-MM-DD for a real date/datetime or an ISO-formatted string; otherwise None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value).strip()
    m = _ISO_RE.match(s)
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
    except ValueError:
        return None


def parse_ts(value: str | None) -> datetime | None:
    """Parse a stored ISO timestamp or date (UTC assumed when naive)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        d = iso_date(value)
        if not d:
            return None
        dt = datetime.fromisoformat(d)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def age_days(value: str | None, now: datetime | None = None) -> float | None:
    dt = parse_ts(value)
    if dt is None:
        return None
    now = now or datetime.now(UTC)
    return (now - dt).total_seconds() / 86400.0
