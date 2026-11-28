"""Cross-source duplicate detection (fork).

The same opening is often discovered on several boards and on the employer's own
career site. Rows are never deleted or merged: a duplicate keeps its own URL and
source history and gets ``duplicate_of`` = the canonical row's URL plus a reason.

A pair is a duplicate only on strong evidence (conservative -- similar titles at one
employer are often genuinely different openings):

1. the same canonical *job-specific* URL -- posting or application URL, in either field
   (tracking parameters, ``www.``, ATS host aliases and ``/apply`` suffixes removed; path
   case and non-default ports kept; generic careers/search pages never count); or
2. the same normalised employer and the same requisition ID; or
3. the same normalised employer, title, location and employment type, **and** both
   descriptions present and near-identical (word-shingle Jaccard >= 0.85).

Different requisition IDs always mean different openings, even with a shared URL, and a
group never joins two requisitions of one employer (such a merge is flagged for review).
A row linked to a group with application history keeps the link. The canonical row is an
applied/claimed one, else the employer's own posting, else the earliest seen.
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
# Requisition IDs embedded in ATS URLs: Workday ".../Title_JREQ205631-1", ".../Title_R0012345",
# iCIMS "/jobs/12345/...", Greenhouse "/jobs/4012345", Lever UUIDs. URL patterns are tried first.
_URL_REQ_PATTERNS = (
    re.compile(r"_([A-Z]{0,6}-?\d{4,12}(?:-\d{1,3})?)/?$", re.IGNORECASE),
    re.compile(r"/jobs?/(\d{5,12})(?:[/?-]|$)", re.IGNORECASE),
    re.compile(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|$)", re.IGNORECASE),
)
_REQ_PATTERNS = (
    re.compile(r"\b(?:req(?:uisition)?|job)\s*(?:id|#|no\.?|number)?\s*[:#]?\s*([A-Z]{0,4}[-_]?\d{4,10})\b", re.IGNORECASE),
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
    """Comparable form of a URL: tracking parameters, ``www.``, ATS host aliases, locale prefixes
    and ``/apply`` suffixes removed. Path case and non-default ports are kept (audit R9: paths
    are case-sensitive on many sites, and another port is another service)."""
    if not url:
        return ""
    try:
        p = urlsplit(url.strip())
        port = p.port
    except ValueError:
        return url.strip()
    host = (p.hostname or "").lower()
    host = host.removeprefix("www.")
    host = _HOST_ALIASES.get(host, host)
    if port and port not in (80, 443):
        host = f"{host}:{port}"
    path = re.sub(r"/+", "/", p.path).rstrip("/")
    path = re.sub(r"/(apply|application|applynow)$", "", path, flags=re.IGNORECASE)
    if "myworkdayjobs.com" in host:
        path = re.sub(r"^/[a-z]{2}-[A-Z]{2}(?=/)", "", path)  # locale prefix
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
                             if not _TRACKING.match(k)))
    return urlunsplit(("https", host, path, query, ""))


# Query keys that identify one posting; without one of these (or an id-like path segment) a URL
# is a generic careers/search page and says nothing about which opening a row is.
_JOB_QUERY_KEYS = re.compile(r"^(jk|vjk|id|jobid|job_id|jid|gh_jid|reqid|req_id|requisitionid|req|job|"
                             r"jobreq|posting|postingid|opening|pid|currentjobid)$", re.IGNORECASE)


def is_generic_url(url: str | None) -> bool:
    """True for URLs that do not identify a single posting (careers home, search pages)."""
    if not url:
        return True
    try:
        p = urlsplit(url)
    except ValueError:
        return True
    if any(_JOB_QUERY_KEYS.match(k) and v for k, v in parse_qsl(p.query)):
        return False
    for seg in (s for s in p.path.split("/") if s):
        if re.search(r"\d", seg) or (len(seg) >= 25 and seg.count("-") >= 2):
            return False
    return True


def _url_identities(job: dict) -> set[str]:
    """Job-specific URLs of a row, posting and application URLs alike (cross-field aliases)."""
    out = set()
    for u in (job.get("application_url"), job.get("url")):
        if u and not is_generic_url(u):
            out.add(canonical_url(u))
    return out


def requisition_id(job: dict) -> str | None:
    if job.get("requisition_id"):
        return str(job["requisition_id"]).upper().replace("_", "-")
    for url in (job.get("application_url") or "", job.get("url") or ""):
        host = (urlsplit(url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in _BOARD_HOSTS):
            continue  # a board's own listing id is not the employer's requisition
        path = urlsplit(url).path
        for pat in _URL_REQ_PATTERNS:
            m = pat.search(path)
            if m:
                return m.group(1).upper().replace("_", "-")
    text = (job.get("full_description") or "")[:4000]
    for pat in _REQ_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).upper().replace("_", "-")
    return None


def _source(job: dict) -> str:
    return f"{job.get('strategy') or ''}|{(job.get('site') or '').lower()}"


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
    """Reason string if ``a`` and ``b`` are the same opening, else None.

    Positive requisition identity wins (audit R9): two known, different requisition IDs are two
    openings even if both rows share a URL (e.g. a generic careers page). URL equality counts
    only for job-specific URLs, and a row's posting URL may match another row's application URL."""
    ra, rb = requisition_id(a), requisition_id(b)
    if ra and rb and ra != rb:
        return None
    shared = _url_identities(a) & _url_identities(b)
    if shared:
        ca, cb = canonical_url(a.get("application_url")), canonical_url(b.get("application_url"))
        if ca and ca == cb and ca in shared:
            return "same application URL"
        cu = canonical_url(a.get("url"))
        if cu and cu == canonical_url(b.get("url")) and cu in shared:
            return "same posting URL"
        return "same posting/application URL"
    comp_a, comp_b = company_of(a), company_of(b)
    if not comp_a or comp_a != comp_b:
        return None
    if ra and rb:
        return "same employer and requisition ID"
    # Similar-text matching is a *cross-source* rule: one source listing the same title twice with
    # different URLs usually means several openings (real data: one employer's Workday site had
    # identical titles with different requisitions and cities).
    if _source(a) == _source(b):
        return None
    if norm_title(a.get("title")) != norm_title(b.get("title")):
        return None
    la, lb = norm_location(a.get("location")), norm_location(b.get("location"))
    if not la or la != lb:
        return None

    ta, tb = (a.get("job_type") or "").lower(), (b.get("job_type") or "").lower()
    if ta and tb and ta != tb:
        return None
    sim = similarity(a.get("full_description") or a.get("description"),
                     b.get("full_description") or b.get("description"))
    if sim >= SIMILARITY:
        return f"same employer/title/location, descriptions {sim:.0%} similar"
    return None


def _is_board(url: str | None) -> bool:
    host = (urlsplit(url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _BOARD_HOSTS)


def _rank(job: dict) -> tuple:
    """Canonical row: one with application history, then the employer's own posting, then an
    official application URL, then the earliest seen (official employer postings win over copies)."""
    claimed = job.get("apply_status") in ("applied", "in_progress", "needs_review", "manual")
    own_official = bool(job.get("url")) and not _is_board(job.get("url"))
    app_official = not _is_board(job.get("application_url") or job.get("url"))
    return (0 if claimed else 1, 0 if own_official else 1, 0 if app_official else 1,
            job.get("first_seen_at") or job.get("discovered_at") or "", job.get("url"))


_COLS = ("url, title, company, site, strategy, location, job_type, application_url, full_description, description,"
         " requisition_id, apply_status, first_seen_at, discovered_at, duplicate_of")


SIMILARITY_BUCKET_MAX = 400  # pairwise text comparison per company bucket (exact identities are unbounded)
_HISTORY = ("applied", "in_progress", "needs_review", "manual")


def run_dedupe(conn: sqlite3.Connection | None = None) -> dict:
    """Group duplicates and record ``dedupe_key`` / ``duplicate_of``. Idempotent.

    Audit R9: exact identities (job-specific URLs across posting/application fields, and
    employer + requisition) are indexed without a size cutoff; only the pairwise text rule is
    bounded, and skipped comparisons are reported. A merge that would put two different
    requisitions of one employer into a group is refused and flagged for review. A row linked to
    a group with application history keeps that link even if its evidence changed, so regrouping
    can never make an applied opening claimable again."""
    conn = conn or get_connection()
    rows = [dict(r) for r in conn.execute(f"SELECT {_COLS} FROM jobs").fetchall()]
    index = {j["url"]: i for i, j in enumerate(rows)}
    parent = list(range(len(rows)))
    reason: dict[int, str] = {}
    reqs: dict[int, set[str]] = {i: ({f"{company_of(j)}|{requisition_id(j)}"} if requisition_id(j) else set())
                                 for i, j in enumerate(rows)}
    review: dict[int, str] = {}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def conflict(ra: int, rb: int) -> bool:
        by_company: dict[str, set[str]] = {}
        for key in reqs[ra] | reqs[rb]:
            comp, _, req = key.partition("|")
            by_company.setdefault(comp, set()).add(req)
        return any(len(v) > 1 for v in by_company.values())

    def union(a: int, b: int, why: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if conflict(ra, rb):
            # The group would contain two different requisitions of one employer: not merged.
            review[a] = review[b] = f"possible duplicate (review): {why}, but requisition IDs differ"
            return
        parent[rb] = ra
        reqs[ra] |= reqs.pop(rb)
        reason[b] = reason.get(b) or why
        reason[a] = reason.get(a) or why

    # 1. Exact identities: index once, compare each bucket against its first member (no cutoff).
    exact: dict[str, list[int]] = {}
    for i, j in enumerate(rows):
        for u in _url_identities(j):
            exact.setdefault("x:" + u, []).append(i)
        rid = requisition_id(j)
        if rid and company_of(j):
            exact.setdefault(f"r:{company_of(j)}|{rid}", []).append(i)
    for members in exact.values():
        for b in members[1:]:
            why = same_opening(rows[members[0]], rows[b])
            if why:
                union(members[0], b, why)
    # 2. Similar text (cross-source, same employer/title/location), bounded per company bucket.
    by_company: dict[str, list[int]] = {}
    for i, j in enumerate(rows):
        if company_of(j):
            by_company.setdefault(company_of(j), []).append(i)
    unchecked = 0
    for members in by_company.values():
        if len(members) > SIMILARITY_BUCKET_MAX:
            unchecked += len(members)
            continue
        for ai in range(len(members)):
            for bi in range(ai + 1, len(members)):
                a, b = members[ai], members[bi]
                if find(a) == find(b):
                    continue
                why = same_opening(rows[a], rows[b])
                if why:
                    union(a, b, why)
    if unchecked:
        log.warning("dedupe: %d rows in very large employer buckets were not text-compared", unchecked)

    groups: dict[int, list[int]] = {}
    for i in range(len(rows)):
        groups.setdefault(find(i), []).append(i)
    dup = kept = 0
    for members in groups.values():
        canonical = min(members, key=lambda i: _rank(rows[i]))
        for i in members:
            j = rows[i]
            of = None if i == canonical else rows[canonical]["url"]
            why = None if of is None else reason.get(i, "duplicate group")
            prev = j.get("duplicate_of")
            if of is None and prev and prev in index and prev != j["url"]:
                old_root = rows[index[prev]]
                old_group = [r for r in rows if r.get("duplicate_of") == prev or r["url"] == prev]
                if any(r.get("apply_status") in _HISTORY for r in old_group) or old_root.get("apply_status") in _HISTORY:
                    of, why = prev, "kept: earlier group has application history"
                    kept += 1
            if of is None and i in review:
                why = review[i]
            dup += of is not None
            conn.execute("UPDATE jobs SET dedupe_key = ?, duplicate_of = ?, duplicate_reason = ? WHERE url = ?",
                         (dedupe_key(j), of, why, j["url"]))
    conn.commit()
    log.info("dedupe: %d rows, %d duplicates (%d kept for history, %d flagged for review)", len(rows), dup, kept,
             len(review))
    return {"status": "ok", "rows": len(rows), "duplicates": dup, "review": len(review), "unchecked": unchecked}



def group_urls(conn: sqlite3.Connection, url: str) -> list[str]:
    """Every URL in ``url``'s duplicate group (including itself)."""
    row = conn.execute("SELECT COALESCE(duplicate_of, url) AS root FROM jobs WHERE url = ?", (url,)).fetchone()
    if not row:
        return [url]
    root = row["root"] if isinstance(row, sqlite3.Row) else row[0]
    return [r[0] for r in conn.execute("SELECT url FROM jobs WHERE url = ? OR duplicate_of = ?", (root, root))]
