"""The engine's lifecycle: what counts as busy, on-request summaries, asking."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from steno import session
from steno.events import (
    AdviceDone,
    EngineError,
    MeetingEnded,
    NotesStatus,
    SummaryReady,
)
from steno.notesjob import NotesState
from steno.summarize import Summary, load_meta


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
            keyterms=[],
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

            async def pipeline(_dir, _job, _rec):
                seen.append(engine.busy)
                await gate.wait()

            engine._notes_pipeline = pipeline
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

    def test_a_new_meeting_is_marked_pending_from_the_start(self):
        """A crash mid-meeting leaves your side untranscribed; retention must
        not read that as finished."""
        session._rewrite_meta(self.root, 1.0, "zoom", False)
        self.assertTrue(load_meta(self.root)["mic_pending"])

    def test_pending_clears_only_once_your_lines_are_on_disk(self):
        engine, _events = make_engine(self.root)
        (self.root / "you.wav").write_bytes(b"\0")
        (self.root / "transcript.jsonl").write_text(
            '{"ts": 5.0, "speaker": "them", "text": "hi"}\n'
        )
        session._rewrite_meta(self.root, 1.0, "zoom", False)
        engine.summarize = False
        seen: list[bool] = []

        async def mic_pass(_dir, _started, _keyterms):
            return [{"ts": 6.0, "speaker": "you", "text": "hello"}], True

        real_write = session.write_transcripts

        def write(d, records):
            seen.append(bool(load_meta(d).get("mic_pending")))  # still set here
            real_write(d, records)

        engine._mic_pass = mic_pass
        with mock.patch.object(session, "write_transcripts", write):
            asyncio.run(engine._run_notes_job(self.root, rec=self.recording()))
        self.assertEqual(seen, [True])
        self.assertNotIn("mic_pending", load_meta(self.root))

    def test_a_failed_mic_pass_leaves_it_pending(self):
        engine, _events = make_engine(self.root)
        (self.root / "you.wav").write_bytes(b"\0")
        (self.root / "transcript.jsonl").write_text(
            '{"ts": 5.0, "speaker": "them", "text": "hi"}\n'
        )
        session._rewrite_meta(self.root, 1.0, "zoom", False)
        engine.summarize = False

        async def mic_pass(_dir, _started, _keyterms):
            return [], False

        engine._mic_pass = mic_pass
        asyncio.run(engine._run_notes_job(self.root, rec=self.recording()))
        self.assertTrue(load_meta(self.root)["mic_pending"])

    def test_a_failed_finish_is_reported_and_clears_busy(self):
        engine, events = make_engine(self.root)

        async def pipeline(_dir, _job, _rec):
            msg = "boom"
            raise RuntimeError(msg)

        async def scenario():
            engine._notes_pipeline = pipeline
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


class TestStartFailure(unittest.TestCase):
    def test_a_failure_after_capture_opened_leaves_no_phantom_recording(self):
        """`current` set with nothing capturing made every later detection
        'already recording' until someone pressed Stop."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        engine, events = make_engine(root)

        async def capture():
            return "mic"

        def boom(_app=""):
            msg = "bad brief"
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, msg)

        with mock.patch.object(session, "capture_mic", capture), \
             mock.patch.object(session, "default_sink_monitor", capture), \
             mock.patch.object(engine, "_load_keyterms", boom):
            asyncio.run(engine._guarded(engine._start, "zoom"))
        self.assertIsNone(engine.current)
        self.assertFalse(engine.busy)
        self.assertTrue(any(isinstance(e, EngineError) for e in events))


class TestSummarizeNow(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dir = self.root / "20260101-100000"
        self.dir.mkdir()
        (self.dir / "transcript.jsonl").write_text(
            '{"ts": 5.0, "speaker": "them", "text": "hi"}\n'
        )

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
        running = next(e for e in events if isinstance(e, NotesStatus))
        self.assertIs(running.state, NotesState.RUNNING)
        self.assertFalse(running.joined)
        ready = next(e for e in events if isinstance(e, SummaryReady))
        self.assertTrue(ready.requested)
        self.assertEqual(ready.title, "Plan")

    def test_a_live_job_is_joined_not_summarized_twice(self):
        engine, events = make_engine(self.root)
        engine._notes.begin(
            self.dir, now=time.time(), requested=False, stall_after_s=60.0
        )
        called = []

        async def fake(*_a):
            called.append(1)
            return Summary(title="x", body="# x")

        with mock.patch.object(session, "summarize_session", fake):
            asyncio.run(engine.summarize_now(self.dir))
        self.assertEqual(called, [])
        (status,) = [e for e in events if isinstance(e, NotesStatus)]
        self.assertIs(status.state, NotesState.RUNNING)
        self.assertTrue(status.joined)  # the page says "already writing"

    def test_force_replaces_a_live_job(self):
        engine, events = make_engine(self.root)
        engine._notes.begin(
            self.dir, now=time.time(), requested=False, stall_after_s=60.0
        )
        called = []

        async def fake(*_a):
            called.append(1)
            return Summary(title="x", body="# x")

        with mock.patch.object(session, "summarize_session", fake):
            asyncio.run(engine.summarize_now(self.dir, force=True))
        self.assertEqual(called, [1])
        statuses = [e for e in events if isinstance(e, NotesStatus)]
        self.assertEqual(statuses[0].generation, 2)
        self.assertFalse(statuses[0].joined)
        self.assertTrue(any(isinstance(e, SummaryReady) for e in events))

    def test_a_stalled_job_is_replaced_without_force(self):
        engine, events = make_engine(self.root)
        engine.notes_timeout_s = 30.0
        engine._notes.begin(
            self.dir, now=time.time() - 1000, requested=False, stall_after_s=30.0
        )
        called = []

        async def fake(*_a):
            called.append(1)
            return Summary(title="x", body="# x")

        with mock.patch.object(session, "summarize_session", fake):
            asyncio.run(engine.summarize_now(self.dir))
        self.assertEqual(called, [1])
        self.assertTrue(any(isinstance(e, SummaryReady) for e in events))

    def test_an_orphaned_meeting_gets_its_mic_pass_first(self):
        engine, events = make_engine(self.root)
        (self.dir / "you.wav").write_bytes(b"\0")
        (self.dir / "transcript.jsonl").write_text(
            '{"ts": 5.0, "speaker": "them", "text": "hi"}\n'
        )
        session._rewrite_meta(self.dir, 1.0, "zoom", False)
        order = []

        async def mic(_dir, _started, _kt):
            order.append("mic")
            return [{"ts": 6.0, "speaker": "you", "text": "ok"}], True

        async def fake(*_a):
            order.append("summary")
            return Summary(title="x", body="# x")

        engine._mic_pass = mic
        with mock.patch.object(session, "summarize_session", fake):
            asyncio.run(engine.summarize_now(self.dir))
        self.assertEqual(order, ["mic", "summary"])
        self.assertNotIn("mic_pending", load_meta(self.dir))
        self.assertTrue(any(isinstance(e, SummaryReady) for e in events))

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
        failed = next(
            e for e in events
            if isinstance(e, NotesStatus) and e.state is NotesState.FAILED
        )
        self.assertIn("host down", failed.error)

    def test_a_wedged_job_is_cut_off_and_the_meeting_can_be_tried_again(self):
        engine, events = make_engine(self.root)
        engine.notes_timeout_s = 0.05

        async def hang(*_a):
            await asyncio.sleep(10)

        with mock.patch.object(session, "summarize_session", hang):
            asyncio.run(engine.summarize_now(self.dir))
        failed = next(
            e for e in events
            if isinstance(e, NotesStatus) and e.state is NotesState.FAILED
        )
        self.assertIn("gave up", failed.error)
        self.assertFalse(engine.busy)
        # The next ask is a fresh start, not a join on the dead job.
        status = engine._notes.get(self.dir)
        assert status is not None
        self.assertIs(status.state, NotesState.FAILED)


class TestAsking(unittest.TestCase):
    """A question goes to the model the window chose, not the summary's."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.engine, self.events = make_engine(Path(tmp.name))
        self.engine.transcript = [{"speaker": "them", "text": "hi", "ts": 1.0}]

    def ask(self, **kw) -> mock.AsyncMock:
        stream = mock.AsyncMock(return_value="an answer")
        with mock.patch.object(session.claude, "stream_text", stream):
            asyncio.run(self.engine.ask("what?", **kw))
        return stream

    def test_it_uses_the_model_it_was_given(self):
        stream = self.ask(model="picked")
        self.assertEqual(stream.call_args.kwargs["model"], "picked")

    def test_without_one_it_uses_the_engines_own(self):
        stream = self.ask()
        self.assertEqual(stream.call_args.kwargs["model"], "m")

    def test_the_summary_model_is_not_moved_by_a_question(self):
        self.ask(model="picked")
        self.assertEqual(self.engine.model, "m")

    def test_the_answer_is_marked_as_a_models(self):
        self.ask(model="picked")
        self.assertEqual(self.events[-1], AdviceDone("an answer"))

    def test_the_engines_own_word_is_not(self):
        self.engine.transcript = []
        self.ask(model="picked")
        (done,) = [e for e in self.events if isinstance(e, AdviceDone)]
        self.assertFalse(done.by_model)

    def fail(self, **kw) -> None:
        stream = mock.AsyncMock(
            side_effect=session.claude.ClaudeError("no route")
        )
        with mock.patch.object(session.claude, "stream_text", stream):
            asyncio.run(self.engine.ask("what?", **kw))

    def test_a_failure_names_the_model_that_was_asked(self):
        """A mistyped id comes back as the provider's words, which may
        not say which model it was."""
        self.fail(model="mimo-typo")
        (error,) = [e for e in self.events if isinstance(e, EngineError)]
        self.assertEqual(error.where, "advisor")
        self.assertEqual(error.message, "mimo-typo did not answer: no route")

    def test_a_failure_of_the_default_names_it_too(self):
        self.fail()
        (error,) = [e for e in self.events if isinstance(e, EngineError)]
        self.assertIn("m did not answer", error.message)

    def test_a_stream_that_dies_mid_answer_ends_as_failed_after_its_text(self):
        """Through the real client: some text arrives, then no [DONE]."""
        body = 'data: {"choices": [{"delta": {"content": "Sign on"}}]}\n\n'
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(200, content=body.encode())
        )
        with mock.patch.object(session.claude, "_transport", transport), \
             mock.patch.dict(os.environ, {"STENO_API_KEY": "sk-test"}):
            asyncio.run(self.engine.ask("what?", model="picked"))
        kinds = [type(e).__name__ for e in self.events]
        self.assertEqual(kinds, ["AdviceDelta", "EngineError", "AdviceDone"])
        self.assertEqual(self.events[0].text, "Sign on")
        self.assertIn("picked did not answer", self.events[1].message)
        self.assertEqual(self.events[2], AdviceDone("", failed=True))

    def test_a_failed_answer_is_ended_as_failed(self):
        self.fail(model="picked")
        self.assertEqual(self.events[-1], AdviceDone("", failed=True))
        self.assertFalse(AdviceDone("an answer").failed)


if __name__ == "__main__":
    unittest.main()
