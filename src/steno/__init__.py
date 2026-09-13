"""Steno: live meeting capture, notes, and todos."""
from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.11.0"

# All current Claude models are natively 1M context, so no beta header is needed.
# Summaries and the advisor share this; override per-run with CLAUDE_MODEL.
# Passed to `claude -p --model`, which takes full IDs and aliases alike.
DEFAULT_MODEL = "claude-opus-5"

LEGACY_NAME = "meeting-copilot"
APP_NAME = "steno"


def icon_dir() -> Path:
    """The icon theme shipped inside the package: the app icon and tray states."""
    return Path(__file__).parent / "data" / "icons"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def runtime_dir() -> Path:
    """Where the running app publishes its state for the bar to read.

    `XDG_RUNTIME_DIR` is per-login and wiped on logout, which is what we want:
    a state file left behind by a killed process must never be believed.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/{APP_NAME}-{os.getuid()}"
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def migrate_legacy() -> list[tuple[Path, Path]]:
    """Rename pre-`steno` config and data directories into place.

    A rename, not a copy: the archive is gigabytes of audio and duplicating it
    to rename a project would be absurd. Only ever moves onto a path that does
    not exist, so running this twice — or after the user moved things by hand —
    cannot clobber anything.
    """
    moved = []
    for new in (config_dir(), data_dir()):
        old = new.parent / LEGACY_NAME
        if old.is_dir() and not new.exists():
            try:
                old.rename(new)
            except OSError:
                continue
            moved.append((old, new))
    return moved
