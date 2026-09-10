"""Planning a re-timing pass: what it would cost, and whether it's needed."""
from __future__ import annotations

import json
import tempfile
import unittest
import wave
from pathlib import Path

from steno import realign


def write_session(root: Path, name: str, *, seconds: float = 60.0,
                  offsets: bool = False, audio: bool = True) -> Path:
    session = root / name
    session.mkdir(parents=True)
    rows = []
    for i in range(3):
        row = {"ts": 1000.0 + i * 10, "speaker": "them", "text": f"line {i}"}
        if offsets:
            row["offset"] = float(i * 10)
        rows.append(row)
    (session / "transcript.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )
    if audio:
        for track in ("you.wav", "them.wav"):
            with wave.open(str(session / track), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(b"\0\0" * int(seconds * 16000))
    return session


class TestAlignment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_old_transcripts_are_not_aligned(self):
        session = write_session(self.root, "a", offsets=False)
        self.assertFalse(realign.is_aligned(session))

    def test_transcripts_with_offsets_are(self):
        session = write_session(self.root, "b", offsets=True)
        self.assertTrue(realign.is_aligned(session))

    def test_a_partly_aligned_transcript_still_needs_the_pass(self):
        """One line without an offset is one line that seeks by guesswork."""
        session = write_session(self.root, "c", offsets=True)
        rows = [json.loads(x) for x in
                (session / "transcript.jsonl").read_text().splitlines()]
        rows[1].pop("offset")
        (session / "transcript.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )
        self.assertFalse(realign.is_aligned(session))

    def test_empty_transcript_is_not_aligned(self):
        session = self.root / "d"
        session.mkdir()
        self.assertFalse(realign.is_aligned(session))

    def test_audio_is_required(self):
        session = write_session(self.root, "e", audio=False)
        self.assertFalse(realign.has_audio(session))


class TestPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_counts_both_tracks(self):
        session = write_session(self.root, "a", seconds=60.0)
        self.assertAlmostEqual(realign.plan(session).minutes, 2.0, places=2)

    def test_cost_follows_the_minutes(self):
        session = write_session(self.root, "a", seconds=600.0)
        job = realign.plan(session)
        self.assertAlmostEqual(job.cost, 20.0 * realign.COST_PER_MINUTE, places=4)

    def test_no_audio_costs_nothing(self):
        session = write_session(self.root, "a", audio=False)
        self.assertEqual(realign.plan(session).minutes, 0.0)

    def test_plan_reports_what_is_already_done(self):
        session = write_session(self.root, "a", offsets=True)
        self.assertTrue(realign.plan(session).already_aligned)

    def test_formatted_plan_totals(self):
        text = realign.format_plan([
            realign.plan(write_session(self.root, "a", seconds=60.0)),
            realign.plan(write_session(self.root, "b", seconds=60.0)),
        ])
        self.assertIn("total", text)
        self.assertIn("4.0 min", text)


if __name__ == "__main__":
    unittest.main()
