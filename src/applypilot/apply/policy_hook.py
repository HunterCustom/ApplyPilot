"""Claude Code hooks that enforce the apply-agent session policy (S1/S2, Task E).

Invoked by the launcher-written settings file as::

    python -m applypilot.apply.policy_hook <pre|post|start> <policy.json>

PreToolUse ("pre") denies by exiting 2 (stderr goes back to the agent). Every
error path also exits 2: the hook fails closed. PostToolUse ("post") inspects
the page URL each browser tool reports and halts the session if the browser
ended up somewhere it may not be (redirects, link clicks, script navigation).
Once a terminal violation is recorded, every later tool call is denied.

Web pages are untrusted: nothing a page says can change these decisions.
"""
from __future__ import annotations

import json
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from applypilot.apply import dry_run_guard
from applypilot.apply import policy as P

GUARD_PREFIX = "DRY RUN GUARD"  # launcher recognises dry-run guard blocks by this text
POLICY_PREFIX = "APPLYPILOT POLICY"

_PAGE_URL_RE = re.compile(r"Page URL:\s*(\S+)")
CONFIRMATION_RE = re.compile(
    r"thank you for (your )?appl|application (has been |was )?(received|submitted|complete)|"
    r"successfully (submitted|applied)|we('ve| have) received your application",
    re.IGNORECASE,
)


def _short(tool_name: str) -> str:
    return tool_name.split("__")[-1]


def _is_submit_like(tool_name: str, tool_input: dict) -> str | None:
    """Reason string if the call may commit an application (needs approval in real runs)."""
    name = _short(tool_name)
    if name in {"browser_click"}:
        label = " ".join(str(tool_input.get(k, "")) for k in ("element", "text", "name"))
        if dry_run_guard.SUBMIT_WORDS.search(label):
            return f"submit-like click ({label.strip()[:60]!r})"
    if name == "browser_press_key" and str(tool_input.get("key", "")).lower() in dry_run_guard.ENTER_KEYS:
        return "pressing Enter may submit a form"
    if name == "browser_type" and tool_input.get("submit"):
        return "typing with submit=true"
    return None


def decide_pre(policy: P.SessionPolicy, tool_name: str, tool_input: dict) -> tuple[bool, str, bool]:
    """Return (allow, reason, terminal). Pure apart from reading session state."""
    if P.session_halted(policy):
        return False, "session halted after a policy violation; stop and output RESULT:FAILED:policy_violation", True
    allowed = set(P.allowed_tool_names(policy.verify_enabled))
    if tool_name not in allowed:
        return False, f"tool '{tool_name}' is not permitted", True
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    name = _short(tool_name)

    for key in ("url",):
        if key in tool_input:
            why = P.url_block_reason(tool_input[key], policy.allowed_hosts, policy.trusted_hosts)
            if why:
                return False, f"navigation blocked: {why}", True
    if name == "browser_tabs" and str(tool_input.get("action", "")).lower() == "new" and tool_input.get("url"):
        why = P.url_block_reason(tool_input["url"], policy.allowed_hosts, policy.trusted_hosts)
        if why:
            return False, f"navigation blocked: {why}", True

    if name == "browser_file_upload":
        allowed_paths = {str(Path(p).resolve()).lower() for p in policy.upload_paths}
        for p in tool_input.get("paths") or []:
            if not isinstance(p, str) or str(Path(p).resolve()).lower() not in allowed_paths:
                return False, "upload blocked: only the prepared resume and cover letter may be uploaded", True

    for value in P.iter_typed_values(tool_input):
        why = P.sensitive_value_reason(value, policy.allowed_sensitive_values)
        if why:
            return False, f"typing blocked: {why}; stop and output RESULT:FAILED:needs_human", False

    if policy.dry_run:
        why = dry_run_guard.blocked_reason(tool_name, tool_input)
        if why:
            return False, f"{GUARD_PREFIX}: {why}. Do not submit. Output RESULT:DRY_RUN now.", False
    return True, "", False


def wait_for_approval(policy: P.SessionPolicy, what: str, poll_s: float = 1.0) -> bool:
    """Ask the human (out of band) to approve one submission. The agent cannot grant this."""
    nonce = secrets.token_hex(8)
    req = P.approval_request_path(policy.dir)
    req.write_text(json.dumps({"nonce": nonce, "what": what, "t": time.time()}), encoding="utf-8")
    grant = policy.dir / f"approval-grant-{nonce}.json"
    print(f"{POLICY_PREFIX}: submission approval requested: applypilot apply --approve "
          f"{policy.dir} --nonce {nonce}", file=sys.stderr)
    deadline = time.time() + policy.approval_timeout_s
    try:
        while time.time() < deadline:
            if grant.exists():
                P.record_audit(policy, {"phase": "approval", "decision": "granted", "nonce": nonce})
                return True
            time.sleep(poll_s)
        return False
    finally:
        req.unlink(missing_ok=True)


def handle_pre(policy: P.SessionPolicy, event: dict) -> int:
    tool = event.get("tool_name") or ""
    tool_input = event.get("tool_input") or {}
    allow, reason, terminal = decide_pre(policy, tool, tool_input)
    host = ""
    if isinstance(tool_input, dict) and isinstance(tool_input.get("url"), str):
        host = P.normalize_host(urlsplit(tool_input["url"]).hostname)
    if allow and not policy.dry_run and policy.require_submit_approval:
        submit = _is_submit_like(tool, tool_input)
        if submit:
            if wait_for_approval(policy, submit):
                P.record_audit(policy, {"phase": "pre", "tool": tool, "decision": "allow",
                                        "submit_approved": True, "id": event.get("tool_use_id")})
                return 0
            allow, reason, terminal = False, f"{submit}: not approved by a human; output RESULT:FAILED:submit_not_approved", False
    P.record_audit(policy, {"phase": "pre", "tool": tool, "decision": "allow" if allow else "deny",
                            "reason": reason[:160], "host": host, "id": event.get("tool_use_id")})
    if allow:
        return 0
    if not reason.startswith(GUARD_PREFIX):
        P.record_violation(policy, tool, reason, terminal)
        reason = f"{POLICY_PREFIX}: {reason}"
    print(reason, file=sys.stderr)
    return 2


def _response_text(resp) -> str:
    if isinstance(resp, str):
        return resp
    try:
        return json.dumps(resp)
    except (TypeError, ValueError):
        return str(resp)


def handle_post(policy: P.SessionPolicy, event: dict) -> int:
    tool = event.get("tool_name") or ""
    text = _response_text(event.get("tool_response"))
    bad = None
    for url in _PAGE_URL_RE.findall(text):
        url = url.strip().rstrip(",;)\\\"'")
        why = P.url_block_reason(url, policy.allowed_hosts, policy.trusted_hosts)
        if why:
            bad = f"browser left the approved sites ({why})"
            break
    if bad:
        P.record_violation(policy, tool, bad, terminal=True)
        P.record_audit(policy, {"phase": "post", "tool": tool, "decision": "halt"})
        print(json.dumps({"decision": "block",
                          "reason": f"{POLICY_PREFIX}: {bad}. Stop now and output RESULT:FAILED:policy_violation"}))
        return 0
    if not policy.dry_run and CONFIRMATION_RE.search(text):
        approved = any(r.get("submit_approved") for r in P.read_jsonl(policy.dir / "audit.jsonl"))
        if approved:
            P.record_audit(policy, {"phase": "post", "tool": tool, "evidence": "confirmation_text"})
    P.record_audit(policy, {"phase": "post", "tool": tool, "decision": "ok"})
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        phase, policy_path = argv[0], argv[1]
        policy = P.SessionPolicy.load(policy_path)
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        if phase == "start":
            (policy.dir / "hook_alive").write_text(str(time.time()), encoding="utf-8")
            return 0
        if phase == "pre":
            return handle_pre(policy, event)
        if phase == "post":
            return handle_post(policy, event)
        raise ValueError(f"unknown phase {phase!r}")
    except Exception as e:  # noqa: BLE001 -- any error must deny (fail closed)
        print(f"{POLICY_PREFIX}: policy hook error ({type(e).__name__}); blocking to stay safe", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
