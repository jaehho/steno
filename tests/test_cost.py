"""The cost meter's arithmetic and its one-line rendering."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from steno.cost import (
    format_meter,
    format_minutes,
    format_usd,
    prerecorded_cost,
    session_cost,
    stream_cost,
)
from steno.gui.sidebar import _session_cost


class TestRates(unittest.TestCase):
    def test_stream_is_dearer_than_batch(self):
        self.assertGreater(stream_cost(1), prerecorded_cost(1))

    def test_session_sums_both_sides_and_a_summary(self):
        # 30 min streamed + 30 min batched + 1 summary
        expect = 30 * 0.0077 + 30 * 0.0043 + 0.01
        self.assertAlmostEqual(session_cost(30, 30, summaries=1), expect)

    def test_zero(self):
        self.assertEqual(session_cost(0, 0, summaries=0), 0.0)


class TestFormat(unittest.TestCase):
    def test_usd(self):
        self.assertEqual(format_usd(0), "$0")
        self.assertEqual(format_usd(0.004), "<$0.01")
        self.assertEqual(format_usd(1.234), "$1.23")

    def test_minutes(self):
        self.assertEqual(format_minutes(0), "0 min")
        self.assertEqual(format_minutes(45), "45 min")
        self.assertEqual(format_minutes(90), "1.5 h")

    def test_meter(self):
        self.assertEqual(format_meter(45, 0.45), "45 min · ~$0.45")


class TestSessionCost(unittest.TestCase):
    """Issue 4: the meter reads what was stamped, and fills in what was not."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self._cost = _session_cost

    def _session(self, name, meta=None):
        d = self.root / name
        d.mkdir()
        if meta is not None:
            (d / "meta.json").write_text(__import__("json").dumps(meta))
        return d

    def test_stamped_estimate_wins(self):
        d = self._session("a", {"cost_usd": 0.12})
        self.assertEqual(self._cost(d, 99.0), 0.12)

    def test_old_sessions_still_count(self):
        d = self._session("b")
        got = self._cost(d, 30.0)
        self.assertGreater(got, 0)

    def test_corrupt_meta_falls_back(self):
        d = self.root / "c"
        d.mkdir()
        (d / "meta.json").write_text("{not json")
        self.assertGreater(self._cost(d, 10.0), 0)
