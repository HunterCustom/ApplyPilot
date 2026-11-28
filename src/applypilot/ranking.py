"""Preference ranking (fork): salary and commute tier influence order, never eligibility.

Order = ``fit_score * FIT_WEIGHT`` (20) + salary points + commute points. Fit stays the main
signal; keep the configured preference spread to about two fit points (40) so pay and commute
reorder similar fits but cannot lift a clearly weaker match above a much better one. Nothing
here filters: undisclosed or low pay only lowers the order, and eligibility is still decided by
``eligibility.py``.

Configured in ``APP_DIR/priority.yaml`` (the candidate's own figures stay in that private file;
without ``salary.bands`` pay does not affect the order)::

    salary:
      hours_per_year: 2080          # used to annualize hourly pay (flagged for human review)
      undisclosed: 5                # points when no salary is published
      bands:                        # first band whose minimum the annual midpoint reaches
        - {min: <annual amount>, label: <name>, points: <number>}
        - {min: 0,               label: low,    points: -10}
    commute:                        # eligibility tier -> points
      highest: 20
      high: 12
      acceptable: 5
"""
from __future__ import annotations

import copy
import re

FIT_WEIGHT = 20

DEFAULTS: dict = {
    "salary": {
        "hours_per_year": 2080,
        "undisclosed": 5,
        "bands": [],  # set in APP_DIR/priority.yaml; empty = pay does not affect the order
    },
    "commute": {"highest": 20, "high": 12, "acceptable": 5},
}

_NUM_RE = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kK])?")
_INTERVALS = (
    ("hour", re.compile(r"\b(hour|hourly|hr|/h)\b", re.IGNORECASE), None),
    ("week", re.compile(r"\b(week|weekly|wk)\b", re.IGNORECASE), 52),
    ("month", re.compile(r"\b(month|monthly|mo)\b", re.IGNORECASE), 12),
    ("day", re.compile(r"\b(day|daily)\b", re.IGNORECASE), 260),
    ("year", re.compile(r"\b(year|yearly|annual|annually|yr|salary)\b", re.IGNORECASE), 1),
)


def yaml_error():
    import yaml
    return yaml.YAMLError


def load_preferences(path=None) -> dict:
    """Defaults merged with ``salary:`` / ``commute:`` from priority.yaml (bad values ignored)."""
    prefs = copy.deepcopy(DEFAULTS)
    try:
        from pathlib import Path

        import yaml

        from applypilot.config import APP_DIR
        p = Path(path) if path else APP_DIR / "priority.yaml"
        data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError, ImportError, yaml_error()):  # optional file: defaults apply
        data = {}
    data = data if isinstance(data, dict) else {}
    sal_raw = data.get("salary")
    sal: dict = sal_raw if isinstance(sal_raw, dict) else {}
    for k in ("hours_per_year", "undisclosed"):
        if isinstance(sal.get(k), (int, float)):
            prefs["salary"][k] = sal[k]
    bands = sal.get("bands")
    if isinstance(bands, list) and all(isinstance(b, dict) and isinstance(b.get("min"), (int, float)) for b in bands):
        prefs["salary"]["bands"] = sorted(
            [{"min": b["min"], "label": str(b.get("label", "")), "points": float(b.get("points", 0))} for b in bands],
            key=lambda b: -b["min"])
    com_raw = data.get("commute")
    com: dict = com_raw if isinstance(com_raw, dict) else {}
    for tier, pts in com.items():
        if isinstance(pts, (int, float)):
            prefs["commute"][str(tier)] = pts
    return prefs


def parse_salary(text: str | None, hours_per_year: float = 2080) -> dict | None:
    """Published pay -> {low, high, interval, annual_low, annual_high, hourly}; None if absent."""
    if not text or not re.search(r"\d", text):
        return None
    nums = []
    for m in _NUM_RE.finditer(text):
        v = float(m.group(1).replace(",", ""))
        if m.group(2):
            v *= 1000
        nums.append(v)
    nums = [n for n in nums if n >= 5][:2]  # ignore stray small numbers ("1-2 years", footnotes)
    if not nums:
        return None
    low, high = min(nums), max(nums)
    interval, factor = None, None
    for name, rx, f in _INTERVALS:
        if rx.search(text):
            interval, factor = name, f
            break
    if interval is None:  # infer: small numbers are hourly rates, large ones annual salaries
        interval, factor = ("hour", None) if high < 500 else ("year", 1)
    mult = hours_per_year if interval == "hour" else factor
    return {"low": low, "high": high, "interval": interval, "annual_low": low * mult, "annual_high": high * mult,
            "hourly": interval == "hour"}


def salary_band(job: dict, prefs: dict | None = None) -> tuple[str, float, str]:
    """(label, points, note) for a job's published pay."""
    prefs = prefs or DEFAULTS
    sal = prefs["salary"]
    parsed = parse_salary(job.get("salary"), float(sal["hours_per_year"]))
    if not sal["bands"]:
        return "not ranked", 0.0, "salary preferences not configured (priority.yaml salary.bands)"
    if parsed is None:
        return "undisclosed", float(sal["undisclosed"]), "no salary published (not a rejection)"
    mid = (parsed["annual_low"] + parsed["annual_high"]) / 2
    note = f"~${mid:,.0f}/yr"
    if parsed["hourly"]:
        note += (f" from ${parsed['low']:g}-{parsed['high']:g}/hr x {sal['hours_per_year']:g} h; confirm hours, "
                 "classification, benefits and travel pay")
    for b in sal["bands"]:
        if mid >= b["min"]:
            return b["label"], float(b["points"]), note
    return "below all bands", 0.0, note


def preference_points(job: dict, prefs: dict | None = None) -> tuple[float, str]:
    prefs = prefs or DEFAULTS
    label, pts, note = salary_band(job, prefs)
    tier = job.get("eligibility_tier") or ""
    cpts = float(prefs["commute"].get(tier, 0)) if tier else 0.0
    return pts + cpts, f"salary {label} ({note}); commute tier {tier or 'n/a'} (+{cpts:g})"


def rank_key(job: dict, prefs: dict | None = None) -> float:
    return float(job.get("fit_score") or 0) * FIT_WEIGHT + preference_points(job, prefs)[0]


def rank(jobs: list[dict], prefs: dict | None = None) -> list[dict]:
    """Stable descending order by fit plus preferences (never drops a job)."""
    prefs = prefs or load_preferences()
    return sorted(jobs, key=lambda j: rank_key(j, prefs), reverse=True)
