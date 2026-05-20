"""Live meeting copilot."""
from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.2.0"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "meeting-copilot"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "meeting-copilot"
