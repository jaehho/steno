"""The notes-job tracker: begin, join, supersede, settle, and stale takeover."""
from __future__ import annotations

import unittest
from pathlib import Path

from steno.notesjob import (
    BeginKind,
    NotesPhase,
    NotesState,
    NotesTracker,
    is_live,
)

STALE = 60.0


class TestBegin(unittest.TestCase):
    def setUp(self):
        self.tracker = NotesTracker()
        self.dir = Path("/tmp/meeting")

    def test_a_first_ask_starts_generation_one(self):
        kind, job = self.tracker.begin(
            self.dir, now=100.0, requested=True, stall_after_s=STALE
        )
        self.assertIs(kind, BeginKind.STARTED)
        self.assertEqual(job.generation, 1)
        self.assertIs(job.state, NotesState.RUNNING)
        self.assertIs(job.phase, NotesPhase.TRANSCRIBING)
        self.assertTrue(job.requested)

    def test_a_live_job_is_joined_not_started_again(self):
        _, first = self.tracker.begin(
            self.dir, now=100.0, requested=False, stall_after_s=STALE
        )
        kind, second = self.tracker.begin(
            self.dir, now=110.0, requested=True, stall_after_s=STALE
        )
        self.assertIs(kind, BeginKind.JOINED)
        self.assertEqual(second.generation, first.generation)

    def test_force_takes_the_meeting_away_from_a_live_job(self):
        self.tracker.begin(self.dir, now=100.0, requested=False, stall_after_s=STALE)
        kind, job = self.tracker.begin(
            self.dir, now=110.0, requested=True, force=True, stall_after_s=STALE
        )
        self.assertIs(kind, BeginKind.SUPERSEDED)
        self.assertEqual(job.generation, 2)

    def test_a_stalled_job_is_replaced_without_force(self):
        self.tracker.begin(self.dir, now=100.0, requested=False, stall_after_s=STALE)
        kind, job = self.tracker.begin(
            self.dir, now=100.0 + STALE + 1, requested=True, stall_after_s=STALE
        )
        self.assertIs(kind, BeginKind.SUPERSEDED)
        self.assertEqual(job.generation, 2)

    def test_a_finished_job_is_replaced(self):
        _, first = self.tracker.begin(
            self.dir, now=100.0, requested=False, stall_after_s=STALE
        )
        self.tracker.finish(first, now=120.0)
        kind, job = self.tracker.begin(
            self.dir, now=130.0, requested=True, stall_after_s=STALE
        )
        self.assertIs(kind, BeginKind.STARTED)
        self.assertEqual(job.generation, 2)

    def test_a_failed_job_is_replaced(self):
        _, first = self.tracker.begin(
            self.dir, now=100.0, requested=False, stall_after_s=STALE
        )
        self.tracker.fail(first, "host down", now=120.0)
        kind, job = self.tracker.begin(
            self.dir, now=130.0, requested=True, stall_after_s=STALE
        )
        self.assertIs(kind, BeginKind.STARTED)
        self.assertEqual(job.generation, 2)


class TestSettle(unittest.TestCase):
    def setUp(self):
        self.tracker = NotesTracker()
        self.dir = Path("/tmp/meeting")
        _, self.job = self.tracker.begin(
            self.dir, now=100.0, requested=True, stall_after_s=STALE
        )

    def test_finish_records_written(self):
        self.assertTrue(self.tracker.finish(self.job, now=120.0))
        current = self.tracker.get(self.dir)
        assert current is not None
        self.assertIs(current.state, NotesState.WRITTEN)

    def test_fail_keeps_the_reason(self):
        self.assertTrue(self.tracker.fail(self.job, "host down", now=120.0))
        current = self.tracker.get(self.dir)
        assert current is not None
        self.assertIs(current.state, NotesState.FAILED)
        self.assertEqual(current.error, "host down")

    def test_a_superseded_job_cannot_settle_the_new_one(self):
        self.tracker.begin(
            self.dir,
            now=110.0,
            requested=True,
            force=True,
            stall_after_s=STALE,
        )
        self.assertFalse(self.tracker.finish(self.job, now=120.0))
        self.assertFalse(self.tracker.fail(self.job, "late", now=120.0))
        current = self.tracker.get(self.dir)
        assert current is not None
        self.assertEqual(current.generation, 2)
        self.assertIs(current.state, NotesState.RUNNING)

    def test_beat_moves_the_phase_and_keeps_the_generation(self):
        updated = self.tracker.beat(
            self.job, phase=NotesPhase.SUMMARIZING, now=150.0
        )
        assert updated is not None
        self.assertIs(updated.phase, NotesPhase.SUMMARIZING)
        self.assertEqual(updated.generation, self.job.generation)
        self.assertEqual(updated.beat_at, 150.0)

    def test_beat_on_a_replaced_job_changes_nothing(self):
        self.tracker.begin(
            self.dir,
            now=110.0,
            requested=True,
            force=True,
            stall_after_s=STALE,
        )
        self.assertIsNone(
            self.tracker.beat(self.job, phase=NotesPhase.SUMMARIZING, now=120.0)
        )


class TestIsLive(unittest.TestCase):
    def test_running_inside_the_window_is_live(self):
        _, job = NotesTracker().begin(
            Path("/m"), now=100.0, requested=True, stall_after_s=STALE
        )
        self.assertTrue(is_live(job, now=100.0 + STALE, stall_after_s=STALE))

    def test_running_past_the_window_is_not(self):
        _, job = NotesTracker().begin(
            Path("/m"), now=100.0, requested=True, stall_after_s=STALE
        )
        self.assertFalse(
            is_live(job, now=100.0 + STALE + 1, stall_after_s=STALE)
        )

    def test_a_settled_job_is_never_live(self):
        tracker = NotesTracker()
        _, job = tracker.begin(
            Path("/m"), now=100.0, requested=True, stall_after_s=STALE
        )
        tracker.finish(job, now=101.0)
        current = tracker.get(Path("/m"))
        assert current is not None
        self.assertFalse(is_live(current, now=101.0, stall_after_s=STALE))


if __name__ == "__main__":
    unittest.main()
