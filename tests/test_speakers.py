"""Turning diarized voices into names (issue 7)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from steno.speakers import label, learn_from, load_names, save_names


class TestNames(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_missing_file_is_empty(self):
        self.assertEqual(load_names(self.dir), {})

    def test_roundtrip(self):
        save_names(self.dir, {"speaker_0": "Alice"})
        self.assertEqual(load_names(self.dir), {"speaker_0": "Alice"})

    def test_empty_map_is_not_written(self):
        save_names(self.dir, {})
        self.assertFalse((self.dir / "speakers.json").is_file())


class TestLabel(unittest.TestCase):
    def test_named_voice(self):
        row = {"speaker": "them", "who": "speaker_0", "text": "hi"}
        self.assertEqual(label(row, {"speaker_0": "Alice"}), "Alice")

    def test_unnamed_falls_back(self):
        row = {"speaker": "them", "who": "speaker_1", "text": "hi"}
        self.assertEqual(label(row, {"speaker_0": "Alice"}), "them")

    def test_near_side_stays_you(self):
        row = {"speaker": "you", "who": "", "text": "hi"}
        self.assertEqual(label(row, {}), "you")


class TestLearn(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def _records(self):
        return [
            {"speaker": "them", "who": "speaker_0", "text": "hello everyone"},
            {"speaker": "them", "who": "speaker_1", "text": "this is Alice"},
            {"speaker": "them", "who": "speaker_0", "text": "Bob speaking"},
            {"speaker": "them", "who": "speaker_2", "text": "nothing useful here"},
        ]

    def test_picks_up_introductions(self):
        names = learn_from(self.dir, self._records())
        self.assertEqual(names.get("speaker_1"), "Alice")
        self.assertEqual(names.get("speaker_0"), "Bob")
        self.assertNotIn("speaker_2", names)

    def test_existing_names_win(self):
        save_names(self.dir, {"speaker_1": "Alicia"})
        names = learn_from(self.dir, self._records())
        self.assertEqual(names["speaker_1"], "Alicia")

    def test_persists(self):
        learn_from(self.dir, self._records())
        self.assertEqual(load_names(self.dir)["speaker_1"], "Alice")
        self.assertTrue((self.dir / "speakers.json").is_file())
