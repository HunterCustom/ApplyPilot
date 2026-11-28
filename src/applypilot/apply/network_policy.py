"""Pure network decisions for the launcher-owned browser boundary (Phase 3b).

Two enforcement points use these functions, both owned by the launcher (never
by the agent or the page):

* ``network_guard.BrowserController`` -- CDP ``Fetch`` interception inside
  Chrome. Sees every HTTP(S) request (navigations, redirect hops, subframes,
  popups, fetch/XHR, beacons, workers) with method and resource type *before
  it is sent*, and decides it with :func:`decide_request`.
* ``egress_proxy.EgressProxy`` -- the only route Chrome has to the network
  (``--proxy-server``). Sees every connection, including WebSockets and
  service-worker traffic, but only the host for HTTPS. Decides with
  :func:`proxy_host_reason` and :func:`ip_block_reason` (after its own DNS
  resolution, connecting only to the vetted address: no DNS rebinding).

Allowlists come only from the session policy written before the agent starts
(job row + constant ATS list + constant static asset hosts). Nothing a page
says or references can widen them.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from applypilot.apply import policy as P

# Exact hosts of large static CDNs that real ATS pages load fonts/scripts/styles from.
# Rules: exact hosts (or provider-owned wildcards), never multi-tenant user-content hosts
# (*.cloudfront.net, *.github.io, *.amazonaws.com ...) whose logs an attacker could read.
# Asset hosts are GET/HEAD-only and only for subresource types -- never documents, XHR or fetch.
STATIC_ASSET_HOSTS = (
    "fonts.googleapis.com", "fonts.gstatic.com", "ajax.googleapis.com", "www.gstatic.com",
    "cdnjs.cloudflare.com", "cdn.jsdelivr.net", "code.jquery.com", "unpkg.com",
    "use.typekit.net", "p.typekit.net", "maxcdn.bootstrapcdn.com", "stackpath.bootstrapcdn.com",
    "*.myworkdaycdn.com", "*.ashbyprd.com",
)
ASSET_RESOURCE_TYPES = frozenset({"Stylesheet", "Image", "Media", "Font", "Script", "TextTrack", "Manifest"})
# Methods that do not carry an application body. Everything else is state-changing.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Resource types that exist to transmit data (navigator.sendBeacon, <a ping>, CSP reports):
# state-changing whatever their method.
TRANSMIT_TYPES = frozenset({"Ping", "CSPViolationReport", "SignedExchange"})
ASSET_MAX_QUERY = 256  # static assets need no long, data-bearing query strings
LOCAL_SCHEMES = frozenset({"data", "blob"})  # never leave the browser
NETWORK_SCHEMES = {"http": "http", "https": "https", "ws": "http", "wss": "https"}

SELFTEST_HOST = "applypilot-selftest.invalid"  # .invalid never resolves (RFC 6761)


@dataclass(frozen=True)
class RequestDecision:
    action: str            # "allow" | "block" | "approve" (real run: needs a human grant)
    reason: str = ""
    terminal: bool = False  # halt the session (possible attack / off-site navigation)
    boundary: bool = False  # dry run: the page tried to transmit data -> stop the run


ALLOW = RequestDecision("allow")


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    if ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network("64:ff9b:1::/48"):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)  # NAT64
    if int(ip) >> 32 == 0 and int(ip) > 1:
        return ipaddress.IPv4Address(int(ip))  # deprecated IPv4-compatible ::a.b.c.d
    return None


def ip_block_reason(addr: str) -> str | None:
    """Why a resolved address must never be contacted, or None if it is public unicast."""
    try:
        ip = ipaddress.ip_address(addr.strip("[]").split("%")[0])
    except ValueError:
        return f"unparseable address {addr!r}"
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_ipv4(ip)
        if inner is not None:
            why = ip_block_reason(str(inner))
            if why:
                return f"{why} (embedded in {ip})"
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or not ip.is_global):
        return f"non-public address {ip}"
    return None


def _host(url: str) -> str:
    try:
        return P.normalize_host(urlsplit(url).hostname)
    except ValueError:
        return ""


def is_private_destination(url: str) -> bool:
    """IP literal (any form) or local-only name: always an attack indicator on a job page."""
    host = _host(url)
    return bool(host) and (P._is_ip_literal(host) or host == "localhost"
                           or host.endswith((".localhost", ".local", ".internal", ".arpa")))


def _as_http(url: str) -> str:
    scheme, sep, rest = url.partition(":")
    mapped = NETWORK_SCHEMES.get(scheme.lower())
    return f"{mapped}{sep}{rest}" if mapped else url


def _is_preflight(method: str, headers: dict | None, has_body: bool) -> bool:
    """A browser-generated CORS preflight: OPTIONS, no body, Access-Control-Request-Method set."""
    names = {str(k).lower() for k in (headers or {})}
    return method == "OPTIONS" and not has_body and "access-control-request-method" in names


def carries_sensitive(url: str, tokens: list[str] | None) -> bool:
    """True if any applicant value (email, phone digits, name ...) appears in the URL."""
    if not tokens:
        return False
    from urllib.parse import unquote_plus
    hay = unquote_plus(unquote_plus(url)).lower()
    digits = "".join(ch for ch in hay if ch.isdigit())
    for t in tokens:
        t = (t or "").strip().lower()
        if len(t) < 4:
            continue
        if t.isdigit():
            if len(t) >= 7 and t in digits:
                return True
        elif t in hay:
            return True
    return False


def decide_request(policy: P.SessionPolicy, *, url: str, method: str, resource_type: str,
                   is_main_frame: bool = False, form_submission: bool = False,
                   headers: dict | None = None, has_body: bool = False,
                   sensitive_tokens: list[str] | None = None) -> RequestDecision:
    """Decide one browser request before it is sent. Pure; callers enforce the result.

    State-changing = any method other than GET/HEAD, a real CORS preflight excepted; any request
    with a body; a GET form submission; a beacon/ping/report; or any request whose URL carries
    an applicant value (data can leave in a GET query just as well as in a POST body)."""
    method = (method or "").upper()
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return RequestDecision("block", "unparseable URL", terminal=True)
    is_document = resource_type == "Document"
    if scheme in LOCAL_SCHEMES and not is_main_frame:
        return ALLOW
    if scheme not in NETWORK_SCHEMES:
        return RequestDecision("block", f"scheme '{scheme or '(none)'}' is not allowed", terminal=True)

    http_url = _as_http(url)
    why = P.url_block_reason(http_url, policy.allowed_hosts, policy.trusted_hosts)
    if why:
        private = is_private_destination(http_url)
        asset_ok = (not private and resource_type in ASSET_RESOURCE_TYPES and method in ("GET", "HEAD")
                    and not has_body and len(urlsplit(url).query) <= ASSET_MAX_QUERY
                    and not carries_sensitive(url, sensitive_tokens)
                    and P.url_block_reason(http_url, list(policy.asset_hosts)) is None)
        if not asset_ok:
            return RequestDecision("block", why, terminal=private or (is_document and is_main_frame))
    elif is_document and is_main_frame:
        tenant = P.tenant_block_reason(http_url, list(policy.tenant_hosts))
        if tenant:
            return RequestDecision("block", tenant, terminal=True)


    writes = ((method not in ("GET", "HEAD") and not _is_preflight(method, headers, has_body))
              or has_body or (is_document and form_submission) or resource_type in TRANSMIT_TYPES)
    if not writes and carries_sensitive(url, sensitive_tokens):
        writes = True
        form_submission = False
        method_desc = f"{method} with applicant data in the URL"
    else:
        method_desc = f"{method} {resource_type or 'request'}"
    if writes:
        what = "form submission" if form_submission else method_desc

        if policy.dry_run:
            return RequestDecision("block", f"dry run: {what} would transmit data", boundary=True)
        return RequestDecision("approve", f"{what} needs a human approval")
    return ALLOW


def proxy_host_reason(policy: P.SessionPolicy | None, host: str, port: int) -> str | None:
    """Host-level egress rule for the proxy (it cannot see methods or paths of HTTPS)."""
    if policy is None:
        return "no active session policy"
    host = P.normalize_host(host)
    hostport = f"{host}:{port}"
    if hostport in {P.normalize_host(t) for t in policy.trusted_hosts}:
        return None
    if port != 443:
        return f"port {port} is not allowed (HTTPS on 443 only)"
    patterns = list(policy.allowed_hosts) + list(policy.asset_hosts)
    return P.url_block_reason(f"https://{host}/", patterns)
