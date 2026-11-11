"""Where home is, for the weather and for finding a business's number "near home".

Kept in config/home.json on your own Mac (never in the code). Copy config/home.example.json and fill it in.
"""
from __future__ import annotations

import json
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "config" / "home.json"
DEFAULT = {"name": "", "lat": 40.7128, "lon": -74.0060, "tz": "America/New_York"}


def load() -> dict:
    try:
        return DEFAULT | json.loads(FILE.read_text())
    except (OSError, ValueError):
        return dict(DEFAULT)
