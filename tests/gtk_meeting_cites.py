"""GTK checks for "where was this said" on a past meeting (issue #26).

Run by test_meeting_cites.py, in a child process on a Broadway display
and never on a real one. The name keeps test discovery from importing
this file in the main process, where `import gi` would connect to the
desktop.

Clicks and keys are the handlers the gestures and key controllers run,
emitted on the widget they are attached to: there is no pointer on
Broadway to move.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import json
import sys
import tempfile
import unittest
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")

from gi.repository import Gdk, Gtk

from steno.gui.meeting import MeetingPage
from steno.gui.window import Window
from tests.gtk_meeting_scroll import on_screen, scroller_of, wait_for
from tests.gtk_sidebar_menu import StubApp, controllers, settle

LINES = 200
STEP_S = 5
STARTED = 1000.0
SUMMARY = """# Cited call

## TL;DR
Planning went fine.

## Decisions
- Opening point <!--cite 5-->
- Pay the deposit by Friday <!--cite 600-610-->
- Ship in March <!--cite 100,800-->
- Nothing cited about this one

## Action items
**You**
- Send the figures <!--cite 400-->
"""
PLAIN_SUMMARY = "# Plain call\n\n## Decisions\n- Nothing is cited here\n"
# The lines each item points at: line n is said at second 5 * n.
OPENING = [1]
DEPOSIT = [120, 121, 122]
SHIP = [20, 160]
MEETINGS = {"20260110-090000": "Plain", "20260111-090000": "Cited"}


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


def make_meetings(root: Path) -> None:
    for stamp, title in MEETINGS.items():
        folder = root / stamp
        folder.mkdir(parents=True)
        count = LINES if title == "Cited" else 3
        records = [
            {
                "speaker": "you" if n % 2 else "them",
                "text": f"line {n} of the meeting, long enough to wrap " * 2,
                "ts": STARTED + STEP_S * n,
            }
            for n in range(count)
        ]
        (folder / "transcript.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records)
        )
        (folder / "meta.json").write_text(
            json.dumps({"title": title, "started_at": STARTED})
        )
        summary = SUMMARY if title == "Cited" else PLAIN_SUMMARY
        (folder / "summary.md").write_text(summary)


def click(widget: Gtk.Widget) -> None:
    (gesture,) = controllers(widget, Gtk.GestureClick)
    gesture.emit("released", 1, 4.0, 4.0)


def press(widget: Gtk.Widget, key: int) -> bool:
    (keys,) = controllers(widget, Gtk.EventControllerKey)
    return bool(keys.emit("key-pressed", key, 0, Gdk.ModifierType(0)))


class CitedMeeting(unittest.TestCase):
    """A window on a long meeting whose summary says where it was said."""

    def setUp(self):
        # PyGObject reports an exception in a signal handler or an idle
        # callback through sys.excepthook and carries on, so a handler
        # that raises would otherwise look like one that did nothing.
        self.raised: list[BaseException] = []
        self._hook = sys.excepthook
        sys.excepthook = lambda _kind, value, _tb: self.raised.append(value)

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make_meetings(self.root)
        self.app = StubApp()
        self.app.register(None)
        self.window = Window(self.app, self.root)
        self.window.present()
        settle()
        self.page: MeetingPage = self.window.meeting
        self.assertEqual(self.page.current.title, "Cited")
        self.assertTrue(
            wait_for(lambda: self.page._rows[LINES - 1].get_height() > 0),
            "the transcript was never laid out",
        )
        self.view = self.page.summary_view

    def tearDown(self):
        sys.excepthook = self._hook
        self.window.destroy()
        settle()
        self._tmp.cleanup()
        self.assertEqual(self.raised, [])

    def item(self, words: str):
        """The summary item whose text has these words."""
        (found,) = [
            s for s in self.view.sources if words in s.markup
        ]
        return found

    def lit(self) -> list[int]:
        return [
            i for i, row in enumerate(self.page._rows)
            if row.has_css_class("line-cited")
        ]

    def scroll_top(self) -> None:
        scroller_of(self.page.transcript_list).get_vadjustment().set_value(0)


class TestSummaryRows(CitedMeeting):
    def test_only_the_items_that_cite_can_be_chosen(self):
        self.assertEqual(
            [s.markup for s in self.view.sources],
            ["Opening point", "Pay the deposit by Friday", "Ship in March"],
        )
        for source in self.view.sources:
            self.assertTrue(source.mark.has_css_class("cited-item"))
            self.assertTrue(source.mark.get_focusable())
            self.assertEqual(source.mark.get_cursor().get_name(), "pointer")

    def test_a_cited_item_says_how_many_places_in_its_tooltip(self):
        self.assertEqual(
            self.item("Ship").mark.get_tooltip_text(),
            "Show where this was said (2 places)",
        )
        self.assertEqual(
            self.item("Pay").mark.get_tooltip_text(),
            "Show where this was said (1 place)",
        )

    def test_a_cited_item_ends_in_its_count_of_places(self):
        # get_text() is empty when the markup does not parse.
        for words, text in (
            ("Ship", "Ship in March 2"),
            ("Pay", "Pay the deposit by Friday 1"),
        ):
            self.assertEqual(self.item(words).mark.get_last_child().get_text(), text)

    def test_the_action_items_live_in_the_todo_list_not_the_summary(self):
        self.assertNotIn(
            "Send the figures", [s.markup for s in self.view.sources]
        )

    def test_an_item_with_no_cite_is_plain_text_you_can_select(self):
        rows = []
        row = self.view.get_first_child()
        while row is not None:
            rows.append(row)
            row = row.get_next_sibling()
        (plain,) = [
            r for r in rows
            if r.has_css_class("summary-item")
            and not r.has_css_class("cited-item")
            and "Nothing cited" in r.get_last_child().get_label()
        ]
        text = plain.get_last_child()
        self.assertTrue(text.get_selectable())
        self.assertEqual(controllers(plain, Gtk.GestureClick), [])

        text.select_region(2, 9)

        self.assertEqual(text.get_selection_bounds(), (True, 2, 9))

    def test_nothing_is_lit_until_an_item_is_chosen(self):
        self.assertEqual(self.lit(), [])
        self.assertIsNone(self.page._active)


class TestChoosingAnItem(CitedMeeting):
    def test_choosing_an_item_lights_exactly_the_lines_it_cites(self):
        self.scroll_top()
        click(self.item("deposit").mark)

        self.assertEqual(self.lit(), DEPOSIT)
        self.assertTrue(self.item("deposit").mark.has_css_class("item-active"))
        for other in ("Opening", "Ship"):
            self.assertFalse(
                self.item(other).mark.has_css_class("item-active")
            )

    def test_a_cite_of_two_places_lights_both(self):
        click(self.item("Ship").mark)
        self.assertEqual(self.lit(), SHIP)

    def test_the_transcript_scrolls_so_the_first_lit_line_is_on_screen(self):
        self.scroll_top()
        adjustment = scroller_of(self.page.transcript_list).get_vadjustment()
        self.assertFalse(on_screen(self.page._rows[DEPOSIT[0]]))

        click(self.item("deposit").mark)

        self.assertGreater(adjustment.get_value(), 0)
        self.assertTrue(on_screen(self.page._rows[DEPOSIT[0]]))

    def test_a_first_line_already_on_screen_does_not_move_the_page(self):
        self.scroll_top()
        adjustment = scroller_of(self.page.transcript_list).get_vadjustment()
        first = self.page._rows[OPENING[0]]
        self.assertTrue(on_screen(first))

        click(self.item("Opening").mark)

        self.assertEqual(self.lit(), OPENING)
        self.assertEqual(adjustment.get_value(), 0)

    def test_the_stacked_layout_scrolls_its_own_scroller(self):
        wide = scroller_of(self.page.transcript_list)
        self.page.set_stacked(True)
        stacked = scroller_of(self.page.transcript_list)
        self.assertIsNot(stacked, wide)
        adjustment = stacked.get_vadjustment()
        self.assertTrue(
            wait_for(lambda: adjustment.get_upper() > adjustment.get_page_size()),
            "the stacked layout never laid out the transcript",
        )
        self.scroll_top()
        self.assertFalse(on_screen(self.page._rows[DEPOSIT[0]]))

        click(self.item("deposit").mark)

        self.assertEqual(self.lit(), DEPOSIT)
        self.assertGreater(adjustment.get_value(), 0)
        self.assertTrue(on_screen(self.page._rows[DEPOSIT[0]]))

    def test_choosing_another_item_moves_the_lines(self):
        click(self.item("deposit").mark)
        click(self.item("Ship").mark)

        self.assertEqual(self.lit(), SHIP)
        self.assertFalse(self.item("deposit").mark.has_css_class("item-active"))
        self.assertTrue(self.item("Ship").mark.has_css_class("item-active"))

    def test_choosing_the_same_item_again_puts_the_lines_out(self):
        click(self.item("deposit").mark)
        click(self.item("deposit").mark)

        self.assertEqual(self.lit(), [])
        self.assertFalse(self.item("deposit").mark.has_css_class("item-active"))

    def test_enter_and_space_choose_the_focused_item(self):
        mark = self.item("deposit").mark
        self.assertTrue(press(mark, Gdk.KEY_Return))
        self.assertEqual(self.lit(), DEPOSIT)
        self.assertTrue(press(mark, Gdk.KEY_space))
        self.assertEqual(self.lit(), [])
        self.assertFalse(press(mark, Gdk.KEY_a))

    def test_a_second_press_of_a_double_click_does_not_undo_the_first(self):
        mark = self.item("deposit").mark
        (gesture,) = controllers(mark, Gtk.GestureClick)
        gesture.emit("released", 1, 4.0, 4.0)
        gesture.emit("released", 2, 4.0, 4.0)
        self.assertEqual(self.lit(), DEPOSIT)

    def test_escape_in_the_page_puts_the_lines_out(self):
        click(self.item("deposit").mark)

        self.assertTrue(press(self.page, Gdk.KEY_Escape))

        self.assertEqual(self.lit(), [])
        self.assertFalse(self.item("deposit").mark.has_css_class("item-active"))
        # With nothing lit, Escape is left for whoever else wants it.
        self.assertFalse(press(self.page, Gdk.KEY_Escape))

    def test_playing_a_lit_line_leaves_the_others_lit(self):
        click(self.item("deposit").mark)

        self.page.transcript_list.emit(
            "row-activated", self.page._rows[DEPOSIT[1]]
        )

        self.assertEqual(self.lit(), DEPOSIT)
        self.assertTrue(
            self.page._rows[DEPOSIT[1]].has_css_class("line-playing")
        )


class TestStaleState(CitedMeeting):
    def test_opening_another_meeting_clears_it(self):
        click(self.item("deposit").mark)
        old_mark = self.item("deposit").mark

        self.window.sidebar.list.select_row(
            self.window.sidebar.list.get_row_at_index(1)
        )

        self.assertEqual(self.page.current.title, "Plain")
        self.assertIsNone(self.page._active)
        self.assertEqual(self.lit(), [])
        self.assertFalse(old_mark.has_css_class("item-active"))

    def test_a_rebuilt_transcript_lights_the_same_lines_again(self):
        click(self.item("deposit").mark)
        old_rows = list(self.page._rows)
        path = self.page.current.dir / "transcript.jsonl"
        extra = {"speaker": "them", "text": "one more", "ts": STARTED + 2000}
        path.write_text(path.read_text() + json.dumps(extra) + "\n")

        self.page.refresh_transcript()

        self.assertEqual(len(self.page._rows), LINES + 1)
        self.assertIsNot(self.page._rows[0], old_rows[0])
        self.assertEqual(self.lit(), DEPOSIT)
        self.assertTrue(self.item("deposit").mark.has_css_class("item-active"))

    def test_a_transcript_that_did_not_change_keeps_what_is_lit(self):
        click(self.item("deposit").mark)
        rows = list(self.page._rows)

        self.page.refresh_transcript()

        self.assertEqual(self.page._rows, rows)
        self.assertEqual(self.lit(), DEPOSIT)

    def test_a_new_summary_keeps_nothing_lit(self):
        click(self.item("deposit").mark)

        self.page.show_summary(self.page.current)

        self.assertIsNone(self.page._active)
        self.assertEqual(self.lit(), [])
        self.assertEqual(len(self.view.sources), 3)

    def test_reloading_the_meeting_keeps_nothing_lit(self):
        click(self.item("deposit").mark)

        self.page.reload()

        self.assertIsNone(self.page._active)
        self.assertEqual(self.lit(), [])


if __name__ == "__main__":
    unittest.main()
