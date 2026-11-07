"""The live view never streams a room's microphone.

With the window open, the engine streams the microphone to Deepgram so
the user sees their own words live. In a room the microphone is everyone
there, and room mode promises nothing of the room goes to Deepgram until
the user says everyone agreed. A recording started by hand may be a
room until its far side speaks (`room.is_room`), so until then its
microphone is not streamed. The microphone and the stream are fakes:
nothing is captured or sent.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steno import session
from steno.transcribe import Utterance


class TestLiveView(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.streamed: list[str] = []

    async def fake_stream(self, speaker, _source, _queue, **_kw):
        """A stream runs until it is stopped."""
        self.streamed.append(speaker)
        await asyncio.Event().wait()

    def run_engine(self, *, manual: bool, heard: list[Utterance]) -> None:
        """A recording with the window open, the far side saying `heard`."""
        async def main() -> None:
            engine = session.Engine(
                emit=lambda _e: None, root=self.root,
                brief_path=self.root / "brief.md", model="m",
            )
            folder = self.root / "meeting"
            folder.mkdir()
            engine.current = session.Recording(
                session_dir=folder, started_at=0.0,
                app="manual" if manual else "ZOOM", manual=manual,
                transcript_jsonl=(folder / "transcript.jsonl").open("a"),
            )
            pump = asyncio.create_task(engine._pump())
            engine._audience = True  # the window is open
            engine._start_you_display()
            await asyncio.sleep(0.05)
            for utterance in heard:
                await engine.queue.put(utterance)
                await asyncio.sleep(0.05)
            pump.cancel()
            engine._stop_you_display()
            await asyncio.sleep(0)
            engine.current.transcript_jsonl.close()

        with mock.patch.object(session, "capture_mic",
                               mock.AsyncMock(return_value="mic")), \
                mock.patch.object(session, "stream_speaker",
                                  self.fake_stream):
            asyncio.run(main())

    def test_a_recording_started_by_hand_streams_no_microphone(self):
        self.run_engine(manual=True, heard=[])
        self.assertEqual(self.streamed, [])

    def test_its_interim_words_from_the_far_side_change_nothing(self):
        self.run_engine(manual=True, heard=[Utterance("them", "hi", False)])
        self.assertEqual(self.streamed, [])

    def test_once_the_far_side_speaks_it_is_a_call_and_streams(self):
        self.run_engine(manual=True, heard=[
            Utterance("them", "Can you hear me?", True),
            Utterance("them", "Good.", True),
        ])
        self.assertEqual(self.streamed, ["you"])

    def test_a_call_found_by_its_app_streams_at_once(self):
        self.run_engine(manual=False, heard=[])
        self.assertEqual(self.streamed, ["you"])


if __name__ == "__main__":
    unittest.main()
