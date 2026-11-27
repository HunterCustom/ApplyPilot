"""Deterministic geographic eligibility classification.

Independent of the LLM fit score: decides whether the candidate may apply to a
job based on where the job is and who it is open to. Pure functions of the job
row plus configuration, so a classification is repeatable for a given
CLASSIFIER_VERSION.

Statuses
    eligible_nc         onsite/hybrid/any job in an accepted North Carolina commute tier
    eligible_us_remote  remote, open nationwide in the US or explicitly including NC
    restricted_remote   remote limited to a state list that may or may not include NC -> review
    ineligible          out-of-state onsite, outside the US, or explicitly excludes NC
    unknown             missing, ambiguous or conflicting evidence -> review
    eligible_manual / ineligible_manual   human decisions; never overwritten by reclassification

Precedence (first match wins): explicit NC exclusion > explicit residency list
without NC > accepted NC location > remote (nationwide / restricted) >
out-of-state onsite > foreign > unknown. Remote wording alone never makes a job
eligible: nationwide US remote needs positive US evidence and no conflicting
onsite statement.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

CLASSIFIER_VERSION = "1.0.0"
MANUAL_VERSION = "manual"

ELIGIBLE_STATUSES = ("eligible_nc", "eligible_us_remote", "eligible_manual")
REVIEW_STATUSES = ("restricted_remote", "unknown")
INELIGIBLE_STATUSES = ("ineligible", "ineligible_manual")
ALL_STATUSES = ELIGIBLE_STATUSES[:2] + REVIEW_STATUSES + ("ineligible",)

SQL_ELIGIBLE = "eligibility_status IN ('eligible_nc', 'eligible_us_remote', 'eligible_manual')"
SQL_NOT_INELIGIBLE = "COALESCE(eligibility_status, '') NOT IN ('ineligible', 'ineligible_manual')"

US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california",
    "CO": "colorado", "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia",
    "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa",
    "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada", "NH": "new hampshire",
    "NJ": "new jersey", "NM": "new mexico", "NY": "new york", "NC": "north carolina",
    "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode island", "SC": "south carolina", "SD": "south dakota", "TN": "tennessee",
    "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington",
    "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}
# Longest names first so "west virginia" wins over "virginia", "new mexico" over "mexico".
_STATE_NAMES = sorted(((n, c) for c, n in US_STATES.items()), key=lambda x: -len(x[0]))
_DC_RE = re.compile(r"\bwashington,?\s*d\.?\s*c\.?\b|\bdistrict of columbia\b")

FOREIGN_NAMES = (
    "canada", "mexico", "united kingdom", "england", "scotland", "wales", "ireland", "india",
    "philippines", "vietnam", "thailand", "australia", "new zealand", "germany", "france",
    "spain", "poland", "netherlands", "sweden", "switzerland", "finland", "japan", "china",
    "singapore", "brazil", "argentina", "colombia", "costa rica", "kenya", "south africa",
    "israel", "united arab emirates", "uae", "sri lanka", "romania", "portugal", "italy",
    "belgium", "denmark", "norway", "austria", "czech republic", "czechia", "hungary",
    "ukraine", "egypt", "nigeria", "pakistan", "malaysia", "indonesia", "south korea", "korea",
    "taiwan", "hong kong", "chile", "peru", "uk", "emea", "apac", "latam", "europe",
    "ontario", "quebec", "british columbia", "alberta", "manitoba", "nova scotia",
    "saskatchewan", "new south wales", "victoria australia", "teletravail",
)
# Cities only count as foreign when no US state is present (London, KY; Dublin, OH exist).
FOREIGN_CITIES = (
    "toronto", "montreal", "vancouver", "calgary", "ottawa", "laval", "bengaluru", "bangalore",
    "hyderabad", "chennai", "pune", "mumbai", "delhi", "noida", "gurgaon", "gurugram", "manila",
    "london", "dublin", "warsaw", "gdansk", "krakow", "madrid", "barcelona", "paris", "berlin",
    "munich", "zug", "zurich", "tokyo", "beijing", "shanghai", "sydney", "melbourne", "pyrmont",
    "gothenburg", "stockholm", "espoo", "colombo", "nairobi", "dubai", "oxford", "langenfeld",
)

_REMOTE_WORD = re.compile(r"\b(remote|work from home|wfh|telecommut\w*|virtual|anywhere)\b")
_HYBRID_WORD = re.compile(r"\bhybrid\b")
_ONSITE_WORD = re.compile(r"\b(on-?site|in[- ]office|in person)\b")
_US_MARKER = re.compile(r"\b(usa|u\.s\.a\.?|u\.s\.|united states(?: of america)?|us|nationwide)\b")
_TRUNCATED = re.compile(r"\bmore\.{0,3}\s*$|\b\d+\s+locations?\b", re.IGNORECASE)

# ---- description patterns -------------------------------------------------
_NATIONWIDE_RE = re.compile(
    r"\b(?:remote|work from home|telecommut\w*|work remotely)\b[^.\n]{0,60}?"
    r"\b(?:anywhere in|across|throughout|within|from anywhere in|based in|located in)\s+the\s+"
    r"(?:u\.?s\.?a?\.?|united states)\b"
    r"|\banywhere in the (?:u\.?s\.?a?\.?|united states)\b"
    r"|\bremote\b[^.\n]{0,40}\bnationwide\b|\bnationwide\b[^.\n]{0,40}\bremote\b"
    r"|\bremote[- ](?:us|usa|united states)\b|\b(?:us|usa)[- ]remote\b",
    re.IGNORECASE,
)
_ONSITE_STATEMENT_RE = re.compile(
    r"\bthis (?:is an?|role is|position is|job is)(?: an?)? (?:fully )?(?:on-?site|in-office|in office)\b"
    r"|\bnot (?:a )?remote\b|\bno remote\b|\bremote work is not\b|\bnot eligible for remote\b"
    r"|\b(?:fully|100%) on-?site\b|\b(?:on-?site|in[- ]office) (?:five|5) days\b",
    re.IGNORECASE,
)
_ALLOW_RES = [re.compile(p, re.IGNORECASE) for p in (
    (r"\bmust (?:currently )?(?:reside|live|be located|be based|be a resident)\s+(?:in|within|near)\s+"
     r"(?:the (?:state|states) of\s+)?(?P<tail>[^.\n;]{2,160})"),
    (r"\brequires? (?:the )?(?:candidate|applicant)s? to (?:reside|live|be located)\s+(?:in|within|near)\s+"
     r"(?P<tail>[^.\n;]{2,160})"),
    (r"\b(?:eligible|approved|permitted|hiring) (?:states|locations)(?: for remote work)?\W{0,6}"
     r"(?:are|include)?\W{0,4}(?P<tail>[^\n]{2,300})"),
    r"\bremote work in (?:one of )?the following (?:states|locations)\W{0,4}(?P<tail>[^\n]{2,400})",
    (r"\b(?:we (?:are|can) (?:only )?(?:able to )?hire|hiring is limited to|"
     r"open (?:only )?to (?:candidates|applicants|residents))\s+(?:who (?:reside|live) )?"
     r"(?:in|from|located in|residing in)\s+(?:the following states\W{0,4})?(?P<tail>[^.\n]{2,300})"),
    (r"\bcandidates (?:must be|should be|are required to be) (?:located|based|residing) in\s+"
     r"(?P<tail>[^.\n;]{2,160})"),
)]
_EXCLUDE_RES = [re.compile(p, re.IGNORECASE) for p in (
    (r"\b(?:not|unable to|cannot|can't|can not)\s+(?:able to\s+)?(?:hire|employ|consider|accept applications from)\s+"
     r"(?:candidates|applicants|employees|individuals|residents|anyone)?\s*(?:who (?:are )?(?:currently )?)?"
     r"(?:living|located|residing|based|reside|live)?\s*(?:in|from)\s+(?P<tail>[^.\n]{2,200})"),
    (r"\bnot (?:open|available|eligible) (?:to|for) (?:candidates|applicants|residents|employees)\s+"
     r"(?:located |residing |based |who reside )?(?:in|of|from)\s+(?P<tail>[^.\n]{2,200})"),
    (r"\b(?:excluding|except(?: for)?|with the exception of)\s+(?:the (?:states? of\s+)?|residents of\s+)?"
     r"(?P<tail>[^.\n)]{2,120})"),
)]
_EXCLUDE_CONTEXT = re.compile(r"remote|resid|locat|states?\b|hire|hiring|based", re.IGNORECASE)
_LOCATION_LINE = re.compile(
    r"(?im)^\s*\**\s*(?:job\s+|work\s+|primary\s+)?locations?\s*\**\s*[:\-]\s*\**\s*(?P<loc>[^\n]{3,100})$"
)
_WORKDAY_SLUG = re.compile(r"myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[^/]+/job/(?P<slug>[^/]+)/")
_TITLE_LOC = re.compile(r"\((?P<loc>[^)]{3,80})\)|\s[-–|]\s(?P<loc2>[^-–|]{3,60})$")


@dataclass
class Place:
    raw: str
    source: str
    states: set[str] = field(default_factory=set)
    foreign: set[str] = field(default_factory=set)
    us: bool = False
    remote: bool = False
    hybrid: bool = False
    onsite: bool = False
    truncated: bool = False

    @property
    def in_nc(self) -> bool:
        return "NC" in self.states


@dataclass
class Eligibility:
    status: str
    reason: str
    evidence: list[str]
    tier: str | None = None
    version: str = CLASSIFIER_VERSION

    def evidence_json(self) -> str:
        return json.dumps(self.evidence, ensure_ascii=False)


@dataclass
class EligibilityConfig:
    home_state: str = "NC"
    tiers: dict[str, list[str]] = field(default_factory=dict)  # tier -> city names
    other_nc: str = "review"  # review | eligible | ineligible
    aliases: dict[str, str] = field(default_factory=lambda: {
        "rtp": "Durham", "research triangle park": "Durham",
    })


def _norm(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9%]+", " ", text.lower()).strip() + " "


def _has_word(norm_text: str, phrase: str) -> bool:
    return f" {phrase} " in norm_text


def states_in(text: str, allow_codes: bool = True) -> set[str]:
    """US states named in text: full names always; two-letter codes only as uppercase tokens."""
    found: set[str] = set()
    lowered = text.lower()
    if _DC_RE.search(lowered):
        found.add("DC")
        lowered = _DC_RE.sub(" ", lowered)
    norm = _norm(lowered)
    for name, code in _STATE_NAMES:
        if _has_word(norm, name):
            found.add(code)
            norm = norm.replace(f" {name} ", " ")
    if allow_codes:
        found |= {t for t in re.split(r"[\s,/|()\-.;:]+", text) if t in US_STATES}
    return found


def _foreign_in(text: str, has_us_state: bool) -> set[str]:
    norm = _norm(text)
    for name, _ in _STATE_NAMES:  # "new mexico" must not read as "mexico"
        norm = norm.replace(f" {name} ", " ")
    found = {n for n in FOREIGN_NAMES if _has_word(norm, n)}
    if not has_us_state:
        found |= {c for c in FOREIGN_CITIES if _has_word(norm, c)}
    if re.search(r"(?:^|[\s,])CAN(?:$|[\s,])", text):
        found.add("canada")
    return found


def analyze_place(raw: str, source: str) -> Place:
    text = raw.strip()
    truncated = bool(_TRUNCATED.search(text))
    text = _TRUNCATED.sub("", text).strip(" ,")
    named_states = states_in(text, allow_codes=False)
    states = states_in(text, allow_codes=True)
    foreign = _foreign_in(text, bool(states))
    if foreign and not named_states:
        states = set()  # bare codes beside a foreign name ("Toronto, ON, CA") are not US states
    low = text.lower()
    return Place(
        raw=raw.strip(), source=source, states=states, foreign=foreign,
        us=bool(_US_MARKER.search(low)), remote=bool(_REMOTE_WORD.search(low)),
        hybrid=bool(_HYBRID_WORD.search(low)), onsite=bool(_ONSITE_WORD.search(low)),
        truncated=truncated,
    )


def _split_locations(location: str) -> list[str]:
    parts = re.split(r"\s*(?:;|\||\n|\s+or\s+|\s+and\s+(?=remote\b))\s*", location, flags=re.IGNORECASE)
    return [p for p in (s.strip() for s in parts) if p]


def gather_places(job: dict) -> list[Place]:
    """Structured places, in decreasing reliability: location field, title, Workday URL, 'Location:' line."""
    places: list[Place] = []
    location = (job.get("location") or "").strip()
    for seg in _split_locations(location):
        places.append(analyze_place(seg, "location"))
    title = job.get("title") or ""
    for m in _TITLE_LOC.finditer(title):
        loc = m.group("loc") or m.group("loc2") or ""
        if states_in(loc) or _REMOTE_WORD.search(loc.lower()):
            for seg in _split_locations(loc):
                places.append(analyze_place(seg, "title"))
    if not location:
        for url in (job.get("url"), job.get("application_url")):
            m = _WORKDAY_SLUG.search(url or "")
            if m:
                slug = m.group("slug").replace("---", " , ").replace("-", " ")
                places.append(analyze_place(slug, "workday_url"))
                break
        desc = job.get("full_description") or ""
        m = _LOCATION_LINE.search(desc[:6000])
        if m and (states_in(m.group("loc")) or _REMOTE_WORD.search(m.group("loc").lower())
                  or _foreign_in(m.group("loc"), False)):
            for seg in _split_locations(m.group("loc")):
                places.append(analyze_place(seg, "description_location_line"))
    return [p for p in places if p.states or p.foreign or p.us or p.remote or p.hybrid or p.onsite]


@dataclass
class DescriptionSignals:
    allowed_states: set[str] = field(default_factory=set)
    excluded_states: set[str] = field(default_factory=set)
    nationwide: bool = False
    onsite_statement: bool = False
    us_required: bool = False
    evidence: list[str] = field(default_factory=list)


def _snippet(text: str, start: int, end: int) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - 20):min(len(text), end + 20)]).strip()[:160]


def scan_description(desc: str) -> DescriptionSignals:
    sig = DescriptionSignals()
    if not desc:
        return sig
    for rx in _ALLOW_RES:
        for m in rx.finditer(desc):
            tail = m.group("tail")
            tail = re.split(r"\bor (?:be )?willing\b|\bor relocate\b", tail, flags=re.IGNORECASE)[0]
            found = states_in(tail)
            if found:
                sig.allowed_states |= found
                sig.evidence.append(f"description restriction: '{_snippet(desc, m.start(), m.end())}'")
            elif re.search(r"\b(u\.?s\.?a?|united states)\b", tail, re.IGNORECASE):
                sig.us_required = True
    for rx in _EXCLUDE_RES:
        for m in rx.finditer(desc):
            context = desc[max(0, m.start() - 120):m.end()]
            if rx is _EXCLUDE_RES[2] and not _EXCLUDE_CONTEXT.search(context):
                continue
            found = states_in(m.group("tail"))
            if found:
                sig.excluded_states |= found
                sig.evidence.append(f"description exclusion: '{_snippet(desc, m.start(), m.end())}'")
    m = _NATIONWIDE_RE.search(desc)
    if m:
        sig.nationwide = True
        sig.evidence.append(f"description nationwide remote: '{_snippet(desc, m.start(), m.end())}'")
    m = _ONSITE_STATEMENT_RE.search(desc)
    if m:
        sig.onsite_statement = True
        sig.evidence.append(f"description onsite statement: '{_snippet(desc, m.start(), m.end())}'")
    return sig


def _nc_tier(place: Place, cfg: EligibilityConfig) -> str | None:
    norm = _norm(place.raw.replace("-", " "))
    for alias, city in cfg.aliases.items():
        if _has_word(norm, alias):
            norm += f"{city.lower()} "
    for tier in ("highest", "high", "acceptable"):
        for city in cfg.tiers.get(tier, []):
            if _has_word(norm, _norm(city).strip()):
                return tier
    return None


def _place_desc(p: Place) -> str:
    bits = []
    if p.states:
        bits.append("/".join(sorted(p.states)))
    if p.us:
        bits.append("US")
    if p.foreign:
        bits.append("foreign:" + "/".join(sorted(p.foreign)))
    if p.remote:
        bits.append("remote")
    if p.hybrid:
        bits.append("hybrid")
    if p.truncated:
        bits.append("list truncated")
    return f"{p.source}: '{p.raw[:80]}' -> {', '.join(bits) or 'no geography'}"


def classify(job: dict, cfg: EligibilityConfig | None = None) -> Eligibility:
    """Classify one job row (dict with title, location, full_description, url, application_url)."""
    cfg = cfg or EligibilityConfig()
    home = cfg.home_state
    places = gather_places(job)
    sig = scan_description(job.get("full_description") or "")
    evidence = [_place_desc(p) for p in places][:4] + sig.evidence[:3]

    def result(status, reason, tier=None):
        return Eligibility(status, reason, evidence[:7], tier)

    home_name = US_STATES[home].title()
    # 1. Explicit exclusions override everything else.
    if home in sig.excluded_states:
        return result("ineligible", f"description explicitly excludes {home_name}")
    # 2. Explicit residency/state requirement that does not include the home state.
    if sig.allowed_states and home not in sig.allowed_states:
        states = ", ".join(sorted(sig.allowed_states))
        return result("ineligible", f"requires residence/location in {states}; {home_name} not included")
    home_permitted = home in sig.allowed_states

    home_places = [p for p in places if home in p.states]
    remote_places = [p for p in places if p.remote and home not in p.states]
    other_us = [p for p in places if p.states and home not in p.states and not p.remote]
    foreign = [p for p in places if p.foreign and not p.states and not p.us]

    # 3. Accepted home-state location.
    for p in home_places:
        tier = _nc_tier(p, cfg)
        if tier:
            return result("eligible_nc", f"{home_name} location in '{tier}' commute tier", tier)
    if any(p.remote for p in home_places):
        return result("eligible_us_remote", f"remote role located in {home_name}", "high")

    # 4. Remote roles.
    us_evidence = sig.nationwide or sig.us_required
    nationwide = [p for p in remote_places if not p.states and not p.foreign and (p.us or us_evidence)]
    restricted = [p for p in remote_places if p.states]
    if nationwide or (remote_places and home_permitted):
        if sig.onsite_statement and not any(p.hybrid for p in places):
            return result("unknown", "listed as remote but the description says onsite (conflict)")
        why = f"remote with {home_name} explicitly permitted" if home_permitted else "nationwide US remote"
        return result("eligible_us_remote", why, "high")
    if restricted:
        states = ", ".join(sorted(set().union(*(p.states for p in restricted))))
        if any(p.truncated for p in restricted):
            return result("restricted_remote",
                          f"remote limited to listed states ({states}, list truncated); "
                          f"{home_name} not confirmed")
        if not home_places and not sig.nationwide:
            return result("ineligible", f"remote limited to {states}; {home_name} not included")
    if remote_places and not nationwide:
        if all(p.foreign for p in remote_places) and not any(p.us for p in remote_places):
            return result("ineligible", "remote role based outside the US")
        if not home_places:
            return result("unknown", "remote role without country or state evidence")

    # 5. Home-state location outside the configured commute tiers.
    if home_places:
        if cfg.other_nc == "eligible":
            return result("eligible_nc", f"{home_name} location outside commute tiers (allowed by config)",
                          "other_nc")
        if cfg.other_nc == "ineligible":
            return result("ineligible", f"{home_name} location outside commute tiers")
        return result("unknown", f"{home_name} location outside commute tiers: review commute")

    # 6. Out-of-state onsite/hybrid, unless the description offers nationwide remote.
    if other_us:
        if sig.nationwide and not sig.onsite_statement:
            return result("eligible_us_remote", "out-of-state office but description offers nationwide remote",
                          "high")
        states = ", ".join(sorted(set().union(*(p.states for p in other_us))))
        return result("ineligible", f"onsite/hybrid outside {home_name} ({states})")

    # 7. Outside the US.
    if foreign:
        return result("ineligible", "located outside the US")

    # 8. US-only marker without a state, or nothing at all.
    if sig.nationwide and not sig.onsite_statement:
        return result("eligible_us_remote", "description offers nationwide US remote", "high")
    if any(p.us for p in places):
        return result("unknown", "US location without state or remote evidence")
    return result("unknown", "no usable location information")


# ---- configuration --------------------------------------------------------

def load_eligibility_config(search_cfg: dict | None = None, profile: dict | None = None) -> EligibilityConfig:
    """Build config from searches.yaml `eligibility:` (preferred) or legacy `location_accept`."""
    if search_cfg is None:
        from applypilot.config import load_search_config
        try:
            search_cfg = load_search_config() or {}
        except (OSError, ValueError):
            search_cfg = {}
    cfg = EligibilityConfig()
    if profile and profile.get("personal", {}).get("province_state"):
        st = str(profile["personal"]["province_state"]).strip()
        code = st.upper() if st.upper() in US_STATES else next(
            (c for c, n in US_STATES.items() if n == st.lower()), None)
        if code:
            cfg.home_state = code
    elig = search_cfg.get("eligibility") or {}
    if elig.get("home_state"):
        cfg.home_state = str(elig["home_state"]).upper()
    tiers = elig.get("commute_tiers") or {}
    if tiers:
        cfg.tiers = {t: [str(c) for c in (tiers.get(t) or [])] for t in ("highest", "high", "acceptable")}
    else:
        # Backward compatibility: legacy location_accept entries become the 'acceptable' tier.
        legacy = [re.sub(r",\s*[A-Za-z]{2}$", "", str(x)).strip(" ,")
                  for x in (search_cfg.get("location_accept") or [])]
        cfg.tiers = {"acceptable": [c for c in legacy if c and c.lower() not in US_STATES.values()
                                    and len(c) > 2]}
    if elig.get("other_nc") in ("review", "eligible", "ineligible"):
        cfg.other_nc = elig["other_nc"]
    for alias, city in (elig.get("aliases") or {}).items():
        cfg.aliases[str(alias).lower()] = str(city)
    return cfg


# ---- persistence ----------------------------------------------------------

def classify_jobs(conn=None, cfg: EligibilityConfig | None = None, force: bool = False) -> dict:
    """Classify enriched jobs that are unclassified or classified by an older version.

    Human decisions (version 'manual') are never overwritten. Never deletes rows.
    Returns {status: count} for the rows (re)classified in this call.
    """
    from applypilot.database import get_connection
    conn = conn or get_connection()
    cfg = cfg or load_eligibility_config()
    where = "(full_description IS NOT NULL OR detail_scraped_at IS NOT NULL)"
    where += " AND COALESCE(eligibility_version, '') != ?"
    params: list = [MANUAL_VERSION]
    if not force:
        where += " AND COALESCE(eligibility_version, '') != ?"
        params.append(CLASSIFIER_VERSION)
    rows = conn.execute(
        f"SELECT url, title, location, full_description, application_url FROM jobs WHERE {where}", params
    ).fetchall()
    now = datetime.now(UTC).isoformat()
    counts: dict[str, int] = {}
    for row in rows:
        e = classify(dict(row), cfg)
        conn.execute("""
            UPDATE jobs SET eligibility_status = ?, eligibility_reason = ?, eligibility_evidence = ?,
                           eligibility_tier = ?, eligibility_version = ?, eligibility_at = ?
            WHERE url = ?
        """, (e.status, e.reason, e.evidence_json(), e.tier, e.version, now, row["url"]))
        counts[e.status] = counts.get(e.status, 0) + 1
    conn.commit()
    return counts


def set_manual_decision(url: str, approve: bool, note: str = "", conn=None) -> bool:
    """Record a human eligibility decision that reclassification will not overwrite."""
    from applypilot.database import get_connection
    conn = conn or get_connection()
    status = "eligible_manual" if approve else "ineligible_manual"
    cur = conn.execute("""
        UPDATE jobs SET eligibility_status = ?, eligibility_reason = ?, eligibility_version = ?,
                       eligibility_at = ?
        WHERE url = ?
    """, (status, f"manual decision{': ' + note if note else ''}", MANUAL_VERSION,
          datetime.now(UTC).isoformat(), url))
    conn.commit()
    return cur.rowcount == 1
