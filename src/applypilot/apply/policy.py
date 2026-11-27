"""Per-session security policy for the apply agent (S1/S2, enforced by policy_hook).

The launcher writes one policy file per application session. The Claude Code
hooks in ``policy_hook`` load it and decide every tool call. Nothing here
trusts the agent or the web page: decisions depend only on the policy file
written by the launcher and on the literal tool input / tool output.

Session files (all inside the session directory, never shown to the agent):
  policy.json      written by the launcher before the agent starts
  audit.jsonl      one record per hook decision (tool name + decision, no values)
  violations.jsonl policy blocks; any entry turns the job into needs_review
  hook_alive       written by the SessionStart hook (proves hooks are active)
  approval-*.json  real-run submission approval requests / grants (see approve_submission)
"""
from __future__ import annotations

import ipaddress
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

# --- Tool surface (S1) --------------------------------------------------------

PLAYWRIGHT_PREFIX = "mcp__playwright__"
VERIFY_PREFIX = "mcp__applypilot_verify__"

# Browser tools the agent may use. Everything else is denied three times over:
# not in --allowedTools (dontAsk denies), listed in --disallowedTools, and refused by the hook.
ALLOWED_BROWSER_TOOLS = (
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click",
    "browser_type", "browser_fill_form", "browser_select_option", "browser_file_upload",
    "browser_tabs", "browser_take_screenshot", "browser_wait_for", "browser_handle_dialog",
    "browser_press_key",
)
VERIFY_TOOL = VERIFY_PREFIX + "get_verification_code"

# Known-dangerous tools exposed by @playwright/mcp that must never run.
DENIED_BROWSER_TOOLS = (
    "browser_evaluate", "browser_run_code", "browser_run_code_unsafe", "browser_install",
    "browser_network_request", "browser_network_requests", "browser_console_messages",
    "browser_drag", "browser_drop", "browser_emulate_media", "browser_resize", "browser_close",
    "browser_find", "browser_hover", "browser_pdf_save", "browser_generate_locator",
)


def allowed_tool_names(verify_enabled: bool) -> list[str]:
    names = [PLAYWRIGHT_PREFIX + t for t in ALLOWED_BROWSER_TOOLS]
    if verify_enabled:
        names.append(VERIFY_TOOL)
    return names


def denied_tool_names() -> list[str]:
    builtins = ["Bash", "PowerShell", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
                "Glob", "Grep", "WebFetch", "WebSearch", "Agent", "Task", "Skill", "ToolSearch"]
    return builtins + [PLAYWRIGHT_PREFIX + t for t in DENIED_BROWSER_TOOLS]


# --- Navigation policy (S2) -------------------------------------------------------

# Applicant tracking systems the agent may visit in addition to the job's own hosts.
# Entries are registrable domains; any subdomain matches.
ATS_DOMAINS = (
    "myworkdayjobs.com", "myworkdaysite.com", "myworkday.com", "workday.com",
    "greenhouse.io", "lever.co", "ashbyhq.com", "icims.com", "adp.com",
    "peopleadmin.com", "successfactors.com", "successfactors.eu", "sapsf.com",
    "taleo.net", "oraclecloud.com", "smartrecruiters.com", "jobvite.com",
    "bamboohr.com", "paylocity.com", "paycomonline.net", "ultipro.com", "ukg.com",
    "applytojob.com", "breezy.hr", "recruitee.com", "workable.com", "dayforcehcm.com",
    "avature.net", "brassring.com", "governmentjobs.com", "schooljobs.com",
    "jazzhr.com", "rippling.com", "pinpointhq.com", "teamtailor.com",
)
# Shared hosting / multi-tenant suffixes where the registrable domain is one label deeper.
_MULTI_TENANT_SUFFIXES = ("co.uk", "com.au", "co.in", "github.io", "herokuapp.com",
                          "azurewebsites.net", "cloudfront.net", "amazonaws.com", "appspot.com")
# Job boards: only the exact host from the job row is allowed, never the whole domain.
_EXACT_HOST_ONLY = ("indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
                    "simplyhired.com", "monster.com", "dice.com", "google.com")

_ALLOWED_SCHEMES = {"https", "http"}
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def normalize_host(host: str | None) -> str:
    return (host or "").strip().rstrip(".").lower()


def base_domain(host: str) -> str:
    host = normalize_host(host)
    labels = host.split(".")
    for suffix in _MULTI_TENANT_SUFFIXES:
        if host.endswith("." + suffix):
            return ".".join(labels[-(suffix.count(".") + 2):])
    return ".".join(labels[-2:])


def _is_ip_literal(host: str) -> bool:
    h = host.strip("[]")
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    # Integer / hex / octal shorthand forms browsers accept (2130706433, 0x7f.1, 127.1).
    return bool(re.fullmatch(r"(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+)){0,3}", h))


def host_matches(host: str, patterns: list[str]) -> bool:
    """True if host equals a pattern, or a pattern '*.d' / 'd' (domain) covers it."""
    host = normalize_host(host)
    for pat in patterns:
        pat = normalize_host(pat)
        if pat.startswith("*."):
            d = pat[2:]
            if host == d or host.endswith("." + d):
                return True
        elif host == pat:
            return True
    return False


def url_block_reason(url: str, allowed_hosts: list[str], trusted_hosts: list[str] | None = None) -> str | None:
    """Why a top-level navigation to ``url`` is not allowed, or None if it is.

    ``trusted_hosts`` are exact host[:port] entries for trusted local
    infrastructure (tests only); they bypass the private-network rule.
    """
    if not isinstance(url, str) or not url.strip():
        return "empty or non-string URL"
    if url.strip() in ("about:blank",):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return "unparseable URL"
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return f"scheme '{scheme or '(none)'}' is not allowed"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "credentials/userinfo in URL are not allowed"
    host = normalize_host(parts.hostname)
    try:
        port = parts.port
    except ValueError:
        return "invalid port"
    hostport = f"{host}:{port}" if port else host
    if trusted_hosts and hostport in {normalize_host(t) for t in trusted_hosts}:
        return None
    if not host:
        return "missing host"
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".arpa")):
        return f"local host '{host}' is not allowed"
    if _is_ip_literal(host):
        return f"IP address hosts are not allowed ('{host}')"
    if "." not in host or not _HOST_RE.match(host):
        return f"invalid or single-label host '{host}'"
    if port not in (None, 80, 443):
        return f"non-standard port {port} is not allowed"
    if not host_matches(host, allowed_hosts):
        return f"host '{host}' is not an approved employer or ATS domain"
    return None


def session_allowed_hosts(job: dict) -> list[str]:
    """Host patterns for one job: its own hosts (employer domain) plus the ATS list."""
    patterns: list[str] = []
    for key in ("application_url", "url"):
        host = normalize_host(urlsplit(job.get(key) or "").hostname)
        if not host or _is_ip_literal(host) or "." not in host:
            continue
        dom = base_domain(host)
        if dom in _EXACT_HOST_ONLY:
            patterns.append(host)
        else:
            patterns.append("*." + dom)
    patterns += ["*." + d for d in ATS_DOMAINS]
    seen: list[str] = []
    for p in patterns:
        if p not in seen:
            seen.append(p)
    return seen


# --- Typed-value policy ---------------------------------------------------------

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\b(?<!\d)\d{9}(?!\d)\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def sensitive_value_reason(value: str, allowed_values: list[str]) -> str | None:
    """Block values that look like SSNs or payment card numbers unless the profile has them."""
    if not isinstance(value, str) or not value:
        return None
    if value.strip() in allowed_values:
        return None
    if _SSN_RE.search(value):
        return "value looks like a Social Security number"
    for m in _CARD_RE.finditer(value):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return "value looks like a payment card number"
    return None


def iter_typed_values(tool_input: dict):
    """Yield every text value a browser tool would type into the page."""
    for key in ("text", "value", "values"):
        v = tool_input.get(key)
        if isinstance(v, str):
            yield v
        elif isinstance(v, list):
            yield from (x for x in v if isinstance(x, str))
    for f in tool_input.get("fields") or []:
        if isinstance(f, dict) and isinstance(f.get("value"), str):
            yield f["value"]


# --- Session policy file --------------------------------------------------------

@dataclass
class SessionPolicy:
    session_dir: str
    dry_run: bool
    allowed_hosts: list[str]
    upload_paths: list[str]
    started_at: float = field(default_factory=time.time)
    verify_enabled: bool = False
    verify_sender_domains: list[str] = field(default_factory=list)
    allowed_sensitive_values: list[str] = field(default_factory=list)
    trusted_hosts: list[str] = field(default_factory=list)  # tests / trusted local infra only
    require_submit_approval: bool = True  # real runs: submit-like clicks wait for a human grant
    approval_timeout_s: int = 600

    @property
    def dir(self) -> Path:
        return Path(self.session_dir)

    def write(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / "policy.json"
        path.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | Path) -> SessionPolicy:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**data)


def _append_jsonl(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def record_audit(policy: SessionPolicy, record: dict) -> None:
    _append_jsonl(policy.dir / "audit.jsonl", {"t": time.time(), **record})


def record_violation(policy: SessionPolicy, tool: str, reason: str, terminal: bool) -> None:
    _append_jsonl(policy.dir / "violations.jsonl",
                  {"t": time.time(), "tool": tool, "reason": reason[:200], "terminal": terminal})


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"corrupt": True})
    return out


def session_halted(policy: SessionPolicy) -> bool:
    return any(v.get("terminal") or v.get("corrupt") for v in read_jsonl(policy.dir / "violations.jsonl"))


# --- Real-run submission approval (trusted controller) --------------------------

def approval_request_path(session_dir: Path) -> Path:
    return session_dir / "approval-request.json"


def approve_submission(session_dir: str | Path, nonce: str) -> bool:
    """Human grant for one pending submit request (called from the CLI, never by the agent)."""
    session_dir = Path(session_dir)
    req = approval_request_path(session_dir)
    if not req.exists():
        return False
    data = json.loads(req.read_text(encoding="utf-8"))
    if data.get("nonce") != nonce:
        return False
    (session_dir / f"approval-grant-{nonce}.json").write_text(
        json.dumps({"nonce": nonce, "t": time.time()}), encoding="utf-8")
    return True
