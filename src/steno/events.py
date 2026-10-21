"""What the engine tells the outside world.

The engine never imports GTK and never touches a widget; it emits these and a
caller decides what to do with them. That keeps every moving part — detection,
capture, transcription, summarization — testable without a display.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .notesjob import NotesPhase, NotesState


@dataclass(frozen=True)
class MeetingStarted:
    session_dir: Path
    app: str
    started_at: float
    manual: bool = False


@dataclass(frozen=True)
class Heard:
    """One utterance from the far side. Interim text is provisional and will be
    replaced by a final with the same speaker."""

    speaker: str
    text: str
    final: bool
    ts: float


@dataclass(frozen=True)
class MeetingEnded:
    session_dir: Path
    duration_s: float


@dataclass(frozen=True)
class NotesStatus:
    """What one meeting's notes job is doing, or how it ended.

    Structured so the page can tell "already running" from "just started",
    show a real error when a summary failed, and still offer the button
    after a failure — the case a free-form progress string hid.
    """

    session_dir: Path
    state: NotesState
    phase: NotesPhase | None = None
    error: str = ""
    requested: bool = False
    # This event is the reply to an ask that found work already under way.
    joined: bool = False


@dataclass(frozen=True)
class SummaryReady:
    session_dir: Path
    title: str
    body: str
    # Asked for from the window, which is open and about to show it: no
    # desktop notification needed.
    requested: bool = False


@dataclass(frozen=True)
class AdviceDelta:
    text: str


@dataclass(frozen=True)
class AdviceDone:
    text: str
    # False when `text` is the engine's own word (nothing to ask about yet),
    # not an answer: no model wrote it, so none should be named under it.
    by_model: bool = True
    # The answer stopped on an error. Text that came before it is only the
    # start of one, and is not signed as the model's answer.
    failed: bool = False


@dataclass(frozen=True)
class EngineError:
    """Something failed but the engine is still running. `fatal` marks the cases
    where a meeting's capture is compromised rather than merely degraded."""

    where: str
    message: str
    fatal: bool = False
    # The meeting it is about, when there is one: a failure for one meeting
    # must not clear another's "writing the summary".
    session_dir: Path | None = None


@dataclass(frozen=True)
class StatusChanged:
    status: str  # "idle" | "recording" | "finalizing" | "paused"
    detail: str = ""


Event = (
    MeetingStarted
    | Heard
    | MeetingEnded
    | NotesStatus
    | SummaryReady
    | AdviceDelta
    | AdviceDone
    | EngineError
    | StatusChanged
)
