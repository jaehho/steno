"""One meeting's notes job: what is running, and what came of it.

Turning a meeting into notes is slow (a batch transcription and a model
call), it can fail, and the person watching may ask for it again while it
runs. The engine keeps one job per meeting so the window can say what is
actually happening, a retry never runs beside a live job by accident, and
a late result from an abandoned job cannot overwrite a newer one.

`now` is always a parameter: this module never reads the clock.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from pathlib import Path


class NotesPhase(enum.StrEnum):
    """What the job is doing, in pipeline order."""

    TRANSCRIBING = "transcribing"
    SUMMARIZING = "summarizing"


class NotesState(enum.StrEnum):
    """Whether the job is still going, and how it ended."""

    RUNNING = "running"
    WRITTEN = "written"
    FAILED = "failed"


@dataclass(frozen=True)
class NotesJob:
    """The notes pipeline of one meeting at one generation.

    `generation` rises every time work is taken up again. A settle that
    carries an older generation is from a job that was already replaced
    and must not change what the page shows.
    """

    session_dir: Path
    generation: int
    state: NotesState
    phase: NotesPhase
    started_at: float
    beat_at: float
    requested: bool
    error: str = ""


class BeginKind(enum.StrEnum):
    """What `begin` did to the job already on this meeting, if any."""

    STARTED = "started"
    JOINED = "joined"
    SUPERSEDED = "superseded"


def is_live(job: NotesJob, *, now: float, stall_after_s: float) -> bool:
    """Whether `job` is still the one the engine should count on.

    A running job that has not moved inside `stall_after_s` is treated as
    gone: the process that owned it died, or it is wedged. The next
    `begin` then takes the meeting over instead of joining a ghost.
    """
    return (
        job.state is NotesState.RUNNING
        and (now - job.beat_at) <= stall_after_s
    )


class NotesTracker:
    """The notes job of each meeting, keyed by session directory."""

    def __init__(self) -> None:
        self._jobs: dict[Path, NotesJob] = {}

    def begin(
        self,
        session_dir: Path,
        *,
        now: float,
        requested: bool,
        force: bool = False,
        stall_after_s: float,
    ) -> tuple[BeginKind, NotesJob]:
        """Take on the notes of `session_dir`, or join the job already on it.

        A live job is joined unless `force` says to start over. A stalled
        or finished job is always replaced, so a summary that never came
        can be asked for again.
        """
        prior = self._jobs.get(session_dir)
        live = prior is not None and is_live(
            prior, now=now, stall_after_s=stall_after_s
        )
        if live and not force:
            return BeginKind.JOINED, prior
        kind = (
            BeginKind.SUPERSEDED
            if prior is not None and prior.state is NotesState.RUNNING
            else BeginKind.STARTED
        )
        generation = 1 if prior is None else prior.generation + 1
        job = NotesJob(
            session_dir=session_dir,
            generation=generation,
            state=NotesState.RUNNING,
            phase=NotesPhase.TRANSCRIBING,
            started_at=now,
            beat_at=now,
            requested=requested,
        )
        self._jobs[session_dir] = job
        return kind, job

    def beat(
        self, job: NotesJob, *, phase: NotesPhase, now: float
    ) -> NotesJob | None:
        """Move `job` to `phase`. None when the job was already replaced."""
        current = self._same(job)
        if current is None:
            return None
        updated = replace(current, phase=phase, beat_at=now)
        self._jobs[job.session_dir] = updated
        return updated

    def finish(self, job: NotesJob, *, now: float) -> bool:
        """Record that `job` wrote the notes. False when it was replaced."""
        return self._settle(job, state=NotesState.WRITTEN, now=now)

    def fail(self, job: NotesJob, error: str, *, now: float) -> bool:
        """Record that `job` failed. False when it was replaced."""
        return self._settle(job, state=NotesState.FAILED, error=error, now=now)

    def get(self, session_dir: Path) -> NotesJob | None:
        """The job on this meeting, whatever state it is in."""
        return self._jobs.get(session_dir)

    def _same(self, job: NotesJob) -> NotesJob | None:
        current = self._jobs.get(job.session_dir)
        if current is None or current.generation != job.generation:
            return None
        return current

    def _settle(
        self,
        job: NotesJob,
        *,
        state: NotesState,
        now: float,
        error: str = "",
    ) -> bool:
        current = self._same(job)
        if current is None:
            return False
        self._jobs[job.session_dir] = replace(
            current, state=state, error=error, beat_at=now
        )
        return True
