"""Steno: live meeting capture, notes, and todos."""
from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.11.0"

def model_default() -> str:
    """STENO_MODEL wins; else the active provider's default. Flip it per run."""
    from . import claude  # noqa: PLC0415  # keep httpx out of bare `import steno`
    return (os.environ.get("STENO_MODEL") or os.environ.get("CLAUDE_MODEL")
            or claude.default_model())


ASK_MODEL_ENV = "STENO_ASK_MODEL"


def resolve_ask_model(env: str, pref: str, default: str) -> str:
    """The model that answers a question: the environment's, else the one
    picked in the window, else `default`. A blank one is not a choice."""
    return env.strip() or pref.strip() or default


def ask_model(pref: str = "") -> str:
    """The model for a question about a meeting, given the window's pick.

    Summaries keep `model_default()`; this is only for asking, and falls
    back to it when nobody has chosen.
    """
    return resolve_ask_model(
        os.environ.get(ASK_MODEL_ENV, ""), pref, model_default())


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
