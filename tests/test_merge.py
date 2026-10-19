"""Joining two halves of a meeting the recorder left split."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from steno import merge, playback
from steno.notes import Todo, load_todos, save_todos


def write_wav(path: Path, seconds: float, rate: int = 16000, fill: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes([fill, 0]) * int(seconds * rate))


def wav_duration(path: Path) -> float:
    return playback.audio_duration(path)


def write_meta(path: Path, **fields) -> None:
    (path / "meta.json").write_text(json.dumps(fields) + "\n")


def write_records(path: Path, rows: list[dict]) -> None:
    with (path / "transcript.jsonl").open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


class TestJoinShift(unittest.TestCase):
    def test_the_wall_clock_gap_wins_when_the_audio_is_short(self):
        self.assertEqual(merge.join_shift(0.0, 30.0, 10.0), 30.0)

    def test_a_longer_track_wins_so_nothing_overlaps(self):
        self.assertEqual(merge.join_shift(0.0, 5.0, 10.0), 10.0)


class TestOffsetShifted(unittest.TestCase):
    def test_only_measured_offsets_move(self):
        out = merge.offset_shifted(
            [{"ts": 1.0, "offset": 0.5}, {"ts": 2.0}], 10.0
        )
        self.assertEqual(out[0]["offset"], 10.5)
        self.assertNotIn("offset", out[1])
        self.assertEqual(out[0]["ts"], 1.0)

    def test_the_originals_are_left_alone(self):
        rows = [{"ts": 1.0, "offset": 0.5}]
        merge.offset_shifted(rows, 10.0)
        self.assertEqual(rows[0]["offset"], 0.5)


class TestPreviousSession(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def make(self, name: str, transcript: bool = True) -> Path:
        path = self.root / name
        path.mkdir()
        if transcript:
            write_records(path, [{"ts": 1.0, "speaker": "them", "text": "hi"}])
        return path

    def test_it_skips_a_folder_without_a_transcript(self):
        older = self.make("20260101-090000")
        self.make("20260101-100000", transcript=False)
        mid = self.make("20260101-110000")
        self.make("20260101-120000")
        self.assertEqual(merge.previous_session(mid), older)

    def test_the_oldest_has_none(self):
        older = self.make("20260101-090000")
        self.make("20260101-120000")
        self.assertIsNone(merge.previous_session(older))

    def test_an_unknown_folder_has_none(self):
        self.make("20260101-090000")
        self.assertIsNone(merge.previous_session(self.root / "nope"))


class TestMergeSessions(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # Trash beside the sessions: a rename across filesystems would fail
        # the way it does when the home trash is on another disk.
        patcher = mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": str(Path(tmp.name) / "data")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.keep = self.root / "20260101-100000"
        self.absorb = self.root / "20260101-110000"
        self.keep.mkdir()
        self.absorb.mkdir()
        # 2s of the earlier half, 3s of the later, 28s of wall-clock gap.
        write_wav(self.keep / "you.wav", 2.0, fill=1)
        write_wav(self.keep / "them.wav", 2.0, fill=1)
        write_wav(self.absorb / "you.wav", 3.0, fill=2)
        write_wav(self.absorb / "them.wav", 3.0, fill=2)
        write_meta(self.keep, started_at=1_000_000.0, app="discord")
        write_meta(self.absorb, started_at=1_000_030.0, app="firefox")
        write_records(self.keep, [
            {"ts": 1_000_001.0, "speaker": "them", "text": "one",
             "offset": 0.5},
            {"ts": 1_000_002.0, "speaker": "you", "text": "two",
             "offset": 1.0},
        ])
        write_records(self.absorb, [
            {"ts": 1_000_031.0, "speaker": "them", "text": "three",
             "offset": 0.5},
            {"ts": 1_000_032.0, "speaker": "you", "text": "four",
             "offset": 1.5},
        ])
        save_todos(self.absorb, [Todo(text="later todo", uid="u2")])
        save_todos(self.keep, [Todo(text="earlier todo", uid="u1")])

    def test_keep_must_be_earlier(self):
        with self.assertRaises(ValueError):
            merge.merge_sessions(self.absorb, self.keep)
        with self.assertRaises(ValueError):
            merge.merge_sessions(self.keep, self.keep)

    def test_audio_is_one_timeline_with_the_gap_filled(self):
        result = merge.merge_sessions(self.keep, self.absorb)
        # 2s keep + 28s silence + 3s absorb = 33s; shift is 30.
        self.assertEqual(result.shift_s, 30.0)
        self.assertAlmostEqual(wav_duration(self.keep / "you.wav"), 33.0)
        self.assertAlmostEqual(wav_duration(self.keep / "them.wav"), 33.0)

    def test_both_sides_share_the_same_prefix(self):
        merge.merge_sessions(self.keep, self.absorb)
        # First frame of the later half is the high fill byte in both tracks.
        for name in ("you.wav", "them.wav"):
            with wave.open(str(self.keep / name), "rb") as handle:
                handle.setpos(int(30.0 * 16000))
                frame = handle.readframes(1)
            self.assertEqual(frame[0], 2, name)

    def test_offsets_move_by_the_same_shift_and_ts_stay_wall_clock(self):
        merge.merge_sessions(self.keep, self.absorb)
        rows = [
            json.loads(line)
            for line in (self.keep / "transcript.jsonl").read_text().splitlines()
        ]
        self.assertEqual([r["text"] for r in rows],
                         ["one", "two", "three", "four"])
        self.assertEqual(rows[0]["offset"], 0.5)
        self.assertEqual(rows[2]["offset"], 30.5)
        self.assertEqual(rows[3]["offset"], 31.5)
        self.assertEqual(rows[2]["ts"], 1_000_031.0)

    def test_todos_are_concatenated_and_the_absorb_is_trashed(self):
        result = merge.merge_sessions(self.keep, self.absorb)
        self.assertEqual(
            [t.text for t in load_todos(self.keep)],
            ["earlier todo", "later todo"],
        )
        self.assertEqual(result.trash, "trashed")
        self.assertFalse(self.absorb.exists())

    def test_mic_pending_drops_you_lines_and_stays_pending(self):
        write_meta(self.absorb, started_at=1_000_030.0, app="firefox",
                   mic_pending=True)
        merge.merge_sessions(self.keep, self.absorb)
        rows = [
            json.loads(line)
            for line in (self.keep / "transcript.jsonl").read_text().splitlines()
        ]
        self.assertEqual([r["speaker"] for r in rows], ["them", "them"])
        meta = json.loads((self.keep / "meta.json").read_text())
        self.assertTrue(meta.get("mic_pending"))

    def test_notes_from_the_later_half_are_appended(self):
        (self.absorb / "notes.md").write_text("from the later half\n")
        (self.keep / "notes.md").write_text("from the earlier half\n")
        merge.merge_sessions(self.keep, self.absorb)
        text = (self.keep / "notes.md").read_text()
        self.assertIn("from the earlier half", text)
        self.assertIn("from the later half", text)
        self.assertLess(
            text.index("from the earlier half"),
            text.index("from the later half"),
        )

    def test_a_text_only_half_still_joins(self):
        (self.absorb / "you.wav").unlink()
        (self.absorb / "them.wav").unlink()
        result = merge.merge_sessions(self.keep, self.absorb)
        self.assertEqual(result.lines, 4)
        # Keep plus the gap, with nothing of the later half to follow it.
        self.assertAlmostEqual(wav_duration(self.keep / "them.wav"), 30.0)
