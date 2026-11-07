"""A recording started by hand is a room: its microphone pass asks
Deepgram for voices, and each line keeps whose it was."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from steno import transcribe

TWO_VOICES = {
    "results": {
        "utterances": [
            {"start": 0.5, "end": 2.0, "transcript": "The second batch is ready.",
             "speaker": 0},
            {"start": 2.4, "end": 4.0, "transcript": "Which version?",
             "speaker": 1},
        ]
    }
}


class TestRoomDiarize(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wav = Path(self.tmp.name) / "you.wav"
        self.wav.write_bytes(b"RIFF....WAVE")
        self.sent: list[httpx.Request] = []
        real = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            self.sent.append(request)
            return httpx.Response(200, json=TWO_VOICES)

        transport = httpx.MockTransport(handler)
        patcher = mock.patch.object(
            transcribe.httpx, "AsyncClient",
            side_effect=lambda **kw: real(transport=transport, **kw),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"DEEPGRAM_API_KEY": "test"})
        env.start()
        self.addCleanup(env.stop)

    def run_pass(self, voices_as: str) -> list[dict]:
        return asyncio.run(transcribe.transcribe_file(
            self.wav, 1000.0, "you", voices_as=voices_as
        ))

    def test_a_room_asks_for_voices_and_keeps_them(self):
        rows = self.run_pass(voices_as="mic")
        self.assertEqual(
            self.sent[0].url.params.get("diarize_model"), "latest"
        )
        self.assertEqual([r["who"] for r in rows], ["mic_0", "mic_1"])

    def test_a_call_leaves_your_side_as_one_voice(self):
        self.run_pass(voices_as="")
        self.assertNotIn("diarize_model", self.sent[0].url.params)


if __name__ == "__main__":
    unittest.main()
