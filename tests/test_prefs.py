"""What the window remembers: the live transcript's size."""
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


if __name__ == "__main__":
    unittest.main()
