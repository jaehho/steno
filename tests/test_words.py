
"""The window's wording, worked out without a display."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from steno import citations
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

    def test_a_meeting_without_a_summary_says_so(self):
        self.assertEqual(
            words.row_meta(datetime(2026, 9, 10, 14, 2), NOW, 42.4, None, summarized=False),
            ["Thu 14:02", "42 min", "no summary"],
        )

    def test_todo_count(self):
        self.assertEqual(words.todo_count(0), "")
        self.assertEqual(words.todo_count(3), "3 to do")

    def test_a_put_away_meeting_says_so_on_its_page(self):
        start = datetime(2026, 9, 10, 14, 2)
        self.assertNotIn("Archived", words.page_subtitle(start, 42.4, None))
        self.assertIn(
            "Archived", words.page_subtitle(start, 42.4, None, archived=True)
        )


class TestArchiveWording(unittest.TestCase):
    """What it says about the promises left in a meeting put away."""

    def test_archiving_says_how_many_to_dos_stay_open(self):
        self.assertNotIn("stay", words.archived_toast("Sync", 0))
        said = words.archived_toast("Sync", 3)
        self.assertIn("Sync", said)
        self.assertIn("3 to-dos stay open", said)
        self.assertIn("1 to-do stay open", words.archived_toast("Sync", 1))

    def test_the_count_of_open_to_dos_explains_the_archived_part(self):
        self.assertEqual(words.owed_note(0), "")
        self.assertIn("2 of them are", words.owed_note(2))
        self.assertIn("1 of them is", words.owed_note(1))

    def test_the_empty_page_counts_the_meetings_put_away(self):
        title, description = words.nothing_open(0)
        self.assertEqual(title, "No meetings yet")
        title, description = words.nothing_open(4)
        self.assertNotEqual(title, "No meetings yet")
        self.assertIn("4 archived meetings", description)
        self.assertIn("1 archived meeting.", words.nothing_open(1)[1])


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


def shown(md: str, has_todos: bool) -> list[tuple[str, str]]:
    return [(b.kind, b.markup) for b in words.summary_blocks(md, has_todos)]


class TestSummaryBlocks(unittest.TestCase):
    def test_title_and_tldr_label_are_dropped(self):
        blocks = shown(SUMMARY, has_todos=True)
        self.assertEqual(
            blocks[0], ("text", "Submission stays on September 26.")
        )
        for _kind, markup in blocks:
            self.assertNotIn("Workshop paper", markup)
            self.assertNotIn("TL;DR", markup)

    def test_rows_come_in_the_order_of_the_page(self):
        self.assertEqual(
            [kind for kind, _ in shown(SUMMARY, has_todos=True)],
            ["text", "heading", "item", "heading", "item"],
        )

    def test_action_items_move_to_the_todo_list(self):
        blocks = shown(SUMMARY, has_todos=True)
        text = " ".join(markup for _kind, markup in blocks)
        self.assertNotIn("Action items", text)
        self.assertNotIn("rerun the baseline", text)
        self.assertIn(("heading", "<b>Open questions</b>"), blocks)

    def test_action_items_stay_when_there_is_no_todo_list(self):
        """Items that failed to become to-dos must not vanish from the page."""
        blocks = shown(SUMMARY, has_todos=False)
        self.assertIn(("item", "<b>You</b>: rerun the baseline."), blocks)

    def test_owner_lines_are_headers_not_items(self):
        md = "## Action items\n**You**\n- Pay it\n**Them**:\n- Send it\n"
        self.assertEqual(
            shown(md, has_todos=False),
            [
                ("heading", "<b>Action items</b>"),
                ("owner", "<b>You</b>"),
                ("item", "Pay it"),
                ("owner", "<b>Them</b>:"),
                ("item", "Send it"),
            ],
        )

    def test_an_item_carries_its_cites_and_the_marker_is_not_shown(self):
        marked = SUMMARY.replace(
            "Out of distribution?", "Out of distribution? <!--cite 12-30,41-->"
        ).replace("appendix.", "appendix. <!--cite 5-->")
        for has_todos in (True, False):
            blocks = words.summary_blocks(marked, has_todos)
            self.assertEqual(
                [(b.kind, b.markup) for b in blocks],
                shown(SUMMARY, has_todos),
            )
            self.assertNotIn("cite", " ".join(b.markup for b in blocks))
        self.assertEqual(
            [b.cites for b in words.summary_blocks(marked, True)],
            [[], [], [(5, 5)], [], [(12, 30), (41, 41)]],
        )

    def test_an_item_is_what_the_citation_engine_calls_one(self):
        """The rows that can carry a cite are the ones `items` finds."""
        md = (
            "# T\n\n## Decisions\n- one <!--cite 1-->\n* two\n"
            "1. three <!--cite 2-->\n   - four\n- [ ] five\nplain\n"
        )
        rows = [b for b in words.summary_blocks(md, False) if b.kind == "item"]
        self.assertEqual(
            [b.markup for b in rows], [i.text for i in citations.items(md)]
        )
        self.assertEqual(
            [(b.bullet, b.indent) for b in rows],
            [("•", 0), ("•", 0), ("1.", 0), ("•", 3), ("•", 0)],
        )

    def test_markup_escapes_before_formatting(self):
        md = "- **a** & <b>\nplain `x` <i>\n## A & B\n"
        self.assertEqual(
            shown(md, has_todos=False),
            [
                ("item", "<b>a</b> &amp; &lt;b&gt;"),
                ("text", "plain <tt>x</tt> &lt;i&gt;"),
                ("heading", "<b>A &amp; B</b>"),
            ],
        )


RECORDS = [
    {"speaker": "them", "text": "can you send the figures"},
    {"speaker": "you", "text": "yes, Thursday"},
    {"speaker": "them", "text": "great"},
    {"speaker": "you", "text": "x" * 30 + " " + "word " * 30},
]
OFFSETS = [10, 14, 20, 75]


class TestCitePlaces(unittest.TestCase):
    def places(self, *cites):
        return words.cite_places(cites, RECORDS, OFFSETS)

    def test_a_range_is_one_place_with_the_lines_that_follow_the_first(self):
        (place,) = self.places((10, 14))
        self.assertEqual(
            (place.rows, place.stamp, place.speaker, place.quote, place.more),
            ([0, 1], "00:10", "Them", "can you send the figures", 1),
        )

    def test_a_second_in_the_middle_of_a_line_finds_the_line(self):
        (place,) = self.places((16, 16))
        self.assertEqual((place.rows, place.stamp, place.more), ([1], "00:14", 0))

    def test_places_are_listed_top_of_the_transcript_first(self):
        found = self.places((75, 75), (10, 10), (20, 20))
        self.assertEqual([p.rows for p in found], [[0], [2], [3]])

    def test_cites_that_land_on_the_same_lines_are_one_place(self):
        self.assertEqual(len(self.places((10, 14), (10, 14), (9, 15))), 1)

    def test_a_cite_before_the_first_line_is_no_place(self):
        self.assertEqual(self.places((3, 5)), [])
        self.assertEqual(
            words.cite_places([(5, 9)], [], []), []
        )

    def test_a_long_line_is_quoted_up_to_a_word(self):
        (place,) = self.places((75, 75))
        self.assertLessEqual(len(place.quote), words.QUOTE_CHARS + 1)
        self.assertTrue(place.quote.endswith("word…"), place.quote)


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

    def test_a_citation_marker_is_not_a_word_to_search_for(self):
        (self.dir / "summary.md").write_text(
            "# Budget chat\n\n- We agreed. <!--cite 1-2-->\n"
        )
        session = type(self.session)(self.dir)
        self.assertEqual(session.locate("agreed")["where"], "summary")
        for query in ("cite", "<!--", "1-2"):
            self.assertIsNone(session.locate(query))

    def test_matches_delegates_to_locate(self):
        self.assertTrue(self.session.matches("roadmap"))
        self.assertFalse(self.session.matches("quantum"))
        self.assertTrue(self.session.matches(""))

    def test_all_words_must_match(self):
        self.assertTrue(self.session.matches("budget fine"))
        self.assertFalse(self.session.matches("budget quantum"))


class TestSummaryMissing(unittest.TestCase):
    def test_offers_to_write_it_when_there_is_a_transcript(self):
        text, offer = words.summary_missing(12)
        self.assertEqual(text, "No summary yet.")
        self.assertTrue(offer)

    def test_says_when_there_is_nothing_to_summarize(self):
        text, offer = words.summary_missing(0)
        self.assertIn("Nothing was transcribed", text)
        self.assertFalse(offer)

    def test_says_it_is_being_written_and_does_not_offer_twice(self):
        text, offer = words.summary_missing(12, "Writing the summary…")
        self.assertEqual(text, "Writing the summary…")
        self.assertFalse(offer)


class TestSummaryNote(unittest.TestCase):
    """The line beside the Summary heading."""

    def test_a_summary_that_cites_nothing_offers_to_do_it_again(self):
        self.assertEqual(
            words.summary_note(citations.Coverage(items=3, cited=0)),
            'No citations. <a href="summarize" '
            'title="Write this summary again">Summarize again</a> '
            "to add them.",
        )
        self.assertEqual(words.SUMMARIZE_AGAIN, "summarize")

    def test_a_summary_with_a_citation_has_nothing_to_say(self):
        self.assertEqual(words.summary_note(citations.Coverage(3, 1)), "")

    def test_one_with_nothing_to_cite_is_not_nagged_about(self):
        self.assertEqual(words.summary_note(citations.Coverage(0, 0)), "")

    def test_while_it_is_written_over_it_says_so_and_offers_nothing(self):
        note = words.summary_note(
            citations.Coverage(items=3, cited=0), "Writing the summary…"
        )
        self.assertEqual(note, "Writing the summary…")
        self.assertEqual(
            words.summary_note(citations.Coverage(3, 3), "Writing…"),
            "Writing…",
        )

    def test_what_is_said_is_escaped_for_markup(self):
        self.assertEqual(words.summary_note(citations.Coverage(), "a < b"),
                         "a &lt; b")
