"""Optional pre-scoring job ordering. Never rejects or modifies database rows."""
from pathlib import Path

import yaml

from applypilot.config import APP_DIR


def load_priority_config(path=None):
    """Read optional per-user priority.yaml; disabled when no file exists."""
    path = Path(path) if path is not None else APP_DIR / "priority.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("priority.yaml must contain a mapping")
    return data if data.get("enabled", False) else {}


def _contains(text, terms):
    return any(str(term).lower() in text for term in terms if term)


def priority_score(job, config):
    """Title relevance, stated location and seniority are ranking hints only.

    Remote labels are not proof of eligibility; ambiguous locations stay visible.
    """
    title = (job.get("title") or "").lower()
    location = (job.get("location") or "").lower()
    description = (job.get("full_description") or "").lower()
    score = 0

    if _contains(title, config.get("preferred_titles", [])):
        score += 100
    elif _contains(title, config.get("secondary_titles", [])):
        score += 55
    if _contains(title, config.get("deprioritize_titles", [])):
        score -= 80
    if _contains(title, config.get("senior_titles", [])):
        score -= 35

    if _contains(location, config.get("preferred_locations", [])):
        score += 70
    elif not location:
        score += 0  # Unknown, not disqualified
    elif _contains(location, config.get("international_locations", [])):
        score -= 100
    elif "remote" in location:
        # Remote outside NC may still be eligible. Leave visible, below NC jobs.
        score += 5
    elif _contains(description[:4000], config.get("preferred_locations", [])):
        score += 15
    else:
        score -= 20

    return score


def prioritize_jobs(jobs, config):
    """Stable descending rank; caller decides how many jobs to score."""
    if not config:
        return list(jobs)
    return sorted(jobs, key=lambda job: priority_score(job, config), reverse=True)
