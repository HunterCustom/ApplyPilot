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
    """Write the document (+ its report). Rejected attempts go to ``base_dir/rejected``.

    An approved document replaces any older same-stem PDF: the old PDF was rendered from
    other text and must never be mistaken for this document's rendering."""
    folder = base_dir if approved else base_dir / "rejected"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{prefix}{suffix}.txt"
    if approved:
        path.with_suffix(".pdf").unlink(missing_ok=True)
    path.write_text(text, encoding="utf-8")
    (folder / f"{prefix}{suffix}_REPORT.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path


# --- immutable artifact binding --------------------------------------------------------------------
# A document is usable only as the exact bytes that were validated: the text hash is recorded at
# validation, the PDF hash only when rendered from text with that hash, and a human approval stores
# the manifest it saw. Any later change to a file, a missing PDF, or a new input fingerprint makes
# the job not ready (fail closed); the launcher hands the agent only hash-verified files.

KINDS = ("resume", "cover")


def sha_file(path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _cols(kind: str) -> tuple[str, str, str, str, str]:
    if kind == "resume":
        return "tailored_resume_path", "resume_validation", "resume_sha", "resume_pdf_path", "resume_pdf_sha"
    if kind == "cover":
        return "cover_letter_path", "cover_validation", "cover_sha", "cover_pdf_path", "cover_pdf_sha"
    raise ValueError(kind)


def artifact_problems(job: dict, kind: str) -> list[str]:
    """Why this job's approved ``kind`` document is not usable as-is (empty = verified)."""
    path_c, val_c, sha_c, pdf_c, pdf_sha_c = _cols(kind)
    label = "tailored resume" if kind == "resume" else "cover letter"
    path = job.get(path_c)
    if not path or job.get(val_c) != "passed":
        return [f"no validated {label}" + (f" ({job.get(val_c)})" if job.get(val_c) else "")]
    if not job.get(sha_c):
        return [f"{label} has no validation hash (regenerate it)"]
    actual = sha_file(path)
    if actual is None:
        return [f"{label} file is missing"]
    if actual != job[sha_c]:
        return [f"{label} text changed after validation"]
    pdf = job.get(pdf_c)
    if not pdf or not job.get(pdf_sha_c):
        return [f"{label} PDF has not been rendered from the validated text"]
    pdf_actual = sha_file(pdf)
    if pdf_actual is None:
        return [f"{label} PDF is missing"]
    if pdf_actual != job[pdf_sha_c]:
        return [f"{label} PDF changed after rendering"]
    return []


def cover_usable(job: dict) -> bool:
    """A cover letter is offered to the agent only when it is fully verified."""
    return not artifact_problems(job, "cover")


def manifest(job: dict) -> dict:
    """The exact artifacts a reviewer approves (and that the agent may upload)."""
    out = {"inputs": job.get("documents_for"), "resume": job.get("resume_sha"),
           "resume_pdf": job.get("resume_pdf_sha")}
    if cover_usable(job):
        out.update({"cover_inputs": job.get("cover_for"), "cover": job.get("cover_sha"),
                    "cover_pdf": job.get("cover_pdf_sha")})
    return out


def record_pdf(conn, url: str, kind: str, txt_path: str | Path, pdf_path: str | Path, txt_sha: str) -> bool:
    """Bind a rendered PDF to the validated text it came from (no-op if the text changed meanwhile)."""
    path_c, _val, sha_c, pdf_c, pdf_sha_c = _cols(kind)
    pdf_sha = sha_file(pdf_path)
    if pdf_sha is None or sha_file(txt_path) != txt_sha:
        return False
    cur = conn.execute(f"UPDATE jobs SET {pdf_c} = ?, {pdf_sha_c} = ? WHERE url = ? AND {path_c} = ? AND {sha_c} = ?",
                       (str(pdf_path), pdf_sha, url, str(txt_path), txt_sha))
    return cur.rowcount == 1
