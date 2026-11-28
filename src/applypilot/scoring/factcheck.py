"""Deterministic grounding checks for generated documents (fork).

The LLM judge catches subtle problems, but it can be wrong in either direction. These
checks are mechanical and fail closed: every concrete claim in a tailored resume or
cover letter must be traceable to a *source of truth* --

* the candidate's base resume,
* ``profile.json`` (``skills_boundary``, ``resume_facts``, work authorisation, ...),
* user-verified ``candidate_evidence.yaml`` (professional experience, certifications,
  personal projects -- kept apart by category).

Checked claims: technologies/tools on a curated IT vocabulary, upper-case acronyms,
numbers that carry meaning (percentages, money, counts of users/devices/tickets, years
of experience), certifications (earned vs. in progress), employment dates and
employers, and personal projects presented as employment. A claim that cannot be
found in the sources is reported; any report rejects the document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Common IT support / infrastructure vocabulary. A term from this list in a generated document
# must also occur in a source document (case-insensitive, word-bounded).
TECH_TERMS = (
    "active directory", "azure ad", "entra id", "entra", "intune", "sccm", "mecm", "jamf", "autopilot",
    "office 365", "microsoft 365", "m365", "exchange online", "exchange", "sharepoint", "onedrive", "microsoft teams",
    "windows server", "windows 10", "windows 11", "macos", "linux", "ubuntu", "red hat", "rhel", "centos",
    "debian", "vmware", "vsphere", "esxi", "hyper-v", "proxmox", "citrix", "azure", "aws", "gcp",
    "google workspace", "okta", "duo", "servicenow", "jira", "zendesk", "freshservice", "connectwise",
    "kaseya", "ninjaone", "datto", "splunk", "sentinel", "crowdstrike", "defender", "sophos", "fortinet",
    "fortigate", "palo alto", "cisco", "meraki", "ubiquiti", "unifi", "juniper", "aruba", "sonicwall",
    "powershell", "python", "bash", "javascript", "typescript", "java", "c#", "c++", "golang", "rust",
    "ruby", "php", "sql", "mysql", "postgresql", "sql server", "mongodb", "terraform", "ansible", "puppet",
    "chef", "docker", "kubernetes", "git", "github", "gitlab", "jenkins", "ci/cd", "veeam", "backup exec",
    "dhcp", "dns", "tcp/ip", "vlan", "vpn", "firewall", "load balancer", "nginx", "apache", "iis",
    "group policy", "gpo", "bitlocker", "mdm", "rmm", "itil", "itsm", "sla", "voip", "pbx", "printers",
    "networking", "wireshark", "nagios", "zabbix", "prometheus", "grafana", "elk", "siem", "edr", "mfa",
    "sso", "saml", "ldap", "kerberos", "pki", "ssl", "tls", "smb", "nfs", "san", "nas", "raid",
)
CERTIFICATIONS = (
    "a+", "network+", "security+", "server+", "cloud+", "linux+", "ccna", "ccnp", "ccie", "mcsa", "mcse",
    "az-900", "az-104", "az-500", "md-102", "ms-900", "ms-102", "sc-300", "aws certified", "cissp", "cism",
    "ceh", "itil foundation", "itil 4", "rhcsa", "rhce", "lpic", "jncia", "pmp", "comptia", "google it support",
    "cysa+", "pentest+", "casp+", "gsec", "oscp", "vcp",
)
# Acronyms that are ordinary English / resume structure, never claims on their own.
_ACRONYM_OK = {"I", "A", "IT", "US", "USA", "NC", "LLC", "INC", "CEO", "HR", "OK", "PM", "AM", "ID", "QA", "UI",
               "UX", "BS", "BA", "MS", "MBA", "PHD", "GPA", "EDUCATION", "EXPERIENCE", "SUMMARY", "PROJECTS",
               "TECHNICAL", "SKILLS", "RESUME", "N/A", "TBD", "FAQ", "PDF", "HTML", "URL", "ETC", "IE", "EG"}
_INPROGRESS_RE = re.compile(r"in[- ]progress|pursuing|studying|expected|scheduled|planned|working toward",
                            re.IGNORECASE)
_METRIC_RE = re.compile(
    r"(?<![\w.])(\$?\d[\d,]*(?:\.\d+)?)\s*(%|percent|\+?\s*(?:users|employees|endpoints|devices|servers|"
    r"workstations|tickets|sites|locations|clients|customers|machines|laptops|accounts|vms|hours|x\b))",
    re.IGNORECASE)
_YEARS_RE = re.compile(r"(\d+)\+?\s*(?:years|yrs)", re.IGNORECASE)
_DATE_RANGE_RE = re.compile(
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+)?(19|20)\d{2}\s*(?:-|to|–)\s*"
    r"(?:present|current|now|((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+)?(19|20)\d{2})",
    re.IGNORECASE)
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9+/&-]{1,7}\b")
# Degree / education claims (audit R1): each degree term and each named institution in a document
# must occur in the sources; an invented degree is never "rewording".
_DEGREE_RE = re.compile(
    r"\b(bachelor(?:'?s)?|master(?:'?s)?|associate(?:'?s)? degree|associate of|doctorate|ph\.?\s?d\.?|"
    r"b\.\s?s\.|b\.\s?a\.|m\.\s?s\.|m\.\s?b\.\s?a\.|bsc|msc|mba|aas|a\.a\.s\.|diploma|degree|graduated|"
    r"certificate program)\b", re.IGNORECASE)
_INSTITUTION_RE = re.compile(
    r"\b((?:[A-Z][a-zA-Z&.'-]+\s+){0,4}(?:University|College|Institute|Academy|Polytechnic)"
    r"(?:\s+of(?:\s+[A-Z][a-zA-Z&.'-]+){1,4})?)")
# Seniority / leadership claims: must be supported by the sources, not inferred from responsibility.
_SENIORITY_RE = re.compile(r"\b(senior|sr\.|lead|principal|staff|head of|chief|director|manager|supervisor|"
                           r"team lead|architect)\b", re.IGNORECASE)
_LEADERSHIP_RE = re.compile(
    r"\b(led|lead|leading|managed|managing|supervised|supervising|directed|mentored|mentoring|oversaw|"
    r"overseeing|headed)\b(?:\W+\w+){0,4}?\W+(team|teams|staff|technicians|engineers|employees|people|reports|"
    r"direct reports|crew|department)\b", re.IGNORECASE)
_CONTACT_RE = re.compile(r"@|https?://|www\.|\(\d{3}\)\s*\d{3}[-.\s]\d{4}|\b\d{3}[-.]\d{3}[-.]\d{4}\b")
_STOP = frozenset(
["a", "an", "and", "the", "of", "for", "to", "in", "on", "with", "by", "at", "from", "as", "or", "into", "across", "over", "under", "via", "per", "is", "are", "was", "were", "be", "been", "being", "this", "that", "these", "those", "their", "its", "our", "your", "my", "we", "i", "you", "he", "she", "they", "it", "all", "any", "each", "more", "most", "other", "such", "than", "then", "also", "using", "used", "use", "including", "include", "includes", "provide", "provided", "providing", "support", "supported", "supporting", "work", "worked", "working", "experience", "experienced"])
_SECTION_RE = re.compile(r"^(SUMMARY|TECHNICAL SKILLS|EXPERIENCE|PROJECTS|EDUCATION|CERTIFICATIONS)\s*$", re.MULTILINE)


@dataclass
class Sources:
    """Everything a document may legitimately draw on, normalised for lookup."""
    text: str                      # lower-cased concatenation of all sources
    raw: str                       # original casing (for acronym lookup)
    certs_earned: list[str] = field(default_factory=list)
    certs_in_progress: list[str] = field(default_factory=list)
    project_names: list[str] = field(default_factory=list)
    resume_text: str = ""

    def has(self, term: str) -> bool:
        t = term.lower().strip()
        if not t:
            return True
        return re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", self.text) is not None


def _flatten(obj) -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            out += _flatten(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            out += _flatten(v)
    elif obj is not None and not isinstance(obj, bool):
        out.append(str(obj))
    return out


def _sections(text: str) -> dict[str, str]:
    marks = [(m.group(1).upper(), m.start(), m.end()) for m in _SECTION_RE.finditer(text or "")]
    out: dict[str, str] = {}
    for i, (name, _s, e) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(text)
        out[name] = text[e:end]
    return out


def build_sources(resume_text: str, profile: dict | None, evidence: dict | None) -> Sources:
    profile = profile or {}
    evidence = evidence or {}
    parts = [resume_text or ""]
    for key in ("skills_boundary", "resume_facts", "experience", "work_authorization", "availability",
                "compensation", "eeo_voluntary"):
        parts += _flatten(profile.get(key))
    for cat in ("professional_experience", "personal_projects"):
        parts += [str(x) for x in evidence.get(cat, [])]
    earned, progress = [], []
    for c in evidence.get("certifications", []) or []:
        (progress if _INPROGRESS_RE.search(str(c)) else earned).append(str(c))
        parts.append(str(c))
    raw = "\n".join(parts)
    projects = []
    for line in _sections(resume_text or "").get("PROJECTS", "").splitlines():
        line = line.strip()
        if line and not line.startswith(("-", "*", "•")):
            projects.append(re.split(r"\s[|(]|\s-\s|,", line)[0].strip())
    for p in evidence.get("personal_projects", []) or []:
        projects.append(re.split(r"[:(\-,]", str(p))[0].strip())
    return Sources(text=raw.lower(), raw=raw, certs_earned=earned, certs_in_progress=progress,
                   project_names=[p for p in projects if len(p) >= 4], resume_text=resume_text or "")


def _near(text: str, start: int, end: int, pattern: re.Pattern, window: int = 60) -> bool:
    return bool(pattern.search(text[max(0, start - window):end + window]))


def check_claims(document: str, sources: Sources, *, kind: str = "resume") -> list[str]:
    """Return one problem string per unsupported claim (empty list = grounded)."""
    problems: list[str] = []
    doc = document or ""
    low = doc.lower()

    for term in TECH_TERMS:
        if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", low) and not sources.has(term):
            problems.append(f"unsupported technology: '{term}'")

    for m in _ACRONYM_RE.finditer(doc):
        tok = m.group(0).strip("-/&")
        if len(tok) < 2 or tok.upper() in _ACRONYM_OK or tok.isdigit():
            continue
        if not re.search(r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])", sources.raw, re.IGNORECASE):
            problems.append(f"unsupported acronym/term: '{tok}'")

    for cert in CERTIFICATIONS:
        for m in re.finditer(r"(?<![a-z0-9])" + re.escape(cert) + r"(?![a-z0-9])", low):
            earned = any(cert in c.lower() for c in sources.certs_earned)
            in_prog = any(cert in c.lower() for c in sources.certs_in_progress)
            in_resume = re.search(r"(?<![a-z0-9])" + re.escape(cert) + r"(?![a-z0-9])", sources.resume_text.lower())
            qualified = _near(low, m.start(), m.end(), _INPROGRESS_RE)
            if earned or (in_resume and not in_prog):
                continue
            if in_prog and qualified:
                continue
            problems.append(f"certification '{cert}' is " + ("in progress, but not stated as such"
                                                              if in_prog else "not in the verified evidence"))
            break

    for m in _METRIC_RE.finditer(doc):
        number = m.group(1).replace(",", "").lstrip("$")
        if number in ("0", "1"):
            continue
        if not re.search(r"(?<![\d.])" + re.escape(number) + r"(?![\d])", sources.text.replace(",", "")):
            problems.append(f"unsupported metric: '{m.group(0).strip()}'")

    for m in _YEARS_RE.finditer(doc):
        if not re.search(r"(?<!\d)" + m.group(1) + r"\+?\s*(?:years|yrs)", sources.text):
            problems.append(f"unsupported experience length: '{m.group(0)}'")

    problems += _check_education(doc, sources)
    problems += _check_leadership(doc, sources)
    problems += _check_number_context(doc, sources)

    if kind == "resume":
        problems += _check_history(doc, sources)
    seen: set[str] = set()
    return [p for p in problems if not (p in seen or seen.add(p))]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9+#/-]{3,}", (text or "").lower()) if w not in _STOP}


def _source_lines(sources: Sources) -> list[str]:
    return [ln for ln in re.split(r"[\n.;]+", sources.raw) if ln.strip()]


def _check_education(doc: str, sources: Sources) -> list[str]:
    problems = []
    for m in _DEGREE_RE.finditer(doc):
        term = m.group(0)
        if not re.search(r"(?<![a-z])" + re.escape(term.lower()) + r"(?![a-z])", sources.text):
            problems.append(f"unsupported education claim: '{term}'")
    for m in _INSTITUTION_RE.finditer(doc):
        name = m.group(1).strip()
        if len(name.split()) >= 2 and name.lower() not in sources.text:
            problems.append(f"unsupported institution: '{name}'")
    return problems


def _check_leadership(doc: str, sources: Sources) -> list[str]:
    """Leadership of people must be described in the sources in a comparable sentence."""
    problems = []
    src_lines = _source_lines(sources)
    for m in _LEADERSHIP_RE.finditer(doc):
        people = m.group(2).lower().rstrip("s")
        people_re = re.compile(r"\b" + re.escape(people) + r"s?\b", re.IGNORECASE)
        supported = any(_LEADERSHIP_RE.search(ln) or
                        (re.search(r"\b(led|managed|supervised|supervising|directed|mentored|oversaw|coordinating|"
                                   r"coordinated)\b", ln, re.IGNORECASE) and people_re.search(ln))
                        for ln in src_lines)
        if not supported:
            problems.append(f"unsupported leadership claim: '{m.group(0)}'")
    return problems


def _check_number_context(doc: str, sources: Sources) -> list[str]:
    """A number must appear in the sources in a sentence about the same thing (audit R1).

    Each metric, count or duration in the document is looked up in the source sentences that
    contain the same number; at least one of them must share two content words with the
    document's sentence. A real number moved to a different employer or project fails."""
    problems = []
    src_lines = _source_lines(sources)
    for line in re.split(r"[\n;]+|(?<=[a-z])\.\s", doc or ""):
        if _CONTACT_RE.search(line):
            continue  # code-injected contact details (phone digits are not claims)
        nums = {n.replace(",", "") for n in re.findall(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])", line)}
        nums = {n for n in nums if n not in ("0", "1") and not re.fullmatch(r"(19|20)\d\d", n)}
        if not nums:
            continue
        ctx = _words(line)
        flat = line.replace(",", "")
        for n in sorted(nums):
            holders = [ln.replace(",", "") for ln in src_lines
                       if re.search(r"(?<![\d.])" + re.escape(n) + r"(?![\d])", ln.replace(",", ""))]
            if not holders:
                problems.append(f"unsupported number: '{n}' in '{line.strip()[:80]}'")
                continue
            if len(ctx) >= 2 and not any(len(ctx & _words(h)) >= 2 for h in holders):
                problems.append(f"number '{n}' is used in a different context than in the sources: "
                                f"'{line.strip()[:80]}'")
                continue
            # What is counted must match: "3,500 computers" may not become "3,500 servers".
            unit = re.search(r"(?<![\d.])" + re.escape(n) + r"\+?\s*([a-z]{3,})", flat, re.IGNORECASE)
            # Product/model names ("Microsoft 365", "Windows 11") are names, not counts.
            named = unit is not None and re.search(r"[A-Z][A-Za-z0-9+-]*\s+$", flat[:unit.start()])
            if unit and not named and unit.group(1).lower() not in _STOP:

                u = unit.group(1).lower().rstrip("s")
                if not any(re.search(r"(?<![\d.])" + re.escape(n) + r"\+?\s*(?:\w+\s+){0,2}" + re.escape(u), h,
                                     re.IGNORECASE) for h in holders):
                    problems.append(f"number '{n}' counts '{unit.group(1)}', which the sources do not: "
                                    f"'{line.strip()[:80]}'")
    return problems



def _check_history(doc: str, sources: Sources) -> list[str]:
    """Employment dates/employers preserved; no new dates; projects not shown as jobs."""
    problems: list[str] = []
    orig_exp = _sections(sources.resume_text).get("EXPERIENCE", "")
    new_exp = _sections(doc).get("EXPERIENCE", "")
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.lower().replace("–", "-").replace(" to ", " - "))
    orig_ranges = {norm(m.group(0)) for m in _DATE_RANGE_RE.finditer(orig_exp)}
    new_ranges = {norm(m.group(0)) for m in _DATE_RANGE_RE.finditer(new_exp)}
    for r in sorted(orig_ranges - new_ranges):
        problems.append(f"employment dates changed or dropped: '{r}'")
    for r in sorted(new_ranges - orig_ranges):
        problems.append(f"employment dates not in the original resume: '{r}'")
    # Titles/employers: every experience heading must be recognisable from the original, and
    # seniority words in headings must exist in the original headings.
    orig_heads = [ln.strip() for ln in orig_exp.splitlines() if ln.strip() and not ln.strip().startswith(("-", "*", "•"))]
    orig_words = set().union(*[_words(h) for h in orig_heads]) if orig_heads else set()
    orig_heads_low = " ".join(orig_heads).lower()
    for ln in new_exp.splitlines():
        head = ln.strip()
        if not head or head.startswith(("-", "*", "•")):
            continue
        hw = _words(head)
        if orig_heads and hw and not (hw & orig_words):
            problems.append(f"experience heading not in the original resume: '{head[:80]}'")
        for s in _SENIORITY_RE.finditer(head):
            if not re.search(r"(?<![a-z])" + re.escape(s.group(0).lower()) + r"(?![a-z])", orig_heads_low):
                problems.append(f"seniority/title not in the original resume: '{s.group(0)}' in '{head[:60]}'")
    new_exp_low = new_exp.lower()

    for name in sources.project_names:
        n = name.lower()
        if n and re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", new_exp_low) \
                and not re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", orig_exp.lower()):
            problems.append(f"personal project '{name}' presented as employment")
    return problems


def ats_problems(text: str, *, max_words: int = 900) -> list[str]:
    """Plain-text ATS readability: required sections, no tables/odd glyphs, sane length."""
    problems = []
    secs = _sections(text)
    for s in ("SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "EDUCATION"):
        if s not in secs or not secs[s].strip():
            problems.append(f"missing or empty section: {s}")
    if re.search(r"[│┃┆║╎|]{2,}|\t", text or ""):
        problems.append("table-like layout (ATS parsers lose columns)")
    if re.search(r"[^\x09\x0a\x0d\x20-\x7e -ſ]", text or ""):
        problems.append("non-standard characters (icons/emoji) found")
    words = len((text or "").split())
    if words < 150:
        problems.append(f"too short ({words} words)")
    if words > max_words:
        problems.append(f"too long ({words} words)")
    return problems


_PROMISE_RE = re.compile(
    r"\b(guarantee[sd]?|i will (immediately |quickly )?(increase|reduce|cut|double|triple|eliminate|transform)|"
    r"100% (uptime|satisfaction|success)|never (fail|miss)|best (candidate|fit) you will|from day one i will)\b",
    re.IGNORECASE)


def cover_letter_problems(text: str, job: dict, sources: Sources, *, min_words: int = 120,
                          max_words: int = 320) -> list[str]:
    """Factual + tone + relevance checks for one cover letter."""
    problems = check_claims(text, sources, kind="cover")
    words = len((text or "").split())
    if words < min_words:
        problems.append(f"too short ({words} words, minimum {min_words})")
    if words > max_words:
        problems.append(f"too long ({words} words, maximum {max_words})")
    m = _PROMISE_RE.search(text or "")
    if m:
        problems.append(f"unsupported promise: '{m.group(0)}'")
    if (text or "").count("!") > 1:
        problems.append("unprofessional tone (multiple exclamation marks)")
    title_words = [w for w in re.findall(r"[a-z]{4,}", (job.get("title") or "").lower())][:4]
    if title_words and not any(w in (text or "").lower() for w in title_words):
        problems.append("does not mention the role")
    company = (job.get("company") or "").strip()
    if company and company.lower() not in (text or "").lower():
        problems.append(f"does not mention the employer ({company})")
    if re.search(r"\b(relocat)", text or "", re.IGNORECASE) and not sources.has("relocat"):
        problems.append("mentions relocation, which the profile does not support")
    return problems
