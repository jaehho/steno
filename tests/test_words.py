
"""The window's wording, worked out without a display."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from steno.gui import words
from steno.gui.sidebar import Session

NOW = datetime(2026, 9, 12, 16, 0)

SUMMARY = """# Workshop paper: figures and deadline

## TL;DR
Submission stays on September 26.

## Decisions
- Ablation table goes to the appendix.

## Action items
- **You**: rerun the baseline.

## Open questions
- Out of distribution?
"""


class TestListing(unittest.TestCase):
    def test_sections(self):
        self.assertEqual(words.list_section(datetime(2026, 9, 12, 9, 0), NOW), "Today")
        self.assertEqual(words.list_section(datetime(2026, 9, 8, 9, 0), NOW), "Last 7 days")
        self.assertEqual(words.list_section(datetime(2026, 8, 1, 9, 0), NOW), "Earlier")

    def test_when_is_as_short_as_is_unambiguous(self):
        self.assertEqual(words.when(datetime(2026, 9, 12, 9, 5), NOW), "09:05")
        self.assertEqual(words.when(datetime(2026, 9, 10, 14, 2), NOW), "Thu 14:02")
        self.assertEqual(words.when(datetime(2026, 3, 3, 14, 2), NOW), "Mar 3")
        self.assertEqual(words.when(datetime(2025, 3, 3, 14, 2), NOW), "Mar 3, 2025")

    def test_row_meta_skips_what_is_missing(self):
        self.assertEqual(
            words.row_meta(datetime(2026, 9, 10, 14, 2), NOW, 42.4, "thesis"),
            ["Thu 14:02", "42 min", "thesis"],
        )
        self.assertEqual(words.row_meta(datetime(2026, 9, 10, 14, 2), NOW, 0.2, None), ["Thu 14:02"])

    def test_todo_count(self):
        self.assertEqual(words.todo_count(0), "")
        self.assertEqual(words.todo_count(3), "3 to do")


class TestStatusCard(unittest.TestCase):
    def test_idle_explains_itself_and_offers_record(self):
        title, sub, button = words.status_card("idle", "", 100.0, 0.0)
        self.assertEqual((title, button), ("Listening", "Record"))
        self.assertIn("mic", sub)

    def test_recording_counts_up_and_names_the_app(self):
        title, sub, button = words.status_card("recording", "zoom", 1000.0 + 125, 1000.0)
        self.assertEqual((title, sub, button), ("Recording", "02:05 · zoom", "Stop"))

    def test_paused_counts_down_and_offers_resume(self):
        _title, sub, button = words.status_card("paused", "", 0.0, 0.0, paused_until=600.0)
        self.assertEqual((sub, button), ("Listening again in 10 min", "Resume"))

    def test_finalizing_has_no_button(self):
        self.assertEqual(words.status_card("finalizing", "", 0.0, 0.0)[2], "")


class TestSummary(unittest.TestCase):
    def test_title_and_tldr_label_are_dropped(self):
        body = words.summary_body(SUMMARY, has_todos=True)
        self.assertNotIn("Workshop paper", body)
        self.assertNotIn("TL;DR", body)
        self.assertTrue(body.startswith("Submission stays"))

    def test_action_items_move_to_the_todo_list(self):
        body = words.summary_body(SUMMARY, has_todos=True)
        self.assertNotIn("Action items", body)
        self.assertNotIn("rerun the baseline", body)
        self.assertIn("## Open questions", body)

    def test_action_items_stay_when_there_is_no_todo_list(self):
        """Items that failed to become to-dos must not vanish from the page."""
        body = words.summary_body(SUMMARY, has_todos=False)
        self.assertIn("rerun the baseline", body)

    def test_markup_escapes_before_formatting(self):
        self.assertEqual(words.markup("- **a** & <b>"), "•  <b>a</b> &amp; &lt;b&gt;")
        self.assertEqual(words.markup("## Decisions"), "<b>Decisions</b>")


if __name__ == "__main__":
    unittest.main()


class TestSearchLocate(unittest.TestCase):
    """Issue 2: search says where it matched, not just that it did."""

    def setUp(self):


        self.root = Path(tempfile.mkdtemp())
        self.dir = self.root / "20260927-100000"
        self.dir.mkdir()
        (self.dir / "transcript.jsonl").write_text(
            '{"ts": 1, "speaker": "them", "text": "the budget is fine"}\n'
            '{"ts": 2, "speaker": "you", "text": "we need the roadmap"}\n'
        )
        (self.dir / "transcript.txt").write_text(
            "[00:01] them: the budget is fine\n"
            "[00:02] you: we need the roadmap\n"
        )
        (self.dir / "summary.md").write_text("# Budget chat\n\nWe agreed.")
        (self.dir / "meta.json").write_text('{"title": "Budget chat"}')
        self.session = Session(self.dir)

    def test_transcript_hit_names_the_line(self):
        hit = self.session.locate("roadmap")
        self.assertEqual(hit["where"], "transcript")
        self.assertEqual(hit["line"], 1)

    def test_summary_hit_is_labelled(self):
        hit = self.session.locate("agreed")
        self.assertEqual(hit["where"], "summary")

    def test_notes_hit_is_labelled(self):
        (self.dir / "notes.md").write_text("ask about the printer\n")
        session = type(self.session)(self.dir)
        self.assertEqual(session.locate("printer")["where"], "notes")

    def test_title_hit(self):
        self.assertEqual(self.session.locate("budget chat")["where"], "title")

    def test_miss_returns_none(self):
        self.assertIsNone(self.session.locate("quantum"))

    def test_matches_delegates_to_locate(self):
        self.assertTrue(self.session.matches("roadmap"))
        self.assertFalse(self.session.matches("quantum"))
        self.assertTrue(self.session.matches(""))

    def test_all_words_must_match(self):
        self.assertTrue(self.session.matches("budget fine"))
        self.assertFalse(self.session.matches("budget quantum"))
