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
import wave
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")

from gi.repository import Gdk, Graphene, Gtk

from steno.gui.meeting import MeetingPage
from steno.gui.style import FALLBACK, stylesheet
from steno.gui.window import Window
from steno.notes import ensure_todos, load_todos
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
- Draft the table

**Them**
- Revised quote next week <!--cite 300-->
"""
PLAIN_SUMMARY = "# Plain call\n\n## Decisions\n- Nothing is cited here\n"
# The lines each item points at: line n is said at second 5 * n.
FIGURES = [80]  # the to-do "Send the figures"
QUOTE = [60]  # waiting on them: "Revised quote next week"
OPENING = [1]
DEPOSIT = [120, 121, 122]
SHIP = [20, 160]
MEETINGS = {"20260110-090000": "Plain", "20260111-090000": "Cited"}


GONE = "The audio for this meeting is gone"
RECORDING = "Playback is off while recording; it would be recorded as them"


class FakePlayer:
    """Stands in for the GStreamer player: nothing here makes a sound."""

    def __init__(self, on_tick=None) -> None:
        self.on_tick = on_tick
        self.tracks: dict = {}
        self.played: list[float] = []

    def available(self) -> bool:
        return True

    def play(self, tracks, seconds: float = 0.0) -> None:
        self.tracks = dict(tracks)
        self.played.append(seconds)

    def set_levels(self, levels) -> None:
        pass

    def stop(self) -> None:
        self.tracks = {}


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
                "offset": float(STEP_S * n),
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
        if title == "Cited":
            for name in ("you.wav", "them.wav"):
                silence(folder / name)


def silence(path: Path) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(bytes(2 * 8000))


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
        # The page is built with this in place of the real player, so no
        # test can open an audio device.
        patch = mock.patch("steno.gui.meeting.Player", FakePlayer)
        patch.start()
        self.addCleanup(patch.stop)
        self.window = Window(self.app, self.root)
        self.window.present()
        settle()
        self.page: MeetingPage = self.window.meeting
        self.assertEqual(self.page.current.title, "Cited")
        self.player = self.page.player
        self.toasts: list[str] = []
        self.page._toast = lambda text, **_kw: self.toasts.append(text)
        self.assertTrue(self.page.player_bar.get_visible())
        self.assertTrue(
            wait_for(lambda: self.page._rows[LINES - 1].get_height() > 0),
            "the transcript was never laid out",
        )
        self.view = self.page.summary_view
        self.assertTrue(
            wait_for(lambda: self.view.sources[0].mark.get_width() > 0),
            "the summary was never laid out",
        )

    def tearDown(self):
        sys.excepthook = self._hook
        self.window.destroy()
        settle()
        self._tmp.cleanup()
        self.assertEqual(self.raised, [])

    def item(self, words: str):
        """The summary item whose text has these words."""
        (found,) = [s for s in self.view.sources if words in s.markup]
        return found

    def lit(self) -> list[int]:
        return [
            i
            for i, row in enumerate(self.page._rows)
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
            # A selectable label takes the click, with a text cursor.
            self.assertFalse(source.mark.get_last_child().get_selectable())

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
            self.assertEqual(
                self.item(words).mark.get_last_child().get_text(), text
            )

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
            r
            for r in rows
            if r.has_css_class("summary-item")
            and not r.has_css_class("cited-item")
            and "Nothing cited" in r.get_last_child().get_label()
        ]
        text = plain.get_last_child()
        self.assertTrue(text.get_selectable())
        self.assertEqual(controllers(plain, Gtk.GestureClick), [])

        text.select_region(2, 9)

        self.assertEqual(text.get_selection_bounds(), (True, 2, 9))

    def tab_stops(self, root: Gtk.Widget, count: int) -> list[Gtk.Widget]:
        """The widgets Tab visits inside `root`, from before the first."""
        self.window.set_focus(None)
        stops = []
        for _ in range(count):
            root.child_focus(Gtk.DirectionType.TAB_FORWARD)
            stops.append(self.window.get_focus())
        return stops

    def test_tab_visits_each_cited_item_in_turn_and_moves_on(self):
        """GtkBox hands focus to its children, which cannot take it; the
        items would be skipped, and Enter and Space unreachable."""
        marks = [s.mark for s in self.view.sources]
        stops = self.tab_stops(self.page, 6)
        found = [stop for stop in stops if stop in marks]
        self.assertEqual(found, marks)
        # Each takes one Tab: the stop after it is not the same widget.
        for mark in marks:
            at = stops.index(mark)
            self.assertIsNot(stops[at + 1], mark)

    def test_enter_on_the_item_tab_reached_lights_its_lines(self):
        marks = [s.mark for s in self.view.sources]
        stops = self.tab_stops(self.page, 6)
        reached = stops[stops.index(marks[1])]
        self.assertTrue(press(reached, Gdk.KEY_Return))
        self.assertEqual(self.lit(), DEPOSIT)

    def test_tab_goes_from_a_to_dos_box_to_its_words(self):
        todo = next(
            s.mark for s in self.page._todo_sources if "figures" in s.markup
        )
        check, _label, target = todo_parts(todo)
        # Tab goes by where things are: they have to be placed first.
        self.assertTrue(wait_for(lambda: target.get_width() > 0))
        self.window.set_focus(check)

        self.assertTrue(
            self.page.todo_box.child_focus(Gtk.DirectionType.TAB_FORWARD)
        )

        self.assertIs(self.window.get_focus(), target)

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
            wait_for(
                lambda: adjustment.get_upper() > adjustment.get_page_size()
            ),
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
        self.assertFalse(
            self.item("deposit").mark.has_css_class("item-active")
        )
        self.assertTrue(self.item("Ship").mark.has_css_class("item-active"))

    def test_a_click_brings_the_focus_into_the_page_so_escape_gets_there(self):
        """Opening a meeting leaves the focus in the list, where Esc would
        not reach the page."""
        self.window.sidebar.list.get_row_at_index(0).grab_focus()
        settle()
        self.assertFalse(self.window.get_focus().is_ancestor(self.page))

        mark = self.item("deposit").mark
        click(mark)

        self.assertIs(self.window.get_focus(), mark)
        self.assertTrue(self.window.get_focus().is_ancestor(self.page))

    def test_choosing_the_same_item_again_puts_the_lines_out(self):
        click(self.item("deposit").mark)
        click(self.item("deposit").mark)

        self.assertEqual(self.lit(), [])
        self.assertFalse(
            self.item("deposit").mark.has_css_class("item-active")
        )

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
        self.assertFalse(
            self.item("deposit").mark.has_css_class("item-active")
        )
        # With nothing lit, Escape is left for whoever else wants it.
        self.assertFalse(press(self.page, Gdk.KEY_Escape))

    def after_the_double_click_time(self) -> None:
        ms = Gtk.Settings.get_default().props.gtk_double_click_time
        wait_for(lambda: False, ms / 1000 + 0.1)

    def test_playing_a_lit_line_leaves_the_others_lit(self):
        click(self.item("deposit").mark)
        self.after_the_double_click_time()

        self.page.transcript_list.emit(
            "row-activated", self.page._rows[DEPOSIT[1]]
        )

        self.assertEqual(self.lit(), DEPOSIT)
        self.assertEqual(self.player.played, [599.6 + 5])
        self.assertTrue(
            self.page._rows[DEPOSIT[1]].has_css_class("line-playing")
        )

    def test_a_double_click_does_not_play_the_line_it_lands_on(
        self,
    ):
        """The first click took the page to the lines, so the second one
        is on a line, not on the item."""
        click(self.item("deposit").mark)
        self.page.transcript_list.emit(
            "row-activated", self.page._rows[DEPOSIT[0]]
        )
        self.assertEqual(self.player.played, [])
        self.assertEqual(self.page._playing_index, -1)

        self.after_the_double_click_time()
        self.page.transcript_list.emit(
            "row-activated", self.page._rows[DEPOSIT[0]]
        )
        self.assertEqual(self.player.played, [599.6])

    def test_a_click_on_a_line_with_nothing_lit_plays_at_once(self):
        self.page.transcript_list.emit("row-activated", self.page._rows[3])
        self.assertEqual(self.player.played, [14.6])


def children(widget: Gtk.Widget) -> list[Gtk.Widget]:
    found = []
    child = widget.get_first_child()
    while child is not None:
        found.append(child)
        child = child.get_next_sibling()
    return found


def texts(button: Gtk.Button) -> list[str]:
    """The words on a place row, left to right."""
    return [
        c.get_label()
        for c in children(button.get_child())
        if isinstance(c, Gtk.Label)
    ]


class TestTheBar(CitedMeeting):
    def setUp(self):
        super().setUp()
        self.bar = self.page.cite_bar

    def buttons(self) -> list[Gtk.Button]:
        return list(self.bar._buttons)

    def press_place(self, index: int) -> None:
        self.buttons()[index].emit("clicked")

    def test_nothing_is_shown_until_an_item_is_chosen(self):
        self.assertFalse(self.bar.get_visible())

    def test_a_place_is_listed_with_its_time_speaker_and_words(self):
        click(self.item("deposit").mark)

        self.assertTrue(self.bar.get_visible())
        self.assertEqual(self.bar.count.get_text(), "1 place")
        (button,) = self.buttons()
        stamp, who, quote, more = texts(button)
        self.assertEqual(
            (stamp, who, more), ("10:00", "Them", "+2 more lines")
        )
        self.assertTrue(quote.startswith("“line 120 of the meeting"), quote)
        self.assertTrue(quote.endswith("”"))
        # A single place has nothing to step to.
        self.assertFalse(self.bar.up.get_visible())
        self.assertFalse(self.bar.down.get_visible())

    def test_it_names_the_item_it_is_for(self):
        click(self.item("deposit").mark)
        self.assertEqual(
            self.bar.title.get_text(), "Pay the deposit by Friday"
        )

    def test_it_sits_outside_the_scrolling_page(self):
        """In the scroller it would go off screen with the jump it is for."""
        click(self.item("Ship").mark)
        self.assertIsNone(self.bar.get_ancestor(Gtk.ScrolledWindow))
        self.assertIs(self.bar.get_parent(), self.page)

    def test_the_transcript_gives_it_room_and_takes_it_back(self):
        adjustment = scroller_of(self.page.transcript_list).get_vadjustment()
        before = adjustment.get_page_size()

        def taken() -> float:
            # The border box: get_height() leaves out the bar's padding.
            return before - adjustment.get_page_size()

        click(self.item("Ship").mark)
        bar = self.bar.get_allocation().height
        self.assertTrue(
            wait_for(
                lambda: bar > 0 and taken() == self.bar.get_allocation().height
            ),
            f"the page gave up {taken()} of {before}, the bar took {bar}",
        )
        self.assertLessEqual(self.bar.scroller.get_height(), 120)

        press(self.page, Gdk.KEY_Escape)

        self.assertFalse(self.bar.get_visible())
        self.assertTrue(wait_for(lambda: taken() == 0))

    def test_two_places_are_listed_in_the_order_of_the_transcript(self):
        click(self.item("Ship").mark)

        self.assertEqual(self.bar.count.get_text(), "2 places")
        first, second = (texts(b) for b in self.buttons())
        self.assertEqual((first[0], second[0]), ("01:40", "13:20"))
        self.assertEqual(len(first), 3)  # no "+k more lines" on a single line
        self.assertTrue(self.bar.up.get_visible())
        self.assertTrue(self.bar.down.get_visible())

    def test_the_steps_walk_the_places_and_scroll_the_transcript(self):
        scroller = scroller_of(self.page.transcript_list)
        adjustment = scroller.get_vadjustment()
        self.scroll_top()
        click(self.item("Ship").mark)
        first, second = (self.page._rows[i] for i in SHIP)
        self.assertEqual(self.page._place, 0)
        self.assertFalse(self.bar.up.get_sensitive())
        self.assertTrue(self.bar.down.get_sensitive())
        self.assertTrue(self.buttons()[0].has_css_class("place-current"))

        self.bar.down.emit("clicked")

        self.assertEqual(self.page._place, 1)
        self.assertTrue(on_screen(second))
        self.assertGreater(adjustment.get_value(), 0)
        self.assertTrue(self.buttons()[1].has_css_class("place-current"))
        self.assertFalse(self.buttons()[0].has_css_class("place-current"))
        self.assertTrue(self.bar.up.get_sensitive())
        self.assertFalse(self.bar.down.get_sensitive())
        # Past the last place there is nowhere to go.
        self.bar.down.emit("clicked")
        self.assertEqual(self.page._place, 1)

        self.bar.up.emit("clicked")

        self.assertEqual(self.page._place, 0)
        self.assertTrue(on_screen(first))
        self.assertEqual(self.lit(), SHIP)
        self.assertEqual(self.player.played, [])

    def test_a_step_to_a_line_with_no_place_yet_asks_again_until_it_has(self):
        """The page scrolls with the search jump's `_scroll_to_row`,
        whose True means "not laid out yet: ask again"."""
        click(self.item("Ship").mark)
        self.scroll_top()
        asked = []
        real = self.page._scroll_to_row
        not_placed_yet = 2  # answers of True before it is

        def late(row, give_up):
            asked.append(row)
            if len(asked) <= not_placed_yet:
                return True
            return real(row, give_up)

        self.page._scroll_to_row = late

        self.bar.down.emit("clicked")

        second = self.page._rows[SHIP[1]]
        self.assertTrue(wait_for(lambda: on_screen(second)))
        self.assertEqual(len(asked), not_placed_yet + 1)
        self.assertEqual(set(asked), {second})

    def test_a_long_list_scrolls_inside_the_bar_and_follows_the_steps(self):
        folder = self.page.current.dir
        (folder / "summary.md").write_text(
            "# T\n\n## Key facts\n"
            "- Said often <!--cite 10,50,90,130,170,195-->\n"
        )
        self.page.reload()
        click(self.item("often").mark)
        adjustment = self.bar.scroller.get_vadjustment()
        self.assertEqual(self.bar.count.get_text(), "6 places")
        self.assertTrue(wait_for(lambda: self.bar.scroller.get_height() > 0))
        self.assertLessEqual(self.bar.scroller.get_height(), 120)
        self.assertGreater(adjustment.get_upper(), adjustment.get_page_size())
        self.assertEqual(adjustment.get_value(), 0)

        for _ in range(5):
            self.bar.down.emit("clicked")

        last = self.buttons()[5]
        self.assertTrue(last.has_css_class("place-current"))
        box = last.get_allocation()
        self.assertGreater(adjustment.get_value(), 0)
        self.assertLessEqual(adjustment.get_value(), box.y)
        self.assertGreaterEqual(
            adjustment.get_value() + adjustment.get_page_size(),
            box.y + box.height,
        )

    def test_a_place_plays_from_where_its_first_line_starts(self):
        click(self.item("deposit").mark)

        self.press_place(0)

        # The line starts at second 600; playback leads in by 0.4 s.
        self.assertEqual(self.player.played, [599.6])
        self.assertTrue(self.page._rows[120].has_css_class("line-playing"))
        self.assertEqual(self.lit(), DEPOSIT)
        self.assertEqual(self.toasts, [])

    def test_a_far_place_scrolls_and_plays_the_line_it_names(self):
        click(self.item("Ship").mark)
        self.scroll_top()

        self.press_place(1)

        self.assertEqual(self.player.played, [799.6])
        self.assertEqual(self.page._playing_index, SHIP[1])
        self.assertTrue(on_screen(self.page._rows[SHIP[1]]))
        self.assertEqual(self.page._place, 1)

    def test_while_recording_it_scrolls_and_says_why_nothing_plays(self):
        self.page._is_recording = lambda: True
        click(self.item("Ship").mark)
        self.scroll_top()

        self.press_place(1)

        self.assertEqual(self.player.played, [])
        self.assertEqual(self.toasts, [RECORDING])
        self.assertTrue(on_screen(self.page._rows[SHIP[1]]))
        self.assertEqual(self.lit(), SHIP)

    def test_when_the_audio_is_gone_it_scrolls_and_says_so(self):
        click(self.item("Ship").mark)
        self.scroll_top()
        for wav in self.page.current.dir.glob("*.wav"):
            wav.unlink()

        self.press_place(1)

        self.assertEqual(self.player.played, [])
        self.assertEqual(self.toasts, [GONE])
        self.assertTrue(on_screen(self.page._rows[SHIP[1]]))

    def test_a_meeting_with_no_audio_still_lights_and_scrolls_without_play(
        self,
    ):
        for wav in self.page.current.dir.glob("*.wav"):
            wav.unlink()
        self.page.reload()
        self.assertFalse(self.page.player_bar.get_visible())
        self.scroll_top()

        click(self.item("deposit").mark)

        self.assertEqual(self.lit(), DEPOSIT)
        self.assertTrue(on_screen(self.page._rows[DEPOSIT[0]]))
        (button,) = self.buttons()
        self.assertFalse(
            [
                c
                for c in children(button.get_child())
                if isinstance(c, Gtk.Image)
            ]
        )
        self.assertEqual(button.get_tooltip_text(), "Show this place")

    def test_with_audio_a_place_has_a_play_icon_and_says_it_plays(self):
        click(self.item("deposit").mark)
        (button,) = self.buttons()
        self.assertEqual(
            len(
                [
                    c
                    for c in children(button.get_child())
                    if isinstance(c, Gtk.Image)
                ]
            ),
            1,
        )
        self.assertEqual(button.get_tooltip_text(), "Play from here")

    def test_the_close_button_puts_the_item_out(self):
        click(self.item("deposit").mark)
        close = self.bar.get_first_child().get_last_child()

        close.emit("clicked")

        self.assertFalse(self.bar.get_visible())
        self.assertEqual(self.lit(), [])
        self.assertIsNone(self.page._active)

    def test_another_meeting_takes_it_away(self):
        click(self.item("deposit").mark)

        self.window.sidebar.list.select_row(
            self.window.sidebar.list.get_row_at_index(1)
        )

        self.assertFalse(self.bar.get_visible())
        self.assertEqual(self.buttons(), [])

    def test_a_rebuilt_transcript_keeps_the_bar_for_the_same_item(self):
        click(self.item("Ship").mark)
        path = self.page.current.dir / "transcript.jsonl"
        extra = {
            "speaker": "them",
            "text": "one more",
            "ts": STARTED + 2000,
            "offset": 2000.0,
        }
        path.write_text(path.read_text() + json.dumps(extra) + "\n")

        self.page.refresh_transcript()

        self.assertTrue(self.bar.get_visible())
        self.assertEqual(self.bar.count.get_text(), "2 places")
        self.assertEqual(self.lit(), SHIP)
        self.assertEqual(self.page._place, 0)


def todo_parts(row: Gtk.Box) -> tuple[Gtk.CheckButton, Gtk.Label, Gtk.Widget]:
    """A to-do row's box, its words, and what is clicked to see where they
    were said. A to-do that cites keeps the words beside the box, in
    something to click; one that does not, inside the box."""
    first = row.get_first_child()
    if isinstance(first, Gtk.CheckButton):
        return first, first.get_child(), first.get_child()
    check, target = children(first)
    return check, target.get_first_child(), target


def find_css(root: Gtk.Widget, name: str) -> Gtk.Widget | None:
    """The first widget at or under `root` carrying the `name` CSS class."""
    if root.has_css_class(name):
        return root
    for child in children(root):
        found = find_css(child, name)
        if found is not None:
            return found
    return None


class TestTodos(CitedMeeting):
    def todo_rows(self) -> dict[str, Gtk.Box]:
        """The to-do rows by their words."""
        return {
            todo_parts(row)[1].get_label(): row
            for row in children(self.page.todo_box)
            if isinstance(row, Gtk.Box)
        }

    def words_of(self, text: str) -> Gtk.Label:
        return todo_parts(self.todo_rows()[text])[1]

    def target_of(self, text: str) -> Gtk.Widget:
        return todo_parts(self.todo_rows()[text])[2]

    def check_of(self, text: str) -> Gtk.CheckButton:
        return todo_parts(self.todo_rows()[text])[0]

    def test_a_to_do_that_cites_has_words_to_click_beside_its_box(self):
        rows = self.todo_rows()
        self.assertEqual(
            sorted(rows),
            ["Draft the table", "Revised quote next week", "Send the figures"],
        )
        cited = self.target_of("Send the figures")
        self.assertTrue(cited.has_css_class("cited-item"))
        self.assertTrue(cited.get_focusable())
        self.assertIsNone(self.check_of("Send the figures").get_child())
        self.assertEqual(
            cited.get_tooltip_text(), "Show where this was said (1 place)"
        )
        self.assertEqual(cited.get_cursor().get_name(), "pointer")
        # One that cites nothing is as it was: the box's own label.
        plain = self.words_of("Draft the table")
        self.assertFalse(plain.has_css_class("cited-item"))
        self.assertIs(plain.get_parent(), self.check_of("Draft the table"))

    def test_clicking_the_words_lights_the_lines_and_leaves_the_box_alone(
        self,
    ):
        self.scroll_top()
        row = self.todo_rows()["Send the figures"]
        check = self.check_of("Send the figures")

        click(self.target_of("Send the figures"))

        self.assertEqual(self.lit(), FIGURES)
        self.assertTrue(row.has_css_class("item-active"))
        self.assertFalse(check.get_active())
        self.assertFalse(
            next(
                t
                for t in load_todos(self.page.current.dir)
                if "figures" in t.text
            ).done
        )
        self.assertTrue(self.bar_shows("Send the figures"))
        self.assertTrue(on_screen(self.page._rows[FIGURES[0]]))

    def bar_shows(self, title: str) -> bool:
        bar = self.page.cite_bar
        return bar.get_visible() and bar.title.get_text() == title

    def test_what_you_wait_on_can_be_clicked_too(self):
        click(self.target_of("Revised quote next week"))
        self.assertEqual(self.lit(), QUOTE)

    def test_the_box_still_ticks_and_the_tick_leaves_the_lines_lit(self):
        click(self.target_of("Send the figures"))
        check = self.check_of("Send the figures")

        check.set_active(True)

        todo = next(
            t for t in load_todos(self.page.current.dir) if "figures" in t.text
        )
        self.assertTrue(todo.done)
        self.assertTrue(
            self.words_of("Send the figures").has_css_class("todo-done")
        )
        self.assertEqual(self.lit(), FIGURES)

    def test_the_words_of_a_plain_to_do_still_tick_its_box(self):
        check = self.check_of("Draft the table")
        self.assertIs(self.words_of("Draft the table").get_parent(), check)
        self.assertFalse(check.get_active())

    def test_one_item_at_a_time_across_the_summary_and_the_to_dos(self):
        click(self.item("deposit").mark)
        click(self.target_of("Send the figures"))
        self.assertEqual(self.lit(), FIGURES)
        self.assertFalse(
            self.item("deposit").mark.has_css_class("item-active")
        )

        click(self.item("deposit").mark)
        self.assertEqual(self.lit(), DEPOSIT)
        self.assertFalse(
            self.todo_rows()["Send the figures"].has_css_class("item-active")
        )

    def test_rebuilding_the_list_keeps_the_lit_to_do_lit(self):
        """asst answers about once a minute and the list is drawn again."""
        click(self.target_of("Send the figures"))
        old = self.todo_rows()["Send the figures"]

        self.page._load_todos(
            self.page.current, ensure_todos(self.page.current.dir)
        )

        new = self.todo_rows()["Send the figures"]
        self.assertIsNot(new, old)
        self.assertTrue(new.has_css_class("item-active"))
        self.assertEqual(self.lit(), FIGURES)
        # Clicking the new row puts it out, as the old one would.
        click(self.target_of("Send the figures"))
        self.assertEqual(self.lit(), [])

    def test_a_lit_summary_item_is_not_touched_by_a_rebuilt_list(self):
        click(self.item("deposit").mark)

        self.page._load_todos(
            self.page.current, ensure_todos(self.page.current.dir)
        )

        self.assertEqual(self.lit(), DEPOSIT)
        self.assertTrue(self.item("deposit").mark.has_css_class("item-active"))

    def test_the_words_sit_where_they_would_in_the_box(self):
        """The same left edge and the same first line, cited or not."""

        def inset(text: str) -> tuple[float, float]:
            """Where the words start inside their own row."""
            label = self.words_of(text)
            ok, point = label.compute_point(
                self.todo_rows()[text], Graphene.Point().init(0, 0)
            )
            assert ok
            return point.x, point.y

        self.assertTrue(
            wait_for(
                lambda: self.words_of("Send the figures").get_height() > 0
            )
        )
        cited, plain = inset("Send the figures"), inset("Draft the table")
        self.assertAlmostEqual(cited[0], plain[0], delta=1.5)
        self.assertAlmostEqual(cited[1], plain[1], delta=1.5)


class FakeClipboard:
    def __init__(self) -> None:
        self.text = ""

    def set(self, text: str) -> None:
        self.text = text


class FakeFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def get_path(self) -> str:
        return str(self.path)


class FakeFileDialog:
    """Answers a save at once with a path chosen in advance."""

    chosen: Path

    def set_initial_name(self, _name: str) -> None:
        pass

    def save(self, _parent, _cancellable, callback) -> None:
        callback(self, None)

    def save_finish(self, _result) -> FakeFile:
        return FakeFile(self.chosen)


class TestLeavingTheApp(CitedMeeting):
    """Copy and Save write each citation as the time stamps the
    transcript section of the same page carries, never the marker."""

    def check(self, text: str) -> None:
        self.assertNotIn("<!--", text)
        self.assertIn("- Pay the deposit by Friday [10:00]\n", text)
        self.assertIn("- Ship in March [01:40, 13:20]\n", text)
        self.assertIn("- [ ] Send the figures [06:40]\n", text)
        self.assertIn("\n[10:00] ", text.partition("## Transcript")[2])

    def test_copy_puts_stamps_on_the_clipboard(self):
        clipboard = FakeClipboard()
        with mock.patch.object(
            self.page, "get_clipboard", return_value=clipboard
        ):
            self.page.actions.activate_action("copy", None)

        self.check(clipboard.text)
        self.assertEqual(self.toasts, ["Copied this meeting"])

    def test_save_writes_the_same_page_to_the_file_chosen(self):
        target = self.root / "saved.md"
        FakeFileDialog.chosen = target
        with mock.patch.object(Gtk, "FileDialog", FakeFileDialog):
            self.page.actions.activate_action("save", None)

        self.check(target.read_text())
        self.assertEqual(self.toasts, ["Saved to saved.md"])


class TestStylesheet(unittest.TestCase):
    def test_gtk_reads_every_rule_of_it(self):
        """A mistyped property is only a warning on stderr and the look
        silently missing."""
        errors: list[str] = []
        provider = Gtk.CssProvider()
        provider.connect(
            "parsing-error",
            lambda _p, section, error: errors.append(
                f"{section.to_string()}: {error.message}"
            ),
        )
        provider.load_from_string(stylesheet(FALLBACK))
        self.assertEqual(errors, [])


class TestWithoutCitations(CitedMeeting):
    HINT = "No citations. Summarize again to add them."

    def open_plain(self) -> None:
        self.window.sidebar.list.select_row(
            self.window.sidebar.list.get_row_at_index(1)
        )
        self.assertEqual(self.page.current.title, "Plain")

    def test_a_summary_that_cites_nothing_says_so_quietly_and_nothing_nags(
        self,
    ):
        self.open_plain()

        self.assertTrue(self.page.summary_hint.get_visible())
        self.assertEqual(self.page.summary_hint.get_text(), self.HINT)
        self.assertTrue(self.page.summary_hint.has_css_class("hint"))
        self.assertEqual(self.view.sources, [])
        self.assertEqual(self.toasts, [])

    def test_none_of_its_text_is_clickable_and_all_of_it_selectable(self):
        self.open_plain()
        for row in children(self.view):
            self.assertFalse(row.has_css_class("cited-item"))
            if row.has_css_class("summary-item"):
                self.assertEqual(controllers(row, Gtk.GestureClick), [])
                _glyph, text = children(row)
                self.assertTrue(text.get_selectable(), text.get_label())
            else:
                self.assertTrue(row.get_selectable(), row.get_label())

    def test_the_hint_goes_when_the_summary_has_citations(self):
        self.assertFalse(self.page.summary_hint.get_visible())
        self.open_plain()
        self.assertTrue(self.page.summary_hint.get_visible())
        self.window.sidebar.list.select_row(
            self.window.sidebar.list.get_row_at_index(0)
        )
        self.assertFalse(self.page.summary_hint.get_visible())

    def test_a_summary_with_nothing_to_cite_has_no_hint(self):
        folder = self.page.current.dir
        (folder / "summary.md").write_text("# T\n\nJust a sentence.\n")
        self.page.reload()
        self.assertFalse(self.page.summary_hint.get_visible())

    def test_a_meeting_with_no_summary_has_no_hint(self):
        (self.page.current.dir / "summary.md").unlink()
        self.page.reload()
        self.assertFalse(self.page.summary_hint.get_visible())
        self.assertFalse(self.view.get_visible())


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


class TestSidebarWidth(CitedMeeting):
    """The drag handle has room to move (issue #33).

    `min`-`max` of 260-300 left 40px of travel, so the sidebar read as
    fixed-width however hard it was dragged. The split is the window's,
    not the page's, so the numbers live in window.py and are only
    checked here.
    """

    def test_the_drag_handle_has_somewhere_to_go(self):
        split = self.window.split
        self.assertGreaterEqual(
            split.get_max_sidebar_width() - split.get_min_sidebar_width(),
            200,
        )

    def test_the_sidebar_can_hold_a_meeting_title_at_its_narrowest(self):
        self.assertGreaterEqual(self.window.split.get_min_sidebar_width(), 200)

    def test_clicking_a_to_dos_text_widens_nothing(self):
        """The complaint was that a click "expands the todo sidebar".

        What does grow is the cite bar (a places list docked above the
        player, by design). Neither the split sidebar nor the to-do
        column may move for it.
        """
        split = self.window.split
        side = find_css(self.page, "side-column")  # the to-do column
        self.assertIsNotNone(side)
        target = next(
            todo_parts(source.mark)[2]
            for source in self.page._todo_sources
            if "figures" in source.markup
        )
        before = (
            split.get_sidebar().get_child().get_width(),
            side.get_width(),
        )

        click(target)
        self.assertTrue(wait_for(self.page.cite_bar.get_visible))

        self.assertEqual(
            (
                split.get_sidebar().get_child().get_width(),
                side.get_width(),
            ),
            before,
        )
        # The bar is what changed, and it stays under its height cap.
        self.assertTrue(wait_for(lambda: self.page.cite_bar.get_height() > 0))
        self.assertLessEqual(self.page.cite_bar.scroller.get_height(), 120)


if __name__ == "__main__":
    unittest.main()
