"""Cross-source duplicate detection (fork).

The same opening is often discovered on several boards and on the employer's own
career site. Rows are never deleted or merged: a duplicate keeps its own URL and
source history and gets ``duplicate_of`` = the canonical row's URL plus a reason.

A pair is a duplicate only on strong evidence (conservative -- similar titles at one
employer are often genuinely different openings):

1. the same canonical application URL (tracking parameters, ``www.``, ATS host aliases
   and ``/apply`` suffixes removed); or
2. the same normalised employer and the same requisition ID; or
3. the same normalised employer, title, location and employment type, **and** both
   descriptions present and near-identical (word-shingle Jaccard >= 0.85).

Different requisition IDs always mean different openings. The canonical row is the one
with an employer/ATS application URL, else an applied/claimed one, else the earliest seen.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from applypilot.database import get_connection

log = logging.getLogger(__name__)

_COMPANY_SUFFIX = re.compile(
    r"\b(incorporated|inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|lp|llp|group|holdings|"
    r"the|usa|us|na|north america)\b\.?", re.IGNORECASE)
_TITLE_ABBR = {"sr": "senior", "jr": "junior", "eng": "engineer", "engr": "engineer", "admin": "administrator",
               "tech": "technician", "mgr": "manager", "assoc": "associate", "spec": "specialist",
               "sys": "systems", "sysadmin": "systems administrator", "svc": "service", "hd": "help desk",
               "helpdesk": "help desk", "desktop support": "desktop support"}
_TRACKING = re.compile(r"^(utm_.*|gh_src|gh_jid_src|source|src|ref|referrer|lever-source|lever-origin|"
                       r"trk|trackingid|refid|codes|mode|jobsearch|sid|iis|iisn|ccuid|jobpipeline|"
                       r"bid|cid|campaign|_ga|fbclid|gclid|mc_cid|mc_eid)$", re.IGNORECASE)
_REQ_PATTERNS = (
    re.compile(r"\b(?:req(?:uisition)?|job)\s*(?:id|#|no\.?|number)?\s*[:#]?\s*([A-Z]{0,4}[-_]?\d{4,10})\b", re.IGNORECASE),
    re.compile(r"_(R-?\d{4,10}|JR-?\d{4,10}|REQ-?\d{4,10})(?:[-_/?]|$)", re.IGNORECASE),
)
_HOST_ALIASES = {"job-boards.greenhouse.io": "boards.greenhouse.io", "jobs.ashbyhq.com": "jobs.ashbyhq.com"}
_BOARD_HOSTS = ("indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com", "simplyhired.com",
                "monster.com", "dice.com", "google.com")
SIMILARITY = 0.85


def norm_company(name: str | None) -> str:
    s = (name or "").lower().replace("&", " and ")
    s = _COMPANY_SUFFIX.sub(" ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_title(title: str | None) -> str:
    s = (title or "").lower()
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)          # "(Remote)", "[Hybrid]"
    s = re.sub(r"\s+-\s+(remote|hybrid|on-?site).*$", " ", s)
    s = re.sub(r"[^a-z0-9+#/ ]+", " ", s)
    words = [_TITLE_ABBR.get(w, w) for w in s.split()]
    return re.sub(r"\s+", " ", " ".join(words)).strip()


def norm_location(loc: str | None) -> str:
    s = (loc or "").lower()
    if "remote" in s and not re.search(r"\b[a-z]{2,}\b,\s*[a-z]{2}\b", s.replace("remote", "")):
        return "remote"
    s = re.sub(r"\(remote\)|remote|united states|usa|us\b", " ", s)
    s = re.sub(r"[^a-z0-9, ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip(" ,")


def canonical_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (p.hostname or "").lower()
    host = host.removeprefix("www.")
    host = _HOST_ALIASES.get(host, host)
    path = re.sub(r"/+", "/", p.path).rstrip("/")
    path = re.sub(r"/(apply|application|applynow)$", "", path, flags=re.IGNORECASE)
    if "myworkdayjobs.com" in host:
        path = re.sub(r"^/[a-z]{2}-[A-Z]{2}(?=/)", "", path)  # locale prefix
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
                             if not _TRACKING.match(k)))
    return urlunsplit(("https", host, path.lower() if "workday" not in host else path, query, ""))


def requisition_id(job: dict) -> str | None:
    if job.get("requisition_id"):
        return str(job["requisition_id"]).upper().replace("_", "-")
    for text in (job.get("application_url") or "", job.get("url") or "", (job.get("full_description") or "")[:4000]):
        for pat in _REQ_PATTERNS:
            m = pat.search(text)
            if m:
                return m.group(1).upper().replace("_", "-")
    return None


def _shingles(text: str, n: int = 3) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {tuple(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def similarity(a: str | None, b: str | None) -> float:
    sa, sb = _shingles(a or ""), _shingles(b or "")
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def company_of(job: dict) -> str:
    return norm_company(job.get("company") or ("" if job.get("strategy") == "jobspy" else job.get("site")))


def dedupe_key(job: dict) -> str:
    return "|".join((company_of(job), norm_title(job.get("title")), norm_location(job.get("location"))))


def same_opening(a: dict, b: dict) -> str | None:
    """Reason string if ``a`` and ``b`` are the same opening, else None."""
    ca, cb = canonical_url(a.get("application_url")), canonical_url(b.get("application_url"))
    if ca and ca == cb:
        return "same application URL"
    if canonical_url(a.get("url")) == canonical_url(b.get("url")):
        return "same posting URL"
    comp_a, comp_b = company_of(a), company_of(b)
    if not comp_a or comp_a != comp_b:
        return None
    ra, rb = requisition_id(a), requisition_id(b)
    if ra and rb:
        return "same employer and requisition ID" if ra == rb else None
    if norm_title(a.get("title")) != norm_title(b.get("title")):
        return None
    if norm_location(a.get("location")) != norm_location(b.get("location")):
        return None
    ta, tb = (a.get("job_type") or "").lower(), (b.get("job_type") or "").lower()
    if ta and tb and ta != tb:
        return None
    sim = similarity(a.get("full_description") or a.get("description"),
                     b.get("full_description") or b.get("description"))
    if sim >= SIMILARITY:
        return f"same employer/title/location, descriptions {sim:.0%} similar"
    return None


def _rank(job: dict) -> tuple:
    host = (urlsplit(job.get("application_url") or job.get("url") or "").hostname or "").lower()
    official = not any(host == h or host.endswith("." + h) for h in _BOARD_HOSTS)
    claimed = job.get("apply_status") in ("applied", "in_progress", "needs_review", "manual")
    return (0 if claimed else 1, 0 if official else 1, job.get("first_seen_at") or job.get("discovered_at") or "",
            job.get("url"))


_COLS = ("url, title, company, site, strategy, location, job_type, application_url, full_description, description,"
         " requisition_id, apply_status, first_seen_at, discovered_at, duplicate_of")


def run_dedupe(conn: sqlite3.Connection | None = None) -> dict:
    """Group duplicates and record ``dedupe_key`` / ``duplicate_of``. Idempotent."""
    conn = conn or get_connection()
    rows = [dict(r) for r in conn.execute(f"SELECT {_COLS} FROM jobs").fetchall()]
    # Blocking: compare only rows sharing an application URL, a posting URL or a company.
    buckets: dict[str, list[int]] = {}
    for i, j in enumerate(rows):
        for key in (("a:" + canonical_url(j.get("application_url"))) if j.get("application_url") else "",
                    "u:" + canonical_url(j.get("url")), ("c:" + company_of(j)) if company_of(j) else ""):
            if key:
                buckets.setdefault(key, []).append(i)
    parent = list(range(len(rows)))
    reason: dict[int, str] = {}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for members in buckets.values():
        if len(members) < 2 or len(members) > 400:
            continue
        for ai in range(len(members)):
            for bi in range(ai + 1, len(members)):
                a, b = members[ai], members[bi]
                if find(a) == find(b):
                    continue
                why = same_opening(rows[a], rows[b])
                if why:
                    parent[find(b)] = find(a)
                    reason[b] = reason.get(b) or why
                    reason[a] = reason.get(a) or why
    groups: dict[int, list[int]] = {}
    for i in range(len(rows)):
        groups.setdefault(find(i), []).append(i)
    dup = 0
    for members in groups.values():
        canonical = min(members, key=lambda i: _rank(rows[i]))
        for i in members:
            j = rows[i]
            of = None if i == canonical else rows[canonical]["url"]
            why = None if of is None else reason.get(i, "duplicate group")
            dup += of is not None
            conn.execute("UPDATE jobs SET dedupe_key = ?, duplicate_of = ?, duplicate_reason = ? WHERE url = ?",
                         (dedupe_key(j), of, why, j["url"]))
    conn.commit()
    log.info("dedupe: %d rows, %d duplicates", len(rows), dup)
    return {"status": "ok", "rows": len(rows), "duplicates": dup}


def group_urls(conn: sqlite3.Connection, url: str) -> list[str]:
    """Every URL in ``url``'s duplicate group (including itself)."""
    row = conn.execute("SELECT COALESCE(duplicate_of, url) AS root FROM jobs WHERE url = ?", (url,)).fetchone()
    if not row:
        return [url]
    root = row["root"] if isinstance(row, sqlite3.Row) else row[0]
    return [r[0] for r in conn.execute("SELECT url FROM jobs WHERE url = ? OR duplicate_of = ?", (root, root))]
