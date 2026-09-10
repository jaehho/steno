"""What the engine tells the outside world.

The engine never imports GTK and never touches a widget; it emits these and a
caller decides what to do with them. That keeps every moving part — detection,
capture, transcription, summarization — testable without a display.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
class Finalizing:
    """Post-meeting work is running: batch transcription, then the summary."""

    session_dir: Path
    step: str


@dataclass(frozen=True)
class SummaryReady:
    session_dir: Path
    title: str
    body: str


@dataclass(frozen=True)
class AdviceDelta:
    text: str


@dataclass(frozen=True)
class AdviceDone:
    text: str


@dataclass(frozen=True)
class EngineError:
    """Something failed but the engine is still running. `fatal` marks the cases
    where a meeting's capture is compromised rather than merely degraded."""

    where: str
    message: str
    fatal: bool = False


@dataclass(frozen=True)
class StatusChanged:
    status: str  # "idle" | "recording" | "finalizing" | "paused"
    detail: str = ""


Event = (
    MeetingStarted
    | Heard
    | MeetingEnded
    | Finalizing
    | SummaryReady
    | AdviceDelta
    | AdviceDone
    | EngineError
    | StatusChanged
)
