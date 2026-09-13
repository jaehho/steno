"""What the running app publishes about itself, and how a bar renders it.

The window is usually closed — the app is a background listener — so the status
bar is the primary place the user learns that something is being recorded.
PRODUCT.md promises recording state is always visible; this file is how that
promise is kept when there is nothing on screen to see.

Deliberately a plain file rather than a socket or a bus service: the reader is a
one-second poll from a bar, and a file that can be `cat`-ed is easier to trust
and to debug than a protocol. No GUI or engine import, so it is testable and the
`bar` command costs nothing to start.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import runtime_dir

STATE_FILENAME = "state.json"

# One icon per state. Nerd Font glyphs, matching the rest of the user's bar.
ICONS = {
    "recording": "󰑊",
    "finalizing": "󰔟",
    "paused": "󰏤",
    "idle": "󰍬",
    "off": "󰍭",
}

WORDS = {
    "recording": "Recording",
    "finalizing": "Finishing up",
    "paused": "Paused",
    "idle": "Listening for a meeting",
    "off": "Steno is not running",
}

PAUSE_CHOICES = ((30, "For 30 minutes"), (60, "For 1 hour"), (240, "For 4 hours"))

# Our own icons (`steno.icon_dir()`), handed to the tray host by path: a stock
# theme name can be missing from whatever theme the host happens to use.
TRAY_ICONS = {
    "recording": "steno-recording",
    "finalizing": "steno-finalizing",
    "paused": "steno-paused",
    "idle": "steno-idle",
}


def state_path() -> Path:
    return runtime_dir() / STATE_FILENAME


def write_state(
    status: str, detail: str = "", since: float | None = None, session: str = ""
) -> None:
    """Publish the app's state. Best-effort: never break a meeting over a bar."""
    payload = {
        "status": status,
        "detail": detail,
        "since": since if since is not None else time.time(),
        "session": session,
        "pid": os.getpid(),
    }
    try:
        tmp = state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(payload) + "\n")
        tmp.replace(state_path())  # atomic: a reader never sees half a file
    except OSError:
        pass


def clear_state() -> None:
    """Remove our own state on the way out.

    Only if we are the process that wrote it: a second instance shutting down
    must not erase the state of the one still recording, which would leave the
    bar claiming nothing is happening while the mic is open.
    """
    try:
        raw = json.loads(state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(raw, dict) and int(raw.get("pid", 0)) != os.getpid():
        return
    try:
        state_path().unlink()
    except OSError:
        pass


def _alive(pid: int) -> bool:
    """Is the app that wrote this still running?

    A crashed app leaves its last state on disk, and a bar cheerfully reporting
    `Recording` when nothing is recording is worse than reporting nothing.
    """
    if pid <= 0:
        return False
    return Path(f"/proc/{pid}").is_dir()


def read_state() -> dict:
    """The published state, or `off` when nothing trustworthy is on disk."""
    try:
        raw = json.loads(state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {"status": "off"}
    if not isinstance(raw, dict) or not _alive(int(raw.get("pid", 0))):
        return {"status": "off"}
    status = str(raw.get("status", "off"))
    return {
        "status": status if status in ICONS else "off",
        "detail": str(raw.get("detail", "")),
        "since": float(raw.get("since", 0.0)),
        "session": str(raw.get("session", "")),
    }


def _elapsed(since: float, now: float) -> str:
    seconds = max(0, int(now - since))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"


def bar_json(state: dict, now: float | None = None) -> dict:
    """Render published state as one waybar custom-module object.

    `text` stays short enough to live in a bar; everything else goes in the
    tooltip. `class` is what the user's stylesheet colours — the recording state
    has to be unmissable, and every other state has to be quiet.
    """
    now = time.time() if now is None else now
    status = state.get("status", "off")
    detail = state.get("detail", "")
    since = float(state.get("since", 0.0) or 0.0)

    text = ""
    if status == "recording" and since:
        text = _elapsed(since, now)
    elif status == "finalizing":
        text = "···"

    tooltip = WORDS.get(status, status)
    if status == "recording" and detail:
        tooltip = f"Recording · {detail}"
        if since:
            tooltip += f" · {_elapsed(since, now)}"
    elif status == "paused" and since:
        left = max(0, int(since - now))
        tooltip = f"Paused · {left // 60}m left" if left else "Paused"
    elif detail:
        tooltip = f"{tooltip} · {detail}"

    return {
        "text": text,
        "alt": status,
        "class": status,
        "tooltip": tooltip,
        "percentage": 0,
    }


def tray_view(status: str, detail: str = "") -> dict:
    """What the tray item shows and offers in a given state.

    No elapsed time: the tray is told about changes, not polled, so a counter
    would freeze at whatever it said when the state last changed.
    """
    recording = status == "recording"
    return {
        "icon": TRAY_ICONS.get(status, TRAY_ICONS["idle"]),
        "tooltip": bar_json({"status": status, "detail": detail})["tooltip"],
        "record_label": "Stop recording" if recording else "Start recording",
        "can_record": status != "finalizing",
        "recording": recording,
        "paused": status == "paused",
    }
