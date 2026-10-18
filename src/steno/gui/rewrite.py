"""Keeping a meeting's project notes current while the window is open.

A to-do ticked, a note typed or a transcript re-timed changes a meeting
that is already in its project's notes. A note autosaves every few
seconds and the folder may be synced, so the pages are not rewritten for
each change: they wait until the changes stop for DELAY_S, and are
written at once when the meeting is left or steno quits. A summary that
lands, a project that is set and a meeting that is deleted do not wait;
they are acts, not edits.

The rewrite runs on a thread, since a folder can be slow, and reports
back on the main loop. `projectnotes` does all the reading and writing,
and writes only what differs, so a rewrite of an unchanged meeting
touches nothing.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gi.repository import GLib

from .. import projectnotes
from . import words

log = logging.getLogger(__name__)

DELAY_S = 60.0
# How long quitting waits for a rewrite that is still running.
QUIT_WAIT_S = 20.0

Schedule = Callable[[float, Callable[[], None]], Any]
Cancel = Callable[[Any], object]


def _glib_after(seconds: float, call: Callable[[], None]) -> int:
    """Run `call` once, `seconds` from now, on the main loop."""
    def once() -> bool:
        call()
        return GLib.SOURCE_REMOVE

    return GLib.timeout_add(max(1, math.ceil(seconds * 1000)), once)


class Debounce:
    """Calls `work(key)` once nothing has touched `key` for `delay`
    seconds.

    Every touch moves the deadline, so a stream of them never gets a
    call in: only the pause after it does. `flush` is for the moments
    that cannot wait. There is one timer, set for the soonest deadline.
    The clock and the timer are arguments so that a test can run a
    minute in no time. Main thread only.
    """

    def __init__(
        self,
        work: Callable[[Path], None],
        delay: float = DELAY_S,
        *,
        clock: Callable[[], float] = time.monotonic,
        schedule: Schedule = _glib_after,
        cancel: Cancel = GLib.source_remove,
    ) -> None:
        self._work = work
        self._delay = delay
        self._clock = clock
        self._schedule = schedule
        self._cancel = cancel
        self._due: dict[Path, float] = {}
        self._timer: Any = None

    def __contains__(self, key: Path) -> bool:
        return key in self._due

    def touch(self, key: Path) -> None:
        self._due[key] = self._clock() + self._delay
        self._arm()

    def flush(self, key: Path | None = None) -> None:
        """Do now what is waiting: `key`'s, or all of it."""
        keys = list(self._due) if key is None else [key]
        ready = [k for k in keys if self._due.pop(k, None) is not None]
        self._arm()
        self._run(ready)

    def forget(self, key: Path) -> None:
        """Drop what is waiting for `key` without doing it."""
        if self._due.pop(key, None) is not None:
            self._arm()

    def _arm(self) -> None:
        if self._timer is not None:
            self._cancel(self._timer)
            self._timer = None
        if self._due:
            wait = min(self._due.values()) - self._clock()
            self._timer = self._schedule(max(0.0, wait), self._fire)

    def _fire(self) -> None:
        self._timer = None
        now = self._clock()
        ready = [k for k, due in self._due.items() if due <= now]
        for key in ready:
            del self._due[key]
        self._arm()
        self._run(ready)

    def _run(self, keys: list[Path]) -> None:
        for key in keys:
            try:
                self._work(key)
            except Exception:
                log.exception("project notes: %s", key.name)


class Rewriter:
    """What the window asks of project notes, none of it on the main
    thread.

    `on_problem(text)` hears of a page that was left alone or a write
    that failed, on the main loop. The rest of the timing arguments go
    to the `Debounce`.
    """

    def __init__(
        self,
        on_problem: Callable[[str], None] | None = None,
        *,
        write: Callable[[Path], projectnotes.Result] = projectnotes.try_write,
        **timing: Any,
    ) -> None:
        self._write = write
        self._on_problem = on_problem
        self._debounce = Debounce(self._start, **timing)
        self._workers: list[threading.Thread] = []

    def changed(self, session_dir: Path) -> None:
        """A to-do, a note or the transcript of this meeting changed: its
        pages are rewritten once things have been quiet for a minute.

        Only for a meeting that has pages in a project already. An edit
        is no reason to put a meeting into a project; that is done by
        its summary landing, by setting its project, and by `steno
        project-notes`.
        """
        if projectnotes.read_record(session_dir) is not None:
            self._debounce.touch(session_dir)

    def leave(self, session_dir: Path) -> None:
        """The meeting is put away: what is waiting for it is written
        now."""
        self._debounce.flush(session_dir)

    def write(
        self,
        session_dir: Path,
        then: Callable[[projectnotes.Result], None] | None = None,
    ) -> None:
        """Write the meeting's pages now, taking the wait off it. `then`
        is called with the outcome on the main loop."""
        self._debounce.forget(session_dir)
        self._start(session_dir, then)

    def forget(self, session_dir: Path) -> None:
        """The meeting is going: nothing more is written for it."""
        self._debounce.forget(session_dir)

    def shutdown(self, timeout: float = QUIT_WAIT_S) -> None:
        """Quitting: write everything that is waiting, and wait a while
        for what is being written."""
        self._debounce.flush()
        self.wait(timeout)

    def wait(self, timeout: float | None = None) -> None:
        """Wait for the writes that are running, `timeout` seconds in all."""
        end = None if timeout is None else time.monotonic() + timeout
        for worker in list(self._workers):
            left = None if end is None else max(0.0, end - time.monotonic())
            worker.join(left)

    def _start(
        self,
        session_dir: Path,
        then: Callable[[projectnotes.Result], None] | None = None,
    ) -> None:
        def work() -> None:
            result = self._write(session_dir)
            GLib.idle_add(self._done, result, then)

        worker = threading.Thread(target=work, name="steno-notes", daemon=True)
        self._workers = [w for w in self._workers if w.is_alive()] + [worker]
        worker.start()

    def _done(
        self,
        result: projectnotes.Result,
        then: Callable[[projectnotes.Result], None] | None,
    ) -> bool:
        if then is not None:
            then(result)
        text = words.project_notes_problem(result)
        if text and self._on_problem is not None:
            self._on_problem(text)
        return GLib.SOURCE_REMOVE
