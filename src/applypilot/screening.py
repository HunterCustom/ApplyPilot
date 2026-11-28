"""Evidence-backed answers to application screening questions (fork).

Answers come only from ``APP_DIR/screening_answers.yaml``, written and reviewed by the
candidate. Nothing is inferred: a question without a verified answer, and every
sensitive or legally consequential question, goes to a human.

``screening_answers.yaml``::

    answers:
      - id: work_authorization
        question: "Are you legally authorized to work in the United States?"
        patterns: ["authorized to work", "legally (authorized|eligible) to work", "work authori[sz]ation"]
        answer: "Yes"
        reusable: true          # explicitly authorized for reuse on every application
      - id: sponsorship
        patterns: ["sponsor", "visa"]
        answer: "No"
        reusable: true
      - id: salary
        patterns: ["salary", "compensation expectation", "desired pay"]
        answer: "65000"
        reusable: false         # approved for review display, but asked again each time

Only ``reusable: true`` answers are ever given to the apply agent. Categories in
``SENSITIVE`` (legal attestations, criminal history, demographics, health, age,
identifiers, consent to terms) always need the candidate, even if an answer exists --
unless that entry is ``reusable: true`` *and* ``sensitive_ok: true`` (an explicit,
per-question authorization, e.g. "Decline to self-identify" for EEO questions).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from applypilot import config

log = logging.getLogger(__name__)

# Standard questions nearly every application asks; readiness reports which lack an answer.
STANDARD = {
    "work_authorization": r"authori[sz]ed to work|legally (authori[sz]ed|eligible|able) to work|work authori[sz]ation|"
                          r"eligible to work",
    "sponsorship": r"sponsor|visa",
    "location": r"(where|which city|current) .*(live|located|reside)|commut|relocat|on-?site|in[- ]office|hybrid",
    "availability": r"start date|when can you start|availab|notice period",
    "salary": r"salary|compensation|desired pay|pay expectation|hourly rate|wage",
}
SENSITIVE = {
    "legal_attestation": r"certif(y|ication) that|attest|under penalty|signature|e-?sign|acknowledge (and agree|that)|"
                         r"i agree|terms (and|&) conditions|arbitration|non-?compete|consent to",
    "criminal_background": r"convict|felony|misdemeanou?r|criminal|arrest|background check",
    "demographic": r"gender|sex\b|race|ethnic|hispanic|latino|veteran|disab|sexual orientation|transgender|pronoun",
    "health": r"medical|health condition|pregnan|drug test|vaccin",
    "age_identity": r"date of birth|\bage\b|how old|social security|\bssn\b|driver'?s licen[cs]e number|passport",
    "religion": r"religio|faith",
    "salary_history": r"current salary|previous salary|salary history|past compensation",
}
_STD_RE = {k: re.compile(v, re.IGNORECASE) for k, v in STANDARD.items()}
_SENS_RE = {k: re.compile(v, re.IGNORECASE) for k, v in SENSITIVE.items()}


@dataclass(frozen=True)
class Entry:
    id: str
    patterns: tuple[re.Pattern, ...]
    answer: str
    reusable: bool
    sensitive_ok: bool
    question: str = ""


@dataclass(frozen=True)
class Decision:
    status: str          # answered | needs_review | sensitive
    answer: str | None
    entry_id: str | None
    category: str | None
    reason: str


def answers_path() -> Path:
    return config.APP_DIR / "screening_answers.yaml"


def load_bank(path: Path | None = None) -> list[Entry]:
    path = path or answers_path()
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"{path.name} is not valid YAML: {e}") from e
    out: list[Entry] = []
    for i, item in enumerate((data or {}).get("answers") or []):
        if not isinstance(item, dict) or not str(item.get("answer", "")).strip():
            raise ValueError(f"{path.name}: answers[{i}] needs a non-empty 'answer'")
        pats = item.get("patterns") or ([re.escape(item["question"])] if item.get("question") else [])
        if not pats:
            raise ValueError(f"{path.name}: answers[{i}] needs 'patterns' or 'question'")
        try:
            compiled = tuple(re.compile(str(p), re.IGNORECASE) for p in pats)
        except re.error as e:
            raise ValueError(f"{path.name}: answers[{i}] has an invalid pattern: {e}") from e
        out.append(Entry(id=str(item.get("id") or f"answer_{i}"), patterns=compiled,
                         answer=str(item["answer"]).strip(), reusable=item.get("reusable") is True,
                         sensitive_ok=item.get("sensitive_ok") is True, question=str(item.get("question") or "")))
    return out


def sensitive_category(question: str) -> str | None:
    for cat, rx in _SENS_RE.items():
        if rx.search(question or ""):
            return cat
    return None


def standard_category(question: str) -> str | None:
    for cat, rx in _STD_RE.items():
        if rx.search(question or ""):
            return cat
    return None


def decide(question: str, bank: list[Entry], *, for_agent: bool = True) -> Decision:
    """How one question may be answered. ``for_agent``: only reusable answers count."""
    sens = sensitive_category(question)
    hit = next((e for e in bank if any(p.search(question or "") for p in e.patterns)), None)
    if sens and not (hit and hit.reusable and hit.sensitive_ok):
        return Decision("sensitive", None, hit.id if hit else None, sens,
                        f"{sens.replace('_', ' ')} question: the candidate must answer it")
    if hit is None:
        return Decision("needs_review", None, None, standard_category(question), "no verified answer")
    if for_agent and not hit.reusable:
        return Decision("needs_review", hit.answer, hit.id, standard_category(question),
                        "answer exists but is not authorized for reuse")
    return Decision("answered", hit.answer, hit.id, sens or standard_category(question), "verified answer")


def coverage(bank: list[Entry] | None = None) -> dict:
    """Which standard questions have a reusable verified answer (used by readiness)."""
    bank = load_bank() if bank is None else bank
    covered, missing = [], []
    probes = {"work_authorization": "Are you legally authorized to work in the United States?",
              "sponsorship": "Will you now or in the future require visa sponsorship?",
              "location": "Are you able to commute to the office / where do you currently live?",
              "availability": "When can you start?",
              "salary": "What are your salary expectations?"}
    for cat, probe in probes.items():
        (covered if decide(probe, bank).status == "answered" else missing).append(cat)
    return {"covered": covered, "missing": missing}


def agent_section(bank: list[Entry] | None = None) -> str:
    """Prompt block for the apply agent: verified reusable answers + the stop rules."""
    bank = load_bank() if bank is None else bank
    usable = [e for e in bank if e.reusable]
    lines = ["== SCREENING QUESTIONS (verified answers only) ==",
             "Use ONLY these candidate-approved answers. Match the question's meaning, not just its words."]
    for e in usable:
        label = e.question or e.id.replace("_", " ")
        lines.append(f"  - {label}: {e.answer}")
    if not usable:
        lines.append("  (none configured)")
    lines += [
        "Any other question -- including 'Do you have experience with X?' when X is not plainly stated in the",
        "resume, and every legal attestation, signature, criminal/background, demographic (EEO), health, age,",
        "identity-number, or consent question -- is for the candidate: do NOT guess, do NOT pick a default,",
        "stop and output RESULT:FAILED:needs_human_answer.",
        "Optional questions may be left blank. Never volunteer information that is not asked for.",
    ]
    return "\n".join(lines)
