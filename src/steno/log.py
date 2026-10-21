"""A log file the app can write to when nobody is watching.

The GUI process is started with stdout and stderr on /dev/null, so a
summary that never came left no trace. This puts the notes pipeline and
the engine's lifecycle in a file that survives the process, so the next
"it said it was writing and never did" can be read instead of guessed.

Write with `logging.getLogger("steno." + name)`. The file is small and
old lines are dropped when it grows past `MAX_BYTES`.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from . import APP_NAME

MAX_BYTES = 512 * 1024
BACKUPS = 2


def log_path() -> Path:
    """Where the running app writes its log. Wiped only by a state clear."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / APP_NAME / "steno.log"


def configure(level: int = logging.INFO) -> Path:
    """Send `steno.*` loggers to `log_path()`. Returns that path.

    Idempotent: calling it again leaves the one handler in place. Safe
    from both the CLI and the GUI; never raises, since a missing
    directory must not stop a meeting from being recorded.
    """
    path = log_path()
    root = logging.getLogger("steno")
    for existing in list(root.handlers):
        if getattr(existing, "steno_file", False):
            return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS
        )
    except OSError:
        # Nowhere to write: keep going, just not with a file.
        handler = logging.NullHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    # Marked so `configure` can find its own handler and leave it alone.
    handler.steno_file = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    return path
