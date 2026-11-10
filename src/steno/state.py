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

import contextlib
import json
import os
import time
from pathlib import Path

from . import runtime_dir
from .livesrc import lease_path, read_lease

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600

STATE_FILENAME = "state.json"

# The app name, and so the published detail, of a recording the user
# started by hand rather than one a call app triggered.
MANUAL = "manual"

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
    with contextlib.suppress(OSError):
        state_path().unlink()


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
    published = {
        "status": status if status in ICONS else "off",
        "detail": str(raw.get("detail", "")),
        "since": float(raw.get("since", 0.0)),
        "session": str(raw.get("session", "")),
    }
    src = named_test_build(read_lease(lease_path()), int(raw.get("pid", 0)))
    if src:
        published["src"] = src
    return published


def named_test_build(lease: dict, pid: int) -> str:
    """The lease's `branch@sha` when it is the running app's own and a
    test build, else "". A lease left by an app that has gone, or by
    the installed checkout, says nothing."""
    if lease.get("pid") != pid or not lease.get("test_build"):
        return ""
    return str(lease.get("label", ""))


def _elapsed(since: float, now: float) -> str:
    seconds = max(0, int(now - since))
    if seconds < SECONDS_PER_MINUTE:
        return f"{seconds}s"
    if seconds < SECONDS_PER_HOUR:
        return f"{seconds // SECONDS_PER_MINUTE}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"


def bar_json(state: dict, now: float | None = None) -> dict:
    """Render published state as one waybar custom-module object.

    `text` stays short enough to live in a bar; everything else goes in the
    tooltip. `class` is what the user's stylesheet colours — the recording state
    has to be unmissable, and every other state has to be quiet.

    `class` and `manual` are read by other programs (`contract/`), so
    they are only ever added to, never renamed.

    `src` is there only while a test build is the running app (a
    worktree from STENO_SRC, or a branch other than main), so the bar
    says which build would record the next meeting.
    """
    now = time.time() if now is None else now
    status = state.get("status", "off")
    detail = state.get("detail", "")
    since = float(state.get("since", 0.0) or 0.0)
    src = state.get("src", "")

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
    if src and status != "off":
        tooltip = f"{tooltip} · test build {src}"

    out = {
        "text": text,
        "alt": status,
        "class": status,
        "tooltip": tooltip,
        "percentage": 0,
        # A recording you started rather than one a call app opened:
        # only you end it (`Engine._on_detect`).
        "manual": status == "recording" and detail == MANUAL,
    }
    if src and status != "off":
        out["src"] = src
    return out


def tray_view(status: str, detail: str = "", src: str = "") -> dict:
    """What the tray item shows and offers in a given state.

    No elapsed time: the tray is told about changes, not polled, so a counter
    would freeze at whatever it said when the state last changed.

    A test build (`src`, its `branch@sha`) names itself in the title,
    which is the line a tray host shows on hover and in its list of
    who asks for attention, and asks for attention for as long as it
    runs: a bar that greys resting icons or keeps the tray in a shut
    drawer still shows that the next meeting would be recorded by it.
    """
    recording = status == "recording"
    tooltip = bar_json({"status": status, "detail": detail})["tooltip"]
    named = f" · test build {src}" if src else ""
    return {
        "icon": TRAY_ICONS.get(status, TRAY_ICONS["idle"]),
        "title": f"Steno{named}",
        "status": "NeedsAttention" if src else "Active",
        "tooltip": tooltip,
        "status_line": f"{tooltip}{named}",
        "record_label": "Stop recording" if recording else "Start recording",
        "can_record": status != "finalizing",
        "recording": recording,
        "paused": status == "paused",
    }
