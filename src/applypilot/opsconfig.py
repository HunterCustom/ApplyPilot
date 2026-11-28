"""Operational settings for the preparation pipeline (fork).

Read from ``APP_DIR/pipeline.yaml`` (optional; every key has a conservative default).
Nothing here changes the LLM provider or model: those stay in ``.env`` (``LLM_URL`` /
``LLM_MODEL``), and the verified baseline remains local Qwen3-14B.

Example ``pipeline.yaml``::

    min_score: 7                 # fit score needed for tailoring / readiness
    freshness_max_days: 30       # posting age (date_posted, else first seen) allowed for preparation
    verify_max_age_hours: 48     # a posting check older than this must be redone before readiness
    require_cover_letter: false  # readiness requires a validated cover letter when true
    verify_delay_s: 2.0          # minimum seconds between two checks of the same host
    daily:
      stages: [discover, enrich, verify, dedupe, classify, score, tailor, cover, pdf, prepare]
      max_score: 200             # LLM scoring calls per run
      max_tailor: 10             # tailored resumes per run
      max_cover: 10
      max_verify: 200
      max_runtime_minutes: 240
"""
from __future__ import annotations

import copy
import logging
from pathlib import Path

import yaml

from applypilot import config

log = logging.getLogger(__name__)

DEFAULTS: dict = {
    "min_score": 7,
    "freshness_max_days": 30,
    "verify_max_age_hours": 48,
    "require_cover_letter": False,
    "verify_delay_s": 2.0,
    "verify_timeout_s": 20,
    "cover_min_words": 120,
    "cover_max_words": 320,
    "daily": {
        "stages": ["discover", "enrich", "verify", "dedupe", "classify", "score", "tailor", "cover", "pdf",
                   "prepare"],
        "max_score": 200,
        "max_tailor": 10,
        "max_cover": 10,
        "max_verify": 200,
        "max_runtime_minutes": 240,
    },
}

DAILY_STAGES = ("discover", "enrich", "verify", "dedupe", "classify", "score", "tailor", "cover", "pdf", "prepare")


def path() -> Path:
    return config.APP_DIR / "pipeline.yaml"


def load() -> dict:
    """Defaults deep-merged with pipeline.yaml. Unknown keys are ignored; bad types fall back."""
    cfg = copy.deepcopy(DEFAULTS)
    p = path()
    if not p.exists():
        return cfg
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("pipeline.yaml unreadable (%s); using defaults", e)
        return cfg
    if not isinstance(data, dict):
        return cfg
    for k, default in DEFAULTS.items():
        if k not in data:
            continue
        v = data[k]
        if isinstance(default, dict) and isinstance(v, dict):
            for dk, dv in default.items():
                if dk in v and (type(v[dk]) is type(dv) or (isinstance(dv, float) and isinstance(v[dk], int))):
                    cfg[k][dk] = v[dk]
        elif type(v) is type(default) or (isinstance(default, float) and isinstance(v, int)):
            cfg[k] = v
    stages = [s for s in cfg["daily"]["stages"] if s in DAILY_STAGES]
    cfg["daily"]["stages"] = stages or list(DEFAULTS["daily"]["stages"])
    return cfg
