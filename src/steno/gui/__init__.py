"""The window. Imports GTK, so the CLI only reaches in for the `gui` verb."""
from __future__ import annotations

from .app import APP_ID, App, run

__all__ = ["APP_ID", "App", "run"]
