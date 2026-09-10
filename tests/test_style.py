"""The stylesheet builds, and says what it must about state and legibility."""
from __future__ import annotations

import unittest

from steno.gui.style import (
    FALLBACK,
    TRANSCRIPT_PT_DEFAULT,
    palette,
    stylesheet,
)


class TestPalette(unittest.TestCase):
    def test_every_fallback_key_is_present(self):
        """A missing desktop palette must never leave a colour undefined."""
        C = palette()
        for key in FALLBACK:
            self.assertIn(key, C)

    def test_values_are_hex(self):
        for key, value in palette().items():
            self.assertRegex(value, r"^#[0-9a-fA-F]{3,8}$", key)


class TestStylesheet(unittest.TestCase):
    def setUp(self):
        self.css = stylesheet(FALLBACK)

    def test_builds_without_placeholders(self):
        self.assertNotIn("{", self.css.replace("{{", "").split("window.steno")[0])

    def test_carries_the_transcript_size(self):
        self.assertIn(f"font-size: {TRANSCRIPT_PT_DEFAULT}pt", self.css)

    def test_transcript_size_is_adjustable(self):
        self.assertIn("font-size: 22pt", stylesheet(FALLBACK, 22))

    def test_every_state_has_a_colour(self):
        for state in ("recording", "idle", "paused", "finalizing"):
            self.assertIn(f".state-{state}", self.css)

    def test_speaker_classes_exist(self):
        self.assertIn(".from-them", self.css)
        self.assertIn(".from-you", self.css)
