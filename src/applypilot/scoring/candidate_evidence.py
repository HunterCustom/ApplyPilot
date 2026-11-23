"""Optional, locally stored evidence for job-fit scoring.

Evidence is separate from the submitted resume. Only user-reviewed facts should
be included; professional work and personal projects must remain distinguishable.
"""
from pathlib import Path

import yaml

from applypilot.config import APP_DIR

EVIDENCE_PATH = APP_DIR / "candidate_evidence.yaml"
CATEGORIES = ("professional_experience", "certifications", "personal_projects")
MAX_ITEMS_PER_CATEGORY = 40
MAX_ITEM_CHARS = 450
MAX_EVIDENCE_CHARS = 10000


def load_candidate_evidence(path=None):
    """Load optional evidence YAML. No file means resume-only scoring."""
    path = Path(path) if path is not None else EVIDENCE_PATH
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    if data.get("enabled") is not True:
        return {}
    validated = {}
    for category in CATEGORIES:
        items = data.get(category, [])
        if not isinstance(items, list):
            raise ValueError(f"{category} must be a list")
        if len(items) > MAX_ITEMS_PER_CATEGORY:
            raise ValueError(f"{category} exceeds {MAX_ITEMS_PER_CATEGORY} entries")
        clean = []
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{category} entries must be nonempty strings")
            if len(item) > MAX_ITEM_CHARS:
                raise ValueError(f"{category} entry exceeds {MAX_ITEM_CHARS} characters")
            clean.append(item.strip())
        validated[category] = clean
    return validated


def format_candidate_evidence(evidence):
    """Return a bounded, clearly labelled candidate-evidence section."""
    if not evidence:
        return ""
    headings = {
        "professional_experience": "USER-VERIFIED PROFESSIONAL EXPERIENCE",
        "certifications": "USER-VERIFIED CERTIFICATIONS",
        "personal_projects": "USER-VERIFIED PERSONAL PROJECTS (NOT EMPLOYMENT)",
    }
    parts = []
    for category in CATEGORIES:
        if evidence.get(category):
            lines = "\n".join(f"- {entry}" for entry in evidence[category])
            parts.append(f"{headings[category]}:\n{lines}")
    result = "\n\n".join(parts)
    if len(result) > MAX_EVIDENCE_CHARS:
        raise ValueError("Candidate evidence exceeds maximum prompt length")
    return result
