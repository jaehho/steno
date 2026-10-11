"""Small things the window remembers between runs.

Kept out of `.env`: that file is for what the tool needs to work, and this is
only how the person likes it laid out. State, not config, so it lives under
`$XDG_STATE_HOME` and a missing or damaged file is simply the defaults.

No GTK import, so it can be tested without a display.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from .style import TRANSCRIPT_PT_DEFAULT, TRANSCRIPT_PT_MAX, TRANSCRIPT_PT_MIN


def prefs_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "steno" / "gui.json"


def _read() -> dict:
    try:
        raw = json.loads(prefs_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def transcript_pt() -> int:
    """The live transcript's type size. It is the captioning surface, so a
    size someone needed once should not reset every time the app restarts."""
    try:
        size = int(_read().get("transcript_pt", TRANSCRIPT_PT_DEFAULT))
    except (TypeError, ValueError):
        return TRANSCRIPT_PT_DEFAULT
    return max(TRANSCRIPT_PT_MIN, min(TRANSCRIPT_PT_MAX, size))


def save_transcript_pt(size: int) -> None:
    prefs = _read()
    prefs["transcript_pt"] = int(size)
    path = prefs_path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(prefs, indent=2) + "\n")
