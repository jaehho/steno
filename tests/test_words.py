
"""The window's wording, worked out without a display."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from steno import citations, projectnotes
from steno.gui import words
from steno.gui.sidebar import Session
from steno.notesjob import NotesPhase, NotesState

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
    """What it says about the to-dos of a meeting put away."""

    def test_archiving_says_how_many_tasks_it_removed_from_asst(self):
        self.assertEqual(words.archived_toast("Sync", 0), "Archived “Sync”")
        self.assertEqual(
            words.archived_toast("Sync", 3),
            "Archived “Sync”. Removed 3 open tasks from asst.",
        )
        self.assertIn("1 open task from", words.archived_toast("Sync", 1))

    def test_a_task_asst_kept_is_said_with_the_way_to_try_again(self):
        """Unarchive does not take a task out of asst, so it is not the
        retry; archiving again is, and for a window that is two steps."""
        self.assertEqual(
            words.archived_toast("Sync", 0, 2),
            "Archived “Sync”. Could not remove 2 open tasks from asst. "
            "Unarchive and archive again to retry.",
        )
        self.assertIn(
            "1 open task from asst. Unarchive",
            words.archived_toast("Sync", 0, 1),
        )
        self.assertEqual(
            words.archived_toast("Sync", 3, 1),
            "Archived “Sync”. Removed 3 open tasks from asst; "
            "could not remove 1. Unarchive and archive again to retry.",
        )
        # Nothing to try again, nothing said about it.
        self.assertNotIn("retry", words.archived_toast("Sync", 3))

    def test_bringing_it_back_says_only_what_it_filed_again(self):
        self.assertEqual(words.unarchived_toast("Sync", 0), "")
        self.assertEqual(
            words.unarchived_toast("Sync", 2),
            "Unarchived “Sync”. Filed 2 open tasks in asst again.",
        )
        self.assertEqual(
            words.unarchived_toast("Sync", 0, 1),
            "Unarchived “Sync”. asst did not take 1 open task; "
            "it is open here only. Archive and unarchive again to retry.",
        )
        self.assertIn(
            "asst did not take 2. Archive and unarchive again",
            words.unarchived_toast("Sync", 1, 2),
        )

    def test_a_meeting_put_away_with_tasks_left_says_what_closes_them(self):
        (line,) = words.put_away_notes(False, True, "20260801-090000")
        self.assertTrue(line.startswith("Some are still open"))
        self.assertIn("Unarchive it and archive it again", line)
        self.assertIn("steno archive 20260801-090000", line)
        (bare,) = words.put_away_notes(False, True)
        self.assertNotIn("steno archive", bare)
        (closed,) = words.put_away_notes(True, False, "20260801-090000")
        self.assertTrue(closed.startswith("Closed when"))
        self.assertEqual(words.put_away_notes(False, False), [])


class TestJoinedToast(unittest.TestCase):
    def test_it_says_where_the_later_half_went(self):
        self.assertEqual(
            words.joined_toast("Op-amp Q&A"),
            "Joined into “Op-amp Q&A”. The later half is in the trash.",
        )

    def test_a_half_that_could_not_be_trashed_does_not_claim_it_was(self):
        self.assertEqual(
            words.joined_toast("Op-amp Q&A", trashed=False),
            "Joined into “Op-amp Q&A”.",
        )


class TestEmptyArchive(unittest.TestCase):
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


class TestSpeakerTags(unittest.TestCase):
    def test_a_place_says_who_when_the_voice_has_a_name(self):
        record = {"speaker": "them", "who": "speaker_0", "text": "hi"}
        (place,) = words.cite_places([(10, 10)], [record], [10], {"speaker_0": "Priya"})
        self.assertEqual(place.speaker, "Priya")

    def test_the_column_fits_its_longest_tag_within_limits(self):
        self.assertEqual(words.tag_chars(["You", "Them"]), words.TAG_MIN_CHARS)
        self.assertEqual(words.tag_chars(["You", "Marcus"]), len("Marcus"))
        self.assertEqual(words.tag_chars(["x" * 40]), words.TAG_MAX_CHARS)
        self.assertEqual(words.tag_chars([]), words.TAG_MIN_CHARS)


class TestCitePlaces(unittest.TestCase):
    def places(self, *cites):
        return words.cite_places(cites, RECORDS, OFFSETS, {})

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
            words.cite_places([(5, 9)], [], [], {}), []
        )

    def test_a_long_line_is_quoted_up_to_a_word(self):
        (place,) = self.places((75, 75))
        self.assertLessEqual(len(place.quote), words.QUOTE_CHARS + 1)
        self.assertTrue(place.quote.endswith("word…"), place.quote)



class TestSearchMatches(unittest.TestCase):
    def test_finds_every_occurrence_whatever_its_case(self):
        self.assertEqual(
            words.match_spans("Budget and budget", "budget"), [(0, 6), (11, 17)]
        )

    def test_two_words_are_one_phrase(self):
        self.assertEqual(
            words.match_spans("the budget review, not review budget", "budget review"),
            [(4, 17)],
        )

    def test_spacing_between_the_words_does_not_matter(self):
        self.assertEqual(words.match_spans("budget\n  review", "budget review"), [(0, 15)])

    def test_the_query_is_taken_literally(self):
        self.assertEqual(words.match_spans("a.b axb", "a.b"), [(0, 3)])

    def test_no_phrase_means_no_spans(self):
        self.assertEqual(words.match_spans("anything", ""), [])

    def test_a_typed_query_loses_only_its_extra_spaces(self):
        self.assertEqual(words.search_phrase("  Budget   review "), "Budget review")

    def test_pango_counts_bytes(self):
        text = "café budget"
        self.assertEqual(
            words.byte_spans(text, words.match_spans(text, "budget")), [(6, 12)]
        )


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

    def named(self):
        """A meeting whose far side has two voices, one of them known."""
        (self.dir / "transcript.jsonl").write_text(
            '{"ts": 1, "speaker": "them", "who": "speaker_1", "text": "hello"}\n'
            '{"ts": 2, "speaker": "them", "who": "speaker_0", "text": "the budget"}\n'
            '{"ts": 3, "speaker": "you", "text": "we need the roadmap"}\n'
        )
        (self.dir / "speakers.json").write_text('{"speaker_0": "Priya"}')
        return type(self.session)(self.dir)

    def test_a_person_is_found_at_their_first_line(self):
        hit = self.named().locate("priya")
        self.assertEqual((hit["where"], hit["line"]), ("speaker", 1))

    def test_a_name_and_a_word_are_one_phrase_like_any_other(self):
        """Priya says "the budget", but nobody says "priya budget"."""
        self.assertIsNone(self.named().locate("priya budget"))

    def test_a_name_nobody_speaks_under_finds_nothing(self):
        """`steno realign` drops the voices, and the file stays behind."""
        (self.dir / "speakers.json").write_text('{"speaker_0": "Priya"}')
        self.assertIsNone(type(self.session)(self.dir).locate("priya"))

    def test_the_markup_of_a_transcript_line_is_not_searched(self):
        """It was `[09:30:00] them: words`, so these found every meeting."""
        (self.dir / "transcript.txt").write_text("[09:30:00] them: zebra\n")
        session = type(self.session)(self.dir)
        for query in ("them", "09:3", "zebra"):
            self.assertIsNone(session.locate(query), query)

    def test_the_folder_name_is_not_searched(self):
        for query in ("20260927", "100000", "-"):
            self.assertIsNone(self.session.locate(query), query)

    def test_the_date_is_searched_as_it_reads(self):
        for query in ("sunday", "september 27", "sep"):
            self.assertEqual(self.session.locate(query)["where"], "date", query)

    def test_the_date_is_found_the_ways_people_write_it(self):
        """The meeting is on Sunday, September 27, 2026."""
        for query in ("sun", "sep 27", "sept 27th", "27 september", "9/27",
                      "09/27/2026", "9/27/26", "27/9", "2026-09-27",
                      "sun sep 27", "Sunday, September 27", "sep 2026"):
            hit = self.session.locate(query)
            self.assertEqual(hit and hit["where"], "date", query)

    def test_another_day_or_a_word_that_is_not_a_date_finds_nothing(self):
        for query in ("monday", "sep 28", "9/28", "9/27/25", "oct", "mon sep 27",
                      "day", "27", "2026", "sept budget", "marc", "13/40"):
            self.assertIsNone(self.session.locate(query), query)


    def test_a_line_comes_before_the_date(self):
        (self.dir / "transcript.jsonl").write_text(
            '{"ts": 1, "speaker": "them", "text": "see you sunday"}\n'
        )
        hit = type(self.session)(self.dir).locate("sunday")
        self.assertEqual((hit["where"], hit["line"]), ("transcript", 0))

    def test_a_project_hit_says_project(self):
        (self.dir / "meta.json").write_text('{"project": "lab"}')
        session = type(self.session)(self.dir)
        self.assertEqual(session.locate("lab")["where"], "project")

    def test_matches_delegates_to_locate(self):
        self.assertTrue(self.session.matches("roadmap"))
        self.assertFalse(self.session.matches("quantum"))
        self.assertTrue(self.session.matches(""))

    def test_only_the_lit_parts_of_a_row_are_searched(self):
        parts = ["Friday, January 9", "45 min", "lab"]
        line = "  ·  ".join(parts)
        spans = words.spans_in_parts(parts, "january 9", lit=[0], sep="  ·  ")
        self.assertEqual([line[a:b] for a, b in spans], ["January 9"])
        spans = words.spans_in_parts(parts, "lab", lit=[2], sep="  ·  ")
        self.assertEqual([line[a:b] for a, b in spans], ["lab"])
        self.assertEqual(
            words.spans_in_parts(parts, "min", lit=[0, 2], sep="  ·  "), [])

    def test_two_words_must_be_together_and_in_order(self):
        self.assertTrue(self.session.matches("budget is fine"))
        self.assertFalse(self.session.matches("fine budget"))
        self.assertFalse(self.session.matches("budget roadmap"))

    def test_a_phrase_reads_through_bold_in_the_summary(self):
        (self.dir / "summary.md").write_text("# Chat\n\nThe **quarterly** plan.")
        session = type(self.session)(self.dir)
        self.assertEqual(session.locate("quarterly plan")["where"], "summary")

    def test_to_do_hit_is_labelled(self):
        (self.dir / "todos.json").write_text(json.dumps(
            [{"text": "renew the certificate", "owner": "you"}]
        ))
        session = type(self.session)(self.dir)
        self.assertEqual(session.locate("renew the")["where"], "to-dos")


class TestDateSearch(unittest.TestCase):
    TUESDAY = datetime(2026, 10, 20, 14, 7)

    def lit(self, query: str, start: datetime = TUESDAY) -> list[str]:
        """What a date search lights in the date a row spells out."""
        text = words.search_date(start)
        spans = words.date_spans(words.date_query(query), start)
        return [text[a:b] for a, b in spans]

    def test_a_month_and_day_light_together(self):
        for query in ("oct 20", "10/20", "october 20th", "2026-10-20"):
            self.assertEqual(self.lit(query), ["October 20"], query)

    def test_a_weekday_lights_the_weekday(self):
        for query in ("tue", "tues", "tuesday"):
            self.assertEqual(self.lit(query), ["Tuesday"], query)
        wednesday = datetime(2026, 10, 21)
        self.assertEqual(self.lit("weds", wednesday), ["Wednesday"])

    def test_every_part_named_is_lit(self):
        self.assertEqual(self.lit("tue oct 20"), ["Tuesday", "October 20"])
        self.assertEqual(self.lit("oct"), ["October"])

    def test_another_day_lights_nothing(self):
        self.assertEqual(self.lit("wed oct 20"), [])
        self.assertEqual(self.lit("10/21"), [])


class TestParseFilters(unittest.TestCase):
    """Issue #40: `key:value` words beside the phrase."""

    def parsed(self, query: str) -> tuple[list[tuple], str]:
        """`(key, value, negated)` of each filter, and the phrase."""
        filters, phrase = words.parse_filters(query)
        return [(f.key, f.value, f.negated) for f in filters], phrase

    def test_each_key_is_a_filter(self):
        for query, read in (
            ("project:lab", ("project", "lab", False)),
            ("speaker:priya", ("speaker", "priya", False)),
            ("after:10/1", ("after", "10/1", False)),
            ("before:oct", ("before", "oct", False)),
            ("has:todo", ("has", "todo", False)),
            ("in:transcript", ("in", "transcript", False)),
        ):
            self.assertEqual(self.parsed(query), ([read], ""), query)

    def test_a_filter_knows_where_it_was_typed(self):
        (found,), _ = words.parse_filters("budget !project:lab")
        self.assertEqual(found.span, (7, 19))

    def test_a_bang_negates_the_one_filter_it_is_on(self):
        self.assertEqual(
            self.parsed("!project:lab speaker:priya"),
            ([("project", "lab", True), ("speaker", "priya", False)], ""),
        )

    def test_an_unknown_key_is_text(self):
        self.assertEqual(self.parsed("foo:bar"), ([], "foo:bar"))
        self.assertEqual(self.parsed("see http://x"), ([], "see http://x"))

    def test_a_value_its_key_cannot_read_is_text(self):
        for query in ("after:soon", "before:tue", "after:20", "has:summary",
                      "in:everything", "project:", "!speaker:"):
            self.assertEqual(self.parsed(query), ([], query), query)

    def test_a_value_ends_at_the_space(self):
        self.assertEqual(
            self.parsed("project:lab budget"),
            ([("project", "lab", False)], "budget"),
        )

    def test_the_rest_is_one_phrase_in_order(self):
        self.assertEqual(
            words.search_phrase(" budget  project:lab review "),
            "budget review",
        )

    def test_keys_and_kept_values_ignore_case(self):
        self.assertEqual(
            self.parsed("Project:Lab IN:Todos HAS:TODO"),
            ([("project", "Lab", False), ("in", "to-dos", False),
              ("has", "todo", False)], ""),
        )

    def test_a_date_is_read_as_a_date_search_reads_it(self):
        for typed in ("10/1", "oct", "october", "2026-10-01", "10/1/26",
                      "20/10", "sept"):
            self.assertEqual(
                self.parsed(f"after:{typed}"),
                ([("after", typed, False)], ""),
                typed,
            )

    def test_the_box_sets_apart_each_filter(self):
        query = "budget project:lab !has:todo"
        spans = words.box_spans(query)
        self.assertEqual(
            [query[a:b] for a, b in spans], ["project:lab", "!has:todo"]
        )

    def test_the_box_sets_apart_a_date_phrase_too(self):
        query = "project:lab  oct 20"
        self.assertEqual(
            [query[a:b] for a, b in words.box_spans(query)],
            ["project:lab", "oct", "20"],
        )
        self.assertEqual(words.box_spans("foo:bar budget"), [])

    def test_in_picks_where_the_phrase_is_looked_for(self):
        def searched(query):
            return words.searched(words.parse_filters(query)[0])

        self.assertEqual(searched("budget"), frozenset(words.SURFACES))
        self.assertEqual(
            searched("in:title in:summary"), {"title", "summary"}
        )
        self.assertEqual(
            searched("!in:transcript"),
            set(words.SURFACES) - {"transcript"},
        )
        self.assertEqual(words.phrase_in("in:title budget", "title"), "budget")
        self.assertEqual(words.phrase_in("in:title budget", "summary"), "")


class TestFilterDay(unittest.TestCase):
    TODAY = date(2026, 11, 7)

    def day(self, query: str, today: date = TODAY) -> date:
        (found,), _ = words.parse_filters(query)
        return words.filter_day(found, today)

    def test_the_day_named(self):
        self.assertEqual(self.day("after:10/1"), date(2026, 10, 1))
        self.assertEqual(self.day("before:2026-10-20"), date(2026, 10, 20))
        self.assertEqual(self.day("after:10/1/25"), date(2025, 10, 1))

    def test_a_month_alone_is_all_of_it(self):
        self.assertEqual(self.day("after:oct"), date(2026, 10, 1))
        self.assertEqual(self.day("before:oct"), date(2026, 10, 31))
        self.assertEqual(self.day("before:feb"), date(2026, 2, 28))

    def test_no_year_is_the_last_such_day_to_have_come(self):
        self.assertEqual(self.day("after:11/7"), date(2026, 11, 7))
        self.assertEqual(self.day("after:12/1"), date(2025, 12, 1))
        january = date(2027, 1, 5)
        self.assertEqual(self.day("after:12/1", january), date(2026, 12, 1))
        self.assertEqual(self.day("before:dec", january), date(2026, 12, 31))

    def test_a_day_past_the_end_of_its_month_is_the_last(self):
        self.assertEqual(self.day("before:2/30"), date(2026, 2, 28))

    def test_only_a_date_filter_names_a_day(self):
        (found,), _ = words.parse_filters("project:lab")
        with self.assertRaises(ValueError):
            words.filter_day(found, self.TODAY)


def write_meeting(
    root: Path, stamp: str, title: str, lines: list[dict], **extra
) -> Session:
    """A meeting folder: transcript lines, a title and whatever `extra`
    holds (`project`, `names`, `todos`, `summary`)."""
    folder = root / stamp
    folder.mkdir()
    (folder / "transcript.jsonl").write_text(
        "".join(json.dumps({"ts": i, **line}) + "\n"
                for i, line in enumerate(lines))
    )
    meta = {"title": title}
    if "project" in extra:
        meta["project"] = extra["project"]
    (folder / "meta.json").write_text(json.dumps(meta))
    if "names" in extra:
        (folder / "speakers.json").write_text(json.dumps(extra["names"]))
    if "todos" in extra:
        (folder / "todos.json").write_text(json.dumps(extra["todos"]))
    if "summary" in extra:
        (folder / "summary.md").write_text(extra["summary"])
    return Session(folder)


class TestSearchFilters(unittest.TestCase):
    """Issue #40: every filter holds, then the phrase is looked for."""

    TODAY = date(2026, 11, 7)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        them = {"speaker": "them"}
        self.sessions = [
            write_meeting(
                root, "20261005-100000", "Alpha",
                [{**them, "who": "speaker_0", "text": "the budget is fine"}],
                project="lab", names={"speaker_0": "Priya"},
                todos=[{"text": "send the deck", "owner": "you"}],
            ),
            write_meeting(
                root, "20261015-100000", "Bravo",
                [{**them, "text": "see foo:bar for the numbers"}],
                project="lab",
                todos=[{"text": "they send it", "owner": "them"}],
            ),
            write_meeting(
                root, "20261019-100000", "Charlie",
                [{"speaker": "you", "text": "ask priya about the budget"}],
                project="thesis",
            ),
            write_meeting(
                root, "20260920-100000", "Budget review",
                [{"speaker": "you", "text": "nothing about money"}],
            ),
            write_meeting(
                root, "20261025-100000", "Echo",
                [{"speaker": "you", "text": "hello"}],
                project="Lab", summary="# Echo\n\nThe budget holds.\n",
            ),
        ]

    def found(self, query: str, today: date = TODAY) -> set[str]:
        return {
            s.title for s in self.sessions if s.matches(query, today)
        }

    def session(self, title: str) -> Session:
        return next(s for s in self.sessions if s.title == title)

    def test_a_phrase_alone_is_as_before(self):
        self.assertEqual(
            self.found("budget"), {"Alpha", "Charlie", "Budget review", "Echo"}
        )

    def test_project_keeps_the_meetings_of_that_project(self):
        self.assertEqual(self.found("budget project:lab"), {"Alpha", "Echo"})
        self.assertEqual(self.found("project:LA"), {"Alpha", "Bravo", "Echo"})

    def test_a_bang_leaves_them_out(self):
        self.assertEqual(
            self.found("!project:lab"), {"Charlie", "Budget review"}
        )
        self.assertEqual(self.found("budget !project:lab"),
                         {"Charlie", "Budget review"})

    def test_after_and_before_make_a_range(self):
        self.assertEqual(
            self.found("after:10/1 before:10/20"),
            {"Alpha", "Bravo", "Charlie"},
        )

    def test_the_days_named_are_in_the_range(self):
        self.assertEqual(
            self.found("after:10/5 before:10/15"), {"Alpha", "Bravo"}
        )

    def test_a_month_alone_is_all_of_it(self):
        """Budget review is in September, the rest in October."""
        october = {"Alpha", "Bravo", "Charlie", "Echo"}
        self.assertEqual(self.found("after:oct before:oct"), october)
        self.assertEqual(self.found("after:oct"), october)
        self.assertEqual(self.found("before:sep"), {"Budget review"})
        self.assertEqual(len(self.found("before:oct")), len(self.sessions))
        self.assertEqual(self.found("!after:10/1"), {"Budget review"})

    def test_a_date_with_no_year_is_the_last_one_to_have_come(self):
        """In January, October is last year's."""
        self.assertEqual(
            self.found("after:10/1 before:10/20", date(2027, 1, 5)),
            {"Alpha", "Bravo", "Charlie"},
        )
        self.assertEqual(
            self.found("after:10/1 before:10/20", date(2026, 9, 1)), set()
        )

    def test_speaker_reads_the_learned_names(self):
        """Charlie says "priya"; only Alpha has a voice called Priya."""
        self.assertEqual(self.found("speaker:priya"), {"Alpha"})
        self.assertEqual(self.found("speaker:PRI budget"), {"Alpha"})
        self.assertEqual(self.found("speaker:them"), {"Bravo"})
        self.assertEqual(
            self.found("speaker:you"), {"Charlie", "Budget review", "Echo"}
        )

    def test_has_todo_counts_as_the_row_does(self):
        """Bravo's to-do is somebody else's: no chip, no hit."""
        self.assertEqual(self.found("has:todo"), {"Alpha"})
        self.assertEqual(
            self.found("!has:todo budget"),
            {"Charlie", "Budget review", "Echo"},
        )

    def test_in_skips_a_hit_elsewhere(self):
        """Budget review has the word only in its title, Echo only in
        its summary."""
        self.assertEqual(
            self.found("in:transcript budget"), {"Alpha", "Charlie"}
        )
        self.assertEqual(
            self.found("!in:title budget"), {"Alpha", "Charlie", "Echo"}
        )
        hit = self.session("Alpha").locate("in:transcript budget")
        self.assertEqual((hit["where"], hit["line"]), ("transcript", 0))
        hit = self.session("Echo").locate("in:summary in:title budget")
        self.assertEqual(hit["where"], "summary")

    def test_an_unknown_key_is_searched_as_text(self):
        self.assertEqual(self.found("foo:bar"), {"Bravo"})

    def test_filters_alone_find_meetings_at_no_line(self):
        hit = self.session("Alpha").locate("project:lab", self.TODAY)
        self.assertEqual(hit, {"where": "", "line": -1})
        self.assertEqual(len(self.found("in:summary")), len(self.sessions))


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
        text, offer = words.summary_missing(
            12, state=NotesState.RUNNING, phase=NotesPhase.SUMMARIZING
        )
        self.assertEqual(text, "Writing the summary…")
        self.assertFalse(offer)

    def test_a_joined_job_says_already(self):
        text, offer = words.summary_missing(
            12,
            state=NotesState.RUNNING,
            phase=NotesPhase.SUMMARIZING,
            joined=True,
        )
        self.assertEqual(text, "Already writing the summary…")
        self.assertFalse(offer)

    def test_a_failure_says_why_and_still_offers_the_retry(self):
        text, offer = words.summary_missing(
            12, state=NotesState.FAILED, error="host down"
        )
        self.assertEqual(text, "No summary: host down")
        self.assertTrue(offer)

    def test_a_failure_without_a_reason_says_so(self):
        text, offer = words.summary_missing(12, state=NotesState.FAILED)
        self.assertIn("No summary:", text)
        self.assertTrue(offer)


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
            citations.Coverage(items=3, cited=0),
            state=NotesState.RUNNING,
            phase=NotesPhase.SUMMARIZING,
        )
        self.assertEqual(note, "Writing the summary…")
        self.assertEqual(
            words.summary_note(
                citations.Coverage(3, 3),
                state=NotesState.RUNNING,
                phase=NotesPhase.TRANSCRIBING,
            ),
            "Transcribing your side…",
        )

    def test_a_failed_rewrite_says_why(self):
        self.assertEqual(
            words.summary_note(
                citations.Coverage(3, 3),
                state=NotesState.FAILED,
                error="host down",
            ),
            "host down",
        )

    def test_what_is_said_is_escaped_for_markup(self):
        self.assertEqual(
            words.summary_note(
                citations.Coverage(),
                state=NotesState.FAILED,
                error="a < b",
            ),
            "a &lt; b",
        )


class TestProjectNotesProblem(unittest.TestCase):
    """A page left alone, or a write that failed, is told; the rest of
    what a write can come to is not worth a toast."""

    def change(self, action: str, note: str = "") -> projectnotes.Change:
        return projectnotes.Change(action, Path("/p/notes/meetings/a.md"), note)

    def problem(self, **given) -> str:
        return words.project_notes_problem(projectnotes.Result("demo", **given))

    def test_a_page_that_is_not_steno_s_is_named(self):
        said = self.problem(changes=[self.change("skip", "not written by steno")])
        self.assertIn("a.md", said)
        self.assertIn("not written by steno", said)

    def test_a_failed_write_says_why(self):
        self.assertIn("No space", self.problem(error="No space left on device"))

    def test_many_pages_are_not_all_listed(self):
        said = self.problem(changes=[self.change("skip", "x")] * 5)
        self.assertEqual(said.count("a.md"), words.NAMED_AT_MOST)
        self.assertIn("…", said)

    def test_a_write_that_went_well_or_nowhere_says_nothing(self):
        for given in (
            {},
            {"changes": [self.change("create"), self.change("same")]},
            {"note": "no such project: /p/x"},
            {"note": "STENO_PROJECT_NOTES='0': nothing written"},
        ):
            with self.subTest(**given):
                self.assertEqual(self.problem(**given), "")
