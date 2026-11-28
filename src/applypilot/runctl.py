"""Cooperative run control for bounded batch stages (fork; audit R5).

``daily`` sets a deadline; long-running stage loops (verify, score, tailor, cover) call
``expired()`` between items and stop early, so ``max_runtime_minutes`` bounds the run
inside a stage instead of only between stages. Outside a daily run no deadline is set and
``expired()`` is always False.
"""
from __future__ import annotations

import threading
import time

_state = threading.local()
_global: dict[str, float | None] = {"deadline": None}


def set_deadline(monotonic_deadline: float | None) -> None:
    _global["deadline"] = monotonic_deadline


def expired() -> bool:
    d = _global["deadline"]
    return d is not None and time.monotonic() > d


def clear() -> None:
    _global["deadline"] = None
    _state.__dict__.clear()
