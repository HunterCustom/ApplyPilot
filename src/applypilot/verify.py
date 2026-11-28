"""Posting verification and freshness (fork).

Before any document is prepared, confirm that the employer's posting still exists and
accepts applications. One plain HTTP GET per posting (no JavaScript, no login, no
CAPTCHA handling), honouring robots.txt and a per-host delay.

Outcomes (``jobs.posting_status``):

* ``active``       -- page loads and names the job; no closed-posting phrase.
* ``closed``       -- the page itself says the posting is closed / filled / expired.
* ``removed``      -- HTTP 404 or 410 from the posting URL.
* ``inaccessible`` -- blocked, rate-limited, login / bot wall, robots.txt disallows us,
                      network error or timeout. Says nothing about the posting itself.
* ``uncertain``    -- loaded, but could not be matched to this job (e.g. redirected to a
                      generic careers page). Routed to manual review, never treated as closed.

Only ``active`` (recent enough, see ``is_fresh``) lets a job become application-ready.
The official employer / ATS URL (``application_url``) is preferred over job-board URLs.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from urllib.parse import urlsplit

from applypilot import opsconfig
from applypilot.database import get_connection
from applypilot.dates import age_days

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; ApplyPilot-posting-check/1.0; personal job search)"
MAX_BYTES = 2_000_000
JOB_BOARD_HOSTS = ("indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com", "simplyhired.com",
                   "monster.com", "dice.com", "google.com")

CLOSED_PHRASES = re.compile(
    r"no longer (accepting applications|available|active|open|being accepted)|"
    r"(position|job|role|requisition|posting|vacancy) (has been|was|is) (filled|closed|removed|cancel+ed|expired)|"
    r"(this|the) (job|position|posting|vacancy|opening|requisition) (has )?(expired|closed|is closed)|"
    r"applications? (are|is) (now )?closed|job (posting )?not found|page you requested (was not found|"
    r"could not be found)|this job is no longer|we('re| are) no longer accepting|"
    r"the job you are (trying to apply for|looking for) has been (filled|closed|removed)",
    re.IGNORECASE,
)
WALL_MARKERS = re.compile(
    r"captcha|verify (that )?you are (a )?human|are you a robot|cf-chl|challenge-platform|access denied|"
    r"please enable (javascript|cookies)|attention required|request blocked|unusual traffic|sign in to (view|continue)",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<script.*?</script>|<style.*?</style>|<[^>]+>", re.IGNORECASE | re.DOTALL)
_STOP = {"and", "the", "for", "with", "of", "in", "to", "a", "an", "remote", "hybrid", "onsite", "i", "ii", "iii",
         "iv", "sr", "jr", "senior", "junior", "level", "lead", "-", "/", "&"}


@dataclass(frozen=True)
class CheckResult:
    status: str
    reason: str
    http_status: int | None = None
    checked_url: str = ""


Fetcher = Callable[[str, float], tuple[int, str, str]]  # (status, final_url, text)


def http_fetch(url: str, timeout: float) -> tuple[int, str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # http(s) only (checked by caller)
            body = resp.read(MAX_BYTES).decode(resp.headers.get_content_charset() or "utf-8", "replace")
            return resp.status, resp.geturl(), body
    except urllib.error.HTTPError as e:
        try:
            body = e.read(200_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, url, body


class RobotsCache:
    def __init__(self, fetch: Fetcher, timeout: float) -> None:
        self.fetch, self.timeout = fetch, timeout
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self._lock:
            if origin not in self._cache:
                rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
                try:
                    status, _, text = self.fetch(origin + "/robots.txt", self.timeout)
                    if status >= 400:
                        rp = None  # no robots.txt (or unreadable): no restriction stated
                    else:
                        assert rp is not None
                        rp.parse(text.splitlines())
                except Exception:  # noqa: BLE001
                    rp = None
                self._cache[origin] = rp
            rp = self._cache[origin]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)


class HostThrottle:
    def __init__(self, delay_s: float) -> None:
        self.delay = delay_s
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str, sleep=time.sleep) -> None:
        host = (urlsplit(url).hostname or "").lower()
        with self._lock:
            now = time.monotonic()
            due = self._last.get(host, 0.0) + self.delay
            self._last[host] = max(now, due)
        if due > now:
            sleep(due - now)


def _title_tokens(title: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9+#]+", (title or "").lower()) if t not in _STOP and len(t) > 1}


def page_text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG_RE.sub(" ", html or "")))


def classify_response(status: int, final_url: str, html: str, title: str) -> tuple[str, str]:
    """Pure decision for one fetched page."""
    if status in (404, 410):
        return "removed", f"HTTP {status}"
    if status in (401, 403, 407, 429) or status >= 500:
        return "inaccessible", f"HTTP {status}"
    if status >= 400:
        return "inaccessible", f"HTTP {status}"
    text = page_text(html)
    m = CLOSED_PHRASES.search(text)
    if m:
        return "closed", f"page says: '{m.group(0)[:80]}'"
    if WALL_MARKERS.search(text) and len(text) < 20000:
        return "inaccessible", "bot/login wall (not bypassed)"
    tokens = _title_tokens(title)
    if not tokens:
        return "uncertain", "job title unknown; cannot match the page"
    hay = text.lower()
    found = {t for t in tokens if t in hay}
    if len(found) >= max(1, (len(tokens) + 1) // 2):
        return "active", f"page names the job ({len(found)}/{len(tokens)} title words)"
    if len(text) < 500:
        return "uncertain", "page content is script-rendered; cannot confirm the posting"
    return "uncertain", "page does not mention this job (possibly redirected to a generic page)"


def preferred_url(job: dict) -> str:
    """Official employer / ATS URL when known, else the discovered URL."""
    app = job.get("application_url") or ""
    host = (urlsplit(app).hostname or "").lower()
    if app.startswith(("http://", "https://")) and not any(host == h or host.endswith("." + h) for h in JOB_BOARD_HOSTS):
        return app
    return job.get("url") or app


def check_posting(job: dict, *, fetch: Fetcher = http_fetch, robots: RobotsCache | None = None,
                  throttle: HostThrottle | None = None, timeout: float = 20.0) -> CheckResult:
    url = preferred_url(job)
    if not url.startswith(("http://", "https://")):
        return CheckResult("uncertain", "no http(s) posting URL", None, url)
    if robots is not None and not robots.allowed(url):
        return CheckResult("inaccessible", "robots.txt disallows automated checks", None, url)
    if throttle is not None:
        throttle.wait(url)
    try:
        status, final_url, html = fetch(url, timeout)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return CheckResult("inaccessible", f"fetch failed: {type(e).__name__}", None, url)
    st, reason = classify_response(status, final_url, html, job.get("title") or "")
    return CheckResult(st, reason, status, url)


# --- freshness -----------------------------------------------------------------------------------

def is_fresh(job: dict, cfg: dict | None = None, now: datetime | None = None) -> tuple[bool, str]:
    """Posting age within ``freshness_max_days`` and a recent ``active`` verification."""
    cfg = cfg or opsconfig.load()
    max_days = float(cfg["freshness_max_days"])
    if job.get("posting_status") != "active":
        return False, f"posting not verified active ({job.get('posting_status') or 'never checked'})"
    checked = age_days(job.get("posting_checked_at"), now)
    if checked is None or checked * 24 > float(cfg["verify_max_age_hours"]):
        return False, "posting verification is stale; re-run verify"
    posted = age_days(job.get("date_posted"), now)
    if posted is not None:
        if posted > max_days:
            return False, f"posted {posted:.0f} days ago (limit {max_days:.0f})"
        return True, f"posted {posted:.0f} days ago"
    seen = age_days(job.get("first_seen_at") or job.get("discovered_at"), now)
    if seen is None:
        return False, "no posting date and no discovery time"
    if seen > max_days:
        return False, f"first seen {seen:.0f} days ago (limit {max_days:.0f}); posting date unknown"
    return True, f"posting date unknown; first seen {seen:.0f} days ago"


# --- stage ------------------------------------------------------------------------------------------

_CANDIDATES = """
    SELECT url, title, application_url, posting_status, posting_checked_at FROM jobs
    WHERE (apply_status IS NULL OR apply_status IN ('failed', 'dry_run'))
      AND duplicate_of IS NULL
      AND (eligibility_status IS NULL OR eligibility_status NOT LIKE 'ineligible%')
      AND (posting_status IS NULL OR posting_status NOT IN ('removed', 'closed'))
      AND (posting_checked_at IS NULL OR posting_checked_at < ?)
    ORDER BY CASE WHEN fit_score IS NULL THEN 1 ELSE 0 END, fit_score DESC, discovered_at DESC
    LIMIT ?
"""


def run_verification(limit: int | None = None, conn: sqlite3.Connection | None = None,
                     fetch: Fetcher = http_fetch) -> dict:
    """Check postings whose verification is missing or older than ``verify_max_age_hours``."""
    cfg = opsconfig.load()
    conn = conn or get_connection()
    limit = limit if limit is not None else int(cfg["daily"]["max_verify"])
    cutoff = datetime.fromtimestamp(time.time() - float(cfg["verify_max_age_hours"]) * 3600, tz=UTC)
    rows = [dict(r) for r in conn.execute(_CANDIDATES, (cutoff.isoformat(), limit)).fetchall()]
    timeout = float(cfg["verify_timeout_s"])
    robots, throttle = RobotsCache(fetch, timeout), HostThrottle(float(cfg["verify_delay_s"]))
    counts: dict[str, int] = {}
    for job in rows:
        try:
            res = check_posting(job, fetch=fetch, robots=robots, throttle=throttle, timeout=timeout)
        except Exception as e:  # noqa: BLE001 -- one bad page never stops the stage
            res = CheckResult("inaccessible", f"checker error: {type(e).__name__}", None, preferred_url(job))
        counts[res.status] = counts.get(res.status, 0) + 1
        conn.execute("UPDATE jobs SET posting_status = ?, posting_reason = ?, posting_http_status = ?,"
                     " posting_checked_at = ?, posting_verified_url = ? WHERE url = ?",
                     (res.status, res.reason[:300], res.http_status, datetime.now(UTC).isoformat(),
                      res.checked_url[:1000], job["url"]))
        conn.commit()
    log.info("verify: %d checked %s", len(rows), counts)
    return {"status": "ok", "checked": len(rows), **counts}
