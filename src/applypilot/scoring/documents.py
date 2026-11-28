"""Document artifacts: sources of truth, staleness fingerprints, approved vs. rejected storage (fork).

* Approved documents live in ``tailored_resumes/`` / ``cover_letters/`` and are the only
  ones whose paths reach ``tailored_resume_path`` / ``cover_letter_path``.
* Rejected attempts go to a ``rejected/`` sub-folder with their validation report and are
  recorded in ``resume_rejected_path`` / ``cover_rejected_path`` -- never used for applying.
* ``documents_for`` is a fingerprint of everything a document was built from (job text,
  base resume, profile facts, candidate evidence). If any of it changes, the documents
  are stale and the job is no longer application-ready until they are regenerated.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from applypilot import config
from applypilot.scoring.candidate_evidence import load_candidate_evidence
from applypilot.scoring.factcheck import Sources, build_sources


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8", "replace")).hexdigest()


def _file_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def load_evidence_safe() -> dict:
    try:
        return load_candidate_evidence()
    except (ValueError, OSError):
        return {}


def truth_sources(profile: dict, resume_text: str | None = None) -> Sources:
    """The candidate's resume, profile and verified evidence: the only facts documents may use."""
    resume = resume_text if resume_text is not None else _file_text(config.RESUME_PATH)
    return build_sources(resume, profile, load_evidence_safe())


def fingerprint(job: dict, profile: dict | None = None, resume_text: str | None = None) -> str:
    """Fingerprint of the inputs of a job's documents (job text + sources of truth)."""
    from applypilot.scoring.candidate_evidence import EVIDENCE_PATH
    profile = profile if profile is not None else config.load_profile()
    resume = resume_text if resume_text is not None else _file_text(config.RESUME_PATH)
    facts = {k: profile.get(k) for k in ("skills_boundary", "resume_facts", "personal", "experience")}
    parts = [job.get("title") or "", job.get("full_description") or "", job.get("application_url") or "",
             _sha(resume), _sha(json.dumps(facts, sort_keys=True, default=str)), _sha(_file_text(EVIDENCE_PATH))]
    return _sha("\n\x00".join(parts))[:32]


def safe_prefix(job: dict) -> str:
    safe_title = re.sub(r"[^\w\s-]", "", job.get("title") or "job")[:50].strip().replace(" ", "_")
    safe_site = re.sub(r"[^\w\s-]", "", job.get("company") or job.get("site") or "site")[:20].strip().replace(" ", "_")
    return f"{safe_site}_{safe_title}_{_sha(job['url'])[:8]}"


def save_artifact(base_dir: Path, prefix: str, suffix: str, text: str, report: dict, approved: bool) -> Path:
    """Write the document (+ its report). Rejected attempts go to ``base_dir/rejected``."""
    folder = base_dir if approved else base_dir / "rejected"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{prefix}{suffix}.txt"
    path.write_text(text, encoding="utf-8")
    (folder / f"{prefix}{suffix}_REPORT.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path
