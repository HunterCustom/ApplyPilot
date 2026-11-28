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

    if kind == "resume":
        problems += _check_history(doc, sources)
    seen: set[str] = set()
    return [p for p in problems if not (p in seen or seen.add(p))]


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
