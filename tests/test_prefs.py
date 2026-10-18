"""What the window remembers: the live transcript's size, the Ask model."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from steno.gui import prefs
from steno.gui.style import TRANSCRIPT_PT_DEFAULT, TRANSCRIPT_PT_MAX, TRANSCRIPT_PT_MIN


class TestPrefs(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_when_nothing_was_saved(self):
        self.assertEqual(prefs.transcript_pt(), TRANSCRIPT_PT_DEFAULT)

    def test_a_chosen_size_is_remembered(self):
        prefs.save_transcript_pt(22)
        self.assertEqual(prefs.transcript_pt(), 22)

    def test_a_damaged_file_is_the_default(self):
        prefs.prefs_path().parent.mkdir(parents=True)
        prefs.prefs_path().write_text("{not json")
        self.assertEqual(prefs.transcript_pt(), TRANSCRIPT_PT_DEFAULT)

    def test_a_silly_size_is_clamped(self):
        prefs.save_transcript_pt(400)
        self.assertEqual(prefs.transcript_pt(), TRANSCRIPT_PT_MAX)
        prefs.save_transcript_pt(1)
        self.assertEqual(prefs.transcript_pt(), TRANSCRIPT_PT_MIN)

    def test_no_ask_model_unless_one_was_picked(self):
        self.assertEqual(prefs.ask_model(), "")

    def test_a_picked_ask_model_is_remembered(self):
        prefs.save_ask_model("mimo-v2.6-pro[1m]")
        self.assertEqual(prefs.ask_model(), "mimo-v2.6-pro[1m]")

    def test_picking_the_default_again_forgets_the_pick(self):
        prefs.save_ask_model("some-model")
        prefs.save_ask_model("  ")
        self.assertEqual(prefs.ask_model(), "")
        self.assertNotIn("ask_model", prefs.prefs_path().read_text())

    def test_a_pick_keeps_the_other_prefs_and_they_keep_it(self):
        prefs.save_transcript_pt(22)
        prefs.save_ask_model("some-model")
        prefs.save_transcript_pt(18)
        self.assertEqual(prefs.ask_model(), "some-model")
        self.assertEqual(prefs.transcript_pt(), 18)

    def test_a_pick_that_is_not_text_is_no_pick(self):
        prefs.prefs_path().parent.mkdir(parents=True)
        prefs.prefs_path().write_text('{"ask_model": 7}')
        self.assertEqual(prefs.ask_model(), "")
        prefs.prefs_path().write_text("{not json")
        self.assertEqual(prefs.ask_model(), "")


if __name__ == "__main__":
    unittest.main()
