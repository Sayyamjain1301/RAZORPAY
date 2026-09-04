"""Tiny JSON-file persistence for everything that needs to survive across
Streamlit reruns and process restarts, but doesn't need a real database at
this scale: rule configs, ownership, comments, saved views, run history,
certifications, first-seen timestamps.

Explicitly out of scope, and said so rather than faked: this is single-user,
local-file state, not a multi-tenant backend. Role-based approval chains and
ownership assignment below are real state machines enforced in code — but
"real user B on a different machine" isn't a thing this file can model.
"""
from __future__ import annotations

import json
import os
import threading

_LOCK = threading.Lock()


def _path(data_dir: str, name: str) -> str:
    return os.path.join(data_dir, f"_state_{name}.json")


def load(data_dir: str, name: str, default):
    p = _path(data_dir, name)
    if not os.path.exists(p):
        return default
    with _LOCK:
        try:
            with open(p) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default


def save(data_dir: str, name: str, value) -> None:
    p = _path(data_dir, name)
    os.makedirs(data_dir, exist_ok=True)
    with _LOCK:
        with open(p, "w") as f:
            json.dump(value, f, indent=2, default=str)
