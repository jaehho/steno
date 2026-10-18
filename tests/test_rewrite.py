"""When the window rewrites a meeting's project notes.

The debounce runs on a clock and a timer that only move when the test
says, so a minute costs nothing. The pages are written for real, to a
temp projects root; no test reads ~/projects or opens a window.
"""
from __future__ import annotations

import itertools
import json
import os
import shutil
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from gi.repository import GLib

from steno import projectnotes
from steno.gui.rewrite import DELAY_S, Debounce, Rewriter
from tests.test_projectnotes import SUMMARY_NAME, TODOS, make_session


class FakeTime:
    """A clock and a timer in one: `advance` runs what has come due."""

    def __init__(self) -> None:
        self.now = 0.0
        self.timers: dict[int, tuple[float, object]] = {}
        self._ids = itertools.count(1)

    def __call__(self) -> float:
        return self.now

    def schedule(self, seconds: float, call) -> int:
        timer = next(self._ids)
        self.timers[timer] = (self.now + seconds, call)
        return timer

    def cancel(self, timer: int) -> None:
        del self.timers[timer]  # KeyError: it was cancelled after it ran

    def advance(self, seconds: float) -> None:
        end = self.now + seconds
        while due := sorted(
            (when, timer) for timer, (when, _) in self.timers.items()
            if when <= end
        ):
            when, timer = due[0]
            self.now = max(self.now, when)
            self.timers.pop(timer)[1]()
        self.now = end

    @property
    def timing(self) -> dict:
        return {"clock": self, "schedule": self.schedule,
                "cancel": self.cancel}


class TestDebounce(unittest.TestCase):
    def setUp(self):
        self.time = FakeTime()
        self.ran: list[tuple[float, str]] = []
        self.debounce = Debounce(
            lambda key: self.ran.append((self.time.now, key.name)),
            DELAY_S, **self.time.timing,
        )
        self.a, self.b = Path("a"), Path("b")

    def test_it_runs_once_the_delay_has_passed_and_not_before(self):
        self.debounce.touch(self.a)
        self.time.advance(DELAY_S - 1)
        self.assertEqual(self.ran, [])
        self.time.advance(1)
        self.assertEqual(self.ran, [(DELAY_S, "a")])
        self.time.advance(10 * DELAY_S)
        self.assertEqual(len(self.ran), 1)

    def test_every_touch_moves_the_deadline(self):
        for _ in range(5):
            self.debounce.touch(self.a)
            self.time.advance(DELAY_S - 10)
        self.assertEqual(self.ran, [])
        self.time.advance(10)
        self.assertEqual([when for when, _ in self.ran], [self.time.now])
        self.assertEqual(self.time.now, 5 * (DELAY_S - 10) + 10)

    def test_meetings_wait_on_their_own(self):
        self.debounce.touch(self.a)
        self.time.advance(30)
        self.debounce.touch(self.b)
        self.time.advance(30)
        self.assertEqual(self.ran, [(DELAY_S, "a")])
        self.time.advance(30)
        self.assertEqual(self.ran, [(DELAY_S, "a"), (90, "b")])

    def test_flush_does_it_now_and_only_once(self):
        self.debounce.touch(self.a)
        self.debounce.touch(self.b)
        self.debounce.flush(self.a)
        self.assertEqual(self.ran, [(0, "a")])
        self.assertNotIn(self.a, self.debounce)
        self.time.advance(10 * DELAY_S)
        self.assertEqual(self.ran, [(0, "a"), (DELAY_S, "b")])

    def test_flushing_what_is_not_waiting_does_nothing(self):
        self.debounce.flush(self.a)
        self.debounce.flush()
        self.assertEqual(self.ran, [])

    def test_flush_with_no_key_does_everything(self):
        self.debounce.touch(self.a)
        self.debounce.touch(self.b)
        self.debounce.flush()
        self.assertEqual(sorted(name for _, name in self.ran), ["a", "b"])
        self.assertEqual(self.time.timers, {})

    def test_forget_drops_it_for_good(self):
        self.debounce.touch(self.a)
        self.debounce.forget(self.a)
        self.debounce.forget(self.b)
        self.time.advance(10 * DELAY_S)
        self.assertEqual(self.ran, [])
        self.assertEqual(self.time.timers, {})

    def test_there_is_one_timer_however_much_is_touched(self):
        for key in (self.a, self.b, self.a, self.b):
            self.debounce.touch(key)
            self.assertEqual(len(self.time.timers), 1)

    def test_a_failing_run_does_not_cost_the_others_theirs(self):
        def work(key: Path) -> None:
            if key == self.a:
                msg = "boom"
                raise RuntimeError(msg)
            self.ran.append((self.time.now, key.name))

        self.debounce = Debounce(work, DELAY_S, **self.time.timing)
        self.debounce.touch(self.a)
        self.debounce.touch(self.b)
        with self.assertLogs("steno.gui.rewrite", "ERROR"):
            self.time.advance(DELAY_S)
        self.assertEqual(self.ran, [(DELAY_S, "b")])


class TestRewriter(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.projects = base / "projects"
        (self.projects / "demo").mkdir(parents=True)
        self.sessions = base / "sessions"
        self.sessions.mkdir()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(projectnotes.SWITCH_ENV, None)
        self.time = FakeTime()
        self.problems: list[str] = []
        self.rp = Rewriter(
            self.problems.append,
            write=lambda d: projectnotes.try_write(d, self.projects),
            **self.time.timing,
        )
        self.d = make_session(self.sessions)

    def write_pages(self) -> None:
        projectnotes.write(self.d, self.projects)

    def page(self) -> str:
        return (self.projects / "demo" / "notes" / "meetings"
                / SUMMARY_NAME).read_text()

    def tick(self) -> None:
        """A to-do ticked: the second one, which the page shows open."""
        done = [dict(t, done=True) for t in TODOS]
        self.d.joinpath("todos.json").write_text(json.dumps(done))

    def settle(self) -> None:
        """Let the writes finish and their reports reach the main loop."""
        self.rp.wait(10)
        context = GLib.MainContext.default()
        # PyGObject's loop hook trips over asyncio's policies on 3.14.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            while context.iteration(False):
                pass

    def test_an_edit_does_not_put_a_meeting_into_a_project(self):
        self.rp.changed(self.d)
        self.assertEqual(self.time.timers, {})
        self.time.advance(10 * DELAY_S)
        self.settle()
        self.assertFalse((self.projects / "demo" / "notes").exists())

    def test_a_meeting_in_a_project_follows_its_edits_after_a_pause(self):
        self.write_pages()
        self.assertIn("- [ ] Draft the comparison table", self.page())
        self.tick()
        self.rp.changed(self.d)
        self.time.advance(DELAY_S - 1)
        self.settle()
        self.assertIn("- [ ] Draft the comparison table", self.page())
        self.time.advance(1)
        self.settle()
        self.assertIn("- [x] Draft the comparison table", self.page())

    def test_a_change_that_leaves_the_pages_as_they_are_writes_nothing(self):
        self.write_pages()
        folder = self.projects / "demo" / "notes" / "meetings"
        before = {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}
        self.rp.changed(self.d)
        self.time.advance(DELAY_S)
        self.settle()
        after = {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}
        self.assertEqual(after, before)

    def test_leaving_the_meeting_writes_what_is_waiting_at_once(self):
        self.write_pages()
        self.tick()
        self.rp.changed(self.d)
        self.rp.leave(self.d)
        self.settle()
        self.assertIn("- [x] Draft the comparison table", self.page())
        self.assertEqual(self.time.timers, {})

    def test_leaving_a_meeting_with_nothing_waiting_does_nothing(self):
        self.write_pages()
        self.tick()  # on disk, but nobody said it changed
        self.rp.leave(self.d)
        self.settle()
        self.assertIn("- [ ] Draft the comparison table", self.page())

    def test_quitting_writes_everything_that_is_waiting(self):
        self.write_pages()
        other = make_session(self.sessions, "20261006-161200", title="Other")
        projectnotes.write(other, self.projects)
        self.tick()
        other.joinpath("notes.md").write_text("new thoughts\n")
        self.rp.changed(self.d)
        self.rp.changed(other)
        self.rp.shutdown(10)
        self.assertIn("- [x] Draft the comparison table", self.page())
        written = (self.projects / "demo" / "notes" / "meetings"
                   / "2026-10-06-1612-other.md").read_text()
        self.assertIn("new thoughts", written)

    def test_a_meeting_that_is_going_is_not_written_again(self):
        self.write_pages()
        self.tick()
        self.rp.changed(self.d)
        self.rp.forget(self.d)
        self.time.advance(10 * DELAY_S)
        self.settle()
        self.assertIn("- [ ] Draft the comparison table", self.page())

    def test_write_now_takes_the_wait_off_and_reports_back(self):
        self.tick()
        self.rp.changed(self.d)  # not in a project yet: nothing waits
        got: list[projectnotes.Result] = []
        self.rp.write(self.d, then=got.append)
        self.settle()
        self.assertIn("- [x] Draft the comparison table", self.page())
        self.assertEqual([r.project for r in got], ["demo"])

    def test_write_now_cancels_the_wait_it_replaces(self):
        self.write_pages()
        self.rp.changed(self.d)
        self.rp.write(self.d)
        self.assertEqual(self.time.timers, {})
        self.settle()

    def test_a_page_left_alone_is_reported(self):
        self.write_pages()
        page = (self.projects / "demo" / "notes" / "meetings" / SUMMARY_NAME)
        page.write_text("my own notes\n")
        with self.assertLogs("steno.projectnotes", "WARNING"):
            self.rp.write(self.d)
            self.settle()
        self.assertEqual(page.read_text(), "my own notes\n")
        self.assertEqual(len(self.problems), 1)
        self.assertIn(SUMMARY_NAME, self.problems[0])

    def test_nothing_to_report_when_it_went_well_or_went_nowhere(self):
        self.rp.write(self.d)
        nowhere = make_session(
            self.sessions, "20261007-090000", project=None)
        self.rp.write(nowhere)
        self.settle()
        self.assertEqual(self.problems, [])


if __name__ == "__main__":
    unittest.main()
