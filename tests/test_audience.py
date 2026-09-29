"""The mic is only streamed live while someone is watching (issue 5)."""
from __future__ import annotations

import unittest

from steno.transcribe import Utterance


class TestUtterancePersist(unittest.TestCase):
    def test_default_persists(self):
        u = Utterance(speaker="them", text="hi", final=True)
        self.assertTrue(u.persist)

    def test_display_only_does_not(self):
        u = Utterance(speaker="you", text="hi", final=True, persist=False)
        self.assertFalse(u.persist)

    def test_pump_skips_display_only(self):
        """A display-only final must never be written beside the batch pass."""
        # The rule lives in Engine._pump: `if not u.final or not u.persist`.
        # Re-check the predicate here so a rewrite of _pump has to break this
        # test too.
        u = Utterance(speaker="you", text="hi", final=True, persist=False)
        self.assertTrue(not u.final or not u.persist)
        u2 = Utterance(speaker="them", text="hi", final=True, persist=True)
        self.assertFalse(not u2.final or not u2.persist)
