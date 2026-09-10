"""Turning a transcript line back into a place in the audio."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import wave
from pathlib import Path

from steno import playback, summarize


def write_wav(path: Path, seconds: float, rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\0\0" * int(seconds * rate))


class TestTracks(unittest.TestCase):
    def test_each_speaker_has_its_own_file(self):
        self.assertEqual(playback.track_for("you"), "you.wav")
        self.assertEqual(playback.track_for("them"), "them.wav")

    def test_unknown_speaker_falls_back_to_the_far_side(self):
        self.assertEqual(playback.track_for("???"), "them.wav")


class TestSeekOffset(unittest.TestCase):
    """A record's timestamp is when the line was *finalized*, so seeking to it
    lands after the sentence. Playback has to rewind by however long it took to
    say, or clicking a line plays the silence following it."""

    def test_seek_is_before_the_finalize_time(self):
        record = {"ts": 1000.0, "text": "we should ship it on friday"}
        offset = playback.seek_offset(record, started_at=900.0)
        self.assertLess(offset, 100.0)
        self.assertGreater(offset, 90.0)

    def test_longer_lines_rewind_further(self):
        started = 0.0
        short = playback.seek_offset({"ts": 100.0, "text": "yes"}, started)
        long = playback.seek_offset(
            {"ts": 100.0, "text": " ".join(["word"] * 40)}, started
        )
        self.assertGreater(short, long)

    def test_never_negative(self):
        """The first line of a meeting finalizes seconds in; rewinding past the
        start of the file would fail the seek outright."""
        offset = playback.seek_offset({"ts": 1.0, "text": "hello " * 20}, 0.0)
        self.assertEqual(offset, 0.0)

    def test_very_long_line_is_capped(self):
        offset = playback.seek_offset(
            {"ts": 1000.0, "text": "word " * 5000}, started_at=0.0
        )
        self.assertGreaterEqual(offset, 1000.0 - playback.MAX_UTTERANCE_S - 1)

    def test_display_offset_is_the_finalize_time(self):
        """The gutter should agree with `transcript.txt`, which stamps finals."""
        self.assertEqual(playback.display_offset({"ts": 90.0}, 30.0), 60)


class TestRecordedOffset(unittest.TestCase):
    """When the transcriber reported a real position, nothing is estimated."""

    def test_exact_offset_wins(self):
        record = {"ts": 9999.0, "text": "hello there", "offset": 120.0}
        self.assertAlmostEqual(
            playback.seek_offset(record, started_at=0.0), 120.0 - playback.LEAD_S
        )

    def test_exact_offset_ignores_the_wall_clock(self):
        """`ts` and `offset` disagree when the socket lagged; the audio wins."""
        record = {"ts": 500.0, "text": "hi", "offset": 30.0}
        self.assertLess(playback.seek_offset(record, started_at=0.0), 31.0)

    def test_gutter_matches_playback(self):
        record = {"ts": 500.0, "text": "hi", "offset": 30.0}
        self.assertEqual(playback.display_offset(record, 0.0), 30)

    def test_missing_offset_falls_back_to_the_estimate(self):
        record = {"ts": 100.0, "text": "a b c d e"}
        self.assertIsNone(playback.recorded_offset(record))
        self.assertGreater(playback.seek_offset(record, started_at=0.0), 90.0)

    def test_junk_offset_is_ignored(self):
        self.assertIsNone(playback.recorded_offset({"offset": "soon"}))


class TestDerivedStart(unittest.TestCase):
    """Sessions recorded before the tool wrote `started_at` still have to line
    up: the audio's own length and mtime say when capture began."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_start_is_mtime_minus_duration(self):
        path = self.dir / "them.wav"
        write_wav(path, 10.0)
        os.utime(path, (1000.0, 1000.0))
        self.assertAlmostEqual(playback.derived_start(self.dir), 990.0, places=1)

    def test_no_audio_at_all(self):
        self.assertEqual(playback.derived_start(self.dir), 0.0)

    def test_beats_the_first_line_fallback(self):
        """The old fallback treated the first spoken word as time zero, which
        threw playback off by the whole quiet head of the recording."""
        write_wav(self.dir / "them.wav", 60.0)
        os.utime(self.dir / "them.wav", (1060.0, 1060.0))
        started = playback.session_start(self.dir, [{"ts": 1045.0}])
        self.assertAlmostEqual(started, 1000.0, places=1)

    def test_repair_writes_it_once(self):
        write_wav(self.dir / "them.wav", 30.0)
        os.utime(self.dir / "them.wav", (1030.0, 1030.0))
        playback.repair_started_at(self.dir, [])
        meta = json.loads((self.dir / "meta.json").read_text())
        self.assertAlmostEqual(meta["started_at"], 1000.0, places=1)
        self.assertTrue(meta["started_at_derived"])

    def test_repair_never_overwrites_a_recorded_start(self):
        (self.dir / "meta.json").write_text(json.dumps({"started_at": 42.0}))
        write_wav(self.dir / "them.wav", 30.0)
        self.assertEqual(playback.repair_started_at(self.dir, []), 42.0)
        self.assertEqual(
            json.loads((self.dir / "meta.json").read_text())["started_at"], 42.0
        )

    def test_repair_keeps_the_rest_of_meta(self):
        (self.dir / "meta.json").write_text(json.dumps({"title": "A meeting"}))
        write_wav(self.dir / "them.wav", 30.0)
        os.utime(self.dir / "them.wav", (1030.0, 1030.0))
        playback.repair_started_at(self.dir, [])
        self.assertEqual(
            json.loads((self.dir / "meta.json").read_text())["title"], "A meeting"
        )


class TestSessionStart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_prefers_meta(self):
        (self.dir / "meta.json").write_text(json.dumps({"started_at": 500.0}))
        self.assertEqual(playback.session_start(self.dir, [{"ts": 900.0}]), 500.0)

    def test_falls_back_to_the_first_line(self):
        self.assertEqual(playback.session_start(self.dir, [{"ts": 900.0}]), 900.0)

    def test_survives_a_broken_meta(self):
        (self.dir / "meta.json").write_text("{oh no")
        self.assertEqual(playback.session_start(self.dir, [{"ts": 42.0}]), 42.0)

    def test_no_information_at_all(self):
        self.assertEqual(playback.session_start(self.dir, []), 0.0)


class TestFollowing(unittest.TestCase):
    def test_finds_the_line_being_spoken(self):
        self.assertEqual(playback.index_at([0.0, 10.0, 20.0], 12.0), 1)

    def test_before_the_first_line(self):
        self.assertEqual(playback.index_at([5.0, 10.0], 1.0), -1)

    def test_after_the_last_line(self):
        self.assertEqual(playback.index_at([5.0, 10.0], 99.0), 1)

    def test_empty(self):
        self.assertEqual(playback.index_at([], 3.0), -1)


class TestFormatting(unittest.TestCase):
    def test_minutes_and_seconds(self):
        self.assertEqual(playback.format_offset(61), "01:01")

    def test_hours_appear_only_when_needed(self):
        self.assertEqual(playback.format_offset(3661), "1:01:01")

    def test_negative_reads_as_zero(self):
        self.assertEqual(playback.format_offset(-5), "00:00")


if __name__ == "__main__":
    unittest.main()


class TestStaleness(unittest.TestCase):
    """The window has to notice files the CLI rewrote underneath it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.session = self.root / "20200101-000000"
        self.session.mkdir()
        (self.session / "transcript.jsonl").write_text("{}\n")
        (self.session / "summary.md").write_text("# a\n")

    def stamp(self):
        return summarize.session_stamp(self.session)

    def test_unchanged_files_look_unchanged(self):
        self.assertEqual(self.stamp(), self.stamp())

    def test_a_rewritten_transcript_shows_up(self):
        before = self.stamp()
        os.utime(self.session / "transcript.jsonl", (1, 1))
        self.assertNotEqual(before, self.stamp())

    def test_a_new_summary_shows_up(self):
        before = self.stamp()
        os.utime(self.session / "summary.md", (1, 1))
        self.assertNotEqual(before, self.stamp())

    def test_a_missing_file_is_not_a_crash(self):
        (self.session / "summary.md").unlink()
        self.assertEqual(len(self.stamp()), 5)
