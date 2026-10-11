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
            {"speaker": "them", "who": "speaker_0", "text": "yes, Bob speaking"},
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


class TestCalledBoundary(unittest.TestCase):
    """"Word here" at a sentence boundary is speech, not a name (issue 14)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def _learn(self, text):
        row = {"speaker": "them", "who": "speaker_0", "text": text}
        return learn_from(self.dir, [row])

    def test_utterance_initial_here_is_not_a_name(self):
        # 20261006-161241: this line taught the map `So`.
        names = self._learn("So here again, we're showing what happens when you...")
        self.assertEqual(names, {})

    def test_here_after_sentence_punctuation_is_not_a_name(self):
        names = self._learn("Great point. So here again, we're showing it.")
        self.assertEqual(names, {})

    def test_mid_sentence_here_is_a_name(self):
        names = self._learn("Well, Alice here - go ahead with the numbers.")
        self.assertEqual(names.get("speaker_0"), "Alice")

    def test_mid_sentence_speaking_is_a_name(self):
        names = self._learn("It's Alice speaking, from the design team.")
        self.assertEqual(names.get("speaker_0"), "Alice")

    def test_real_mention_after_a_false_one_still_wins(self):
        names = self._learn(
            "So here again. Well, Alice here will take that one."
        )
        self.assertEqual(names.get("speaker_0"), "Alice")


class TestFalseIntroductions(unittest.TestCase):
    """Things that sound like an introduction and are not."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def _learn(self, text):
        row = {"speaker": "them", "who": "speaker_0", "text": text}
        return learn_from(self.dir, [row])

    def test_a_weekday_is_not_a_name(self):
        self.assertEqual(self._learn("I think it's Monday already"), {})

    def test_it_is_a_thing_is_not_a_name(self):
        self.assertEqual(self._learn("honestly it's Zoom again"), {})

    def test_mid_sentence_this_is_is_not_a_name(self):
        self.assertEqual(self._learn("so this is Python code"), {})

    def test_im_with_a_capital_i_is_a_name(self):
        self.assertEqual(self._learn("Hi, I'm Alice").get("speaker_0"), "Alice")

    def test_typographic_apostrophe(self):
        self.assertEqual(self._learn("Hi, I\u2019m Alice").get("speaker_0"), "Alice")

    def test_my_name_is(self):
        self.assertEqual(self._learn("My name is Priya.").get("speaker_0"), "Priya")

    def test_an_opening_this_is(self):
        self.assertEqual(self._learn("This is Bob from Acme").get("speaker_0"), "Bob")

    def test_a_feeling_is_not_a_name(self):
        self.assertEqual(self._learn("I'm Sorry about that"), {})
