"""What a meeting's notes job is doing, and how it ended.

A small vocabulary shared by the engine's NotesStatus event and the
window's wording. The engine keeps at most one live task per meeting; a
second ask joins that task rather than starting a second summary.
"""
from __future__ import annotations

import enum


class NotesPhase(enum.StrEnum):
    """What a running job is doing, in pipeline order."""

    TRANSCRIBING = "transcribing"
    SUMMARIZING = "summarizing"


class NotesState(enum.StrEnum):
    """Whether the job is still going, and how it ended."""

    RUNNING = "running"
    WRITTEN = "written"
    FAILED = "failed"
