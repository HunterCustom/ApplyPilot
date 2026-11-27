"""Claude Code PreToolUse hook that blocks submission-like actions during dry runs.

The launcher passes this hook to the apply agent only in dry-run mode
(``--settings``). Claude Code runs it before every tool call; exit code 2
blocks the call and returns the stderr message to the agent.

This is a best-effort, tool-level safeguard, not a guarantee: it can only
judge what the tool input says. A click whose ``element`` description does
not reveal that it submits (e.g. a button labelled "Continue" that actually
submits) cannot be recognised. Dry runs therefore still require supervision.
"""
import json
import re
import sys

# Wording that commits an application. "Apply" alone is excluded: job pages use it to open the form.
SUBMIT_WORDS = re.compile(
    r"\b(submit|send\s+(my\s+)?application|complete\s+(my\s+)?application|"
    r"finish\s+(my\s+)?application|confirm\s+(and\s+)?apply|apply\s+now\s+and\s+submit)\b",
    re.IGNORECASE,
)
SCRIPT_SUBMIT = re.compile(r"submit|requestSubmit|\.click\s*\(|dispatchEvent|form\.", re.IGNORECASE)
ENTER_KEYS = {"enter", "numpadenter", "return"}


def blocked_reason(tool_name: str, tool_input: dict) -> str | None:
    """Return why a tool call must be blocked in a dry run, or None to allow it."""
    name = (tool_name or "").split("__")[-1]
    tool_input = tool_input or {}
    if tool_name.startswith("mcp__gmail__") and name in {"send_email", "draft_email"}:
        return "sending email is disabled during dry runs"
    if name in {"browser_click", "browser_double_click", "browser_hover_click"}:
        label = " ".join(str(tool_input.get(k, "")) for k in ("element", "text", "name"))
        if SUBMIT_WORDS.search(label):
            return f"clicking a submit control ({label.strip()[:60]!r}) is disabled during dry runs"
    if name == "browser_press_key" and str(tool_input.get("key", "")).lower() in ENTER_KEYS:
        return "pressing Enter can submit a form and is disabled during dry runs"
    if name == "browser_type" and tool_input.get("submit"):
        return "typing with submit=true is disabled during dry runs"
    if name in {"browser_evaluate", "browser_run_code"}:
        code = " ".join(str(v) for v in tool_input.values())
        if SCRIPT_SUBMIT.search(code):
            return "scripts that click or submit are disabled during dry runs"
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("dry-run guard: unreadable hook input; blocking to stay safe", file=sys.stderr)
        return 2
    reason = blocked_reason(event.get("tool_name", ""), event.get("tool_input") or {})
    if reason:
        print(f"DRY RUN GUARD: {reason}. Do not submit. Output RESULT:DRY_RUN now.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
