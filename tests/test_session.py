"""The engine's lifecycle: what counts as busy, and on-request summaries."""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from steno import session
from steno.events import EngineError, Finalizing, MeetingEnded, SummaryReady
from steno.summarize import Summary


def make_engine(root: Path) -> tuple[session.Engine, list]:
    events: list = []
    engine = session.Engine(
        emit=events.append, root=root, brief_path=root / "brief.md", model="m"
    )
    return engine, events


class TestBusy(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def recording(self):
        return SimpleNamespace(
            stop=lambda: None, transcript_jsonl=None,
            started_at=time.time() - 60, session_dir=self.root, app="zoom",
        )

    def test_a_meeting_is_never_neither_recording_nor_busy(self):
        """Quitting must wait for the summary, not just for the recording.

        `current` is None the moment a meeting ends; `busy` has to cover the
        gap before and during the batch pass, or a quit cancels the notes.
        """
        engine, _events = make_engine(self.root)
        gate: asyncio.Event
        seen: list[bool] = []

        async def scenario():
            nonlocal gate
            gate = asyncio.Event()

            async def finish(_rec):
                seen.append(engine.busy)
                await gate.wait()

            engine._finish = finish
            engine.current = self.recording()
            ended = asyncio.create_task(engine._end())
            await asyncio.sleep(0.05)  # inside _end's settle sleep
            seen.append(engine.current is None and engine.busy)
            await ended
            await asyncio.sleep(0)  # let the finisher start
            # `_end` has returned while the meeting is still being finished.
            seen.append(engine.busy)
            gate.set()
            await asyncio.gather(*engine._finishers)
            seen.append(engine.busy)

        asyncio.run(scenario())
        self.assertEqual(seen, [True, True, True, False])

    def test_a_failed_finish_is_reported_and_clears_busy(self):
        engine, events = make_engine(self.root)

        async def finish(_rec):
            msg = "boom"
            raise RuntimeError(msg)

        async def scenario():
            engine._finish = finish
            engine.current = self.recording()
            await engine._end()
            await asyncio.gather(*engine._finishers)

        asyncio.run(scenario())
        self.assertFalse(engine.busy)
        errors = [e for e in events if isinstance(e, EngineError)]
        self.assertEqual([e.where for e in errors], ["summary"])

    def test_a_failure_in_a_step_does_not_end_the_engine(self):
        """A full disk at the start of one meeting must not kill detection."""
        engine, events = make_engine(self.root)

        async def start_fails(_app, **_kw):
            msg = "No space left on device"
            raise OSError(msg)

        engine._start = start_fails
        with self.assertRaises(OSError):
            asyncio.run(engine._on_command("start"))
        # The guarded path is what the loops use.
        asyncio.run(engine._guarded(engine._on_command, "start"))
        errors = [e for e in events if isinstance(e, EngineError)]
        self.assertEqual([e.where for e in errors], ["engine"])
        self.assertTrue(errors[0].fatal)


class TestSummarizeNow(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dir = self.root / "20260101-100000"
        self.dir.mkdir()

    def test_emits_a_requested_summary(self):
        engine, events = make_engine(self.root)
        busy: list[bool] = []

        async def fake(_dir, _model, _brief):
            busy.append(engine.busy)
            return Summary(title="Plan", body="# Plan\n\n- ok")

        with mock.patch.object(session, "summarize_session", fake):
            asyncio.run(engine.summarize_now(self.dir))
        self.assertEqual(busy, [True])
        self.assertFalse(engine.busy)
        self.assertIsInstance(events[0], Finalizing)
        ready = next(e for e in events if isinstance(e, SummaryReady))
        self.assertTrue(ready.requested)
        self.assertEqual(ready.title, "Plan")

    def test_failure_is_reported_not_raised(self):
        engine, events = make_engine(self.root)

        async def fake(_dir, _model, _brief):
            msg = "host down"
            raise RuntimeError(msg)

        with mock.patch.object(session, "summarize_session", fake):
            asyncio.run(engine.summarize_now(self.dir))
        errors = [e for e in events if isinstance(e, EngineError)]
        self.assertEqual([e.where for e in errors], ["summary"])
        self.assertIn("host down", errors[0].message)
        self.assertFalse(engine.busy)
        self.assertFalse(any(isinstance(e, MeetingEnded) for e in events))


if __name__ == "__main__":
    unittest.main()
