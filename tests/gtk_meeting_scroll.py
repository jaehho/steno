"""GTK checks for scrolling the transcript (issue #18).

Run by test_meeting_scroll.py, in a child process on a Broadway display
and never on a real one. The name keeps test discovery from importing
this file in the main process, where `import gi` would connect to the
desktop.

Where a row sits is read here with compute_point, not with the
translate_coordinates the page calls, so a wrong reading of that one
cannot make the check agree with the code.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")

from gi.repository import Gdk, GLib, Graphene, Gtk

from steno.gui.meeting import FIRST_LINES, LINES_PER_FRAME, MeetingPage, _line_text
from steno.gui.window import Window
from tests.gtk_sidebar_menu import StubApp, settle

LINES = 200
HIT = 150
FOLLOWED = 120
NEEDLE = "zanzibar"
FOUND = (20, 90, 180)
MEETINGS = {"20260110-090000": "Long", "20260111-090000": "Short"}


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


def line(n: int) -> str:
    text = f"line {n} of a long meeting, said at enough length to wrap"
    return NEEDLE if n == HIT else text


def make_meetings(root: Path) -> None:
    """A long meeting with the needle, and a newer one without it.

    The newer one opens first, so searching has to move to the other.
    """
    for stamp, title in MEETINGS.items():
        folder = root / stamp
        folder.mkdir(parents=True)
        count = LINES if title == "Long" else 3
        records = [
            {"speaker": "them", "text": line(n), "ts": 1000.0 + 5 * n}
            for n in range(count)
        ]
        (folder / "transcript.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records)
        )
        (folder / "meta.json").write_text(json.dumps({"title": title}))


def wait_for(done, seconds: float = 5.0) -> bool:
    """Run the main loop until `done()`; False if it never does."""
    end = time.monotonic() + seconds
    while not done():
        if time.monotonic() > end:
            return False
        settle()
        time.sleep(0.01)
    return True


def scroller_of(row: Gtk.ListBoxRow) -> Gtk.ScrolledWindow:
    return row.get_ancestor(Gtk.ScrolledWindow)


def content_of(row: Gtk.ListBoxRow) -> Gtk.Widget:
    """What the row's scroller scrolls: the child of its viewport."""
    return scroller_of(row).get_child().get_child()


def y_in_content(row: Gtk.ListBoxRow) -> float:
    ok, point = row.compute_point(content_of(row), Graphene.Point().init(0, 0))
    assert ok, "the row and the content it is in share no ancestor"
    return point.y


def on_screen(row: Gtk.ListBoxRow) -> bool:
    """The scroller is set to show the whole row.

    Read from the adjustment, which moves at once, and not from where
    the row is drawn, which waits for the next frame.
    """
    top = y_in_content(row)
    adjustment = scroller_of(row).get_vadjustment()
    first = adjustment.get_value()
    last = first + adjustment.get_page_size()
    return first <= top and top + row.get_height() <= last


class OnTheShortMeeting(unittest.TestCase):
    """A window open on the newer, short meeting."""

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
        self.assertEqual(self.page.current.title, "Short")

    def tearDown(self):
        sys.excepthook = self._hook
        self.window.destroy()
        settle()
        self._tmp.cleanup()
        self.assertEqual(self.raised, [])


class TestTranscriptScroll(OnTheShortMeeting):
    def open_long_meeting(self) -> Gtk.ScrolledWindow:
        """Search, then pick the one meeting that holds the needle."""
        sidebar = self.window.sidebar
        sidebar.search.set_text(NEEDLE)
        self.assertTrue(
            wait_for(lambda: [i.title for i in sidebar.items] == ["Long"]),
            "the search did not narrow the list to the meeting it hits",
        )
        sidebar.list.select_row(sidebar.list.get_row_at_index(0))
        self.assertEqual(self.page.current.title, "Long")
        scroller = scroller_of(self.page.transcript_list)
        self.assertTrue(
            wait_for(lambda: self.page._rows[HIT].get_height() > 0),
            "the transcript was never laid out",
        )
        return scroller

    def test_the_searched_word_is_lit_and_cleared_with_the_search(self):
        self.open_long_meeting()
        label = _line_text(self.page._rows[HIT])
        lit = label.get_attributes().to_string()
        self.assertIn(f"0 {len(NEEDLE)} background", lit)
        self.assertIsNone(_line_text(self.page._rows[0]).get_attributes())
        self.window.sidebar.search.set_text("")
        self.assertTrue(wait_for(lambda: label.get_attributes() is None))
        self.assertEqual(self.page._hits, [])

    def test_the_searched_word_is_lit_in_the_summary(self):
        (self.root / "20260110-090000" / "summary.md").write_text(
            f"# Long\n\n## Decisions\n\n- We picked {NEEDLE} today\n"
        )
        self.open_long_meeting()
        lit = [
            label for label in self.page.summary_view.labels
            if label.get_attributes() is not None
        ]
        self.assertEqual(len(lit), 1)
        self.assertIn(NEEDLE, lit[0].get_text())

    def test_the_search_is_lit_in_the_title_to_dos_and_notes(self):
        folder = self.root / "20260110-090000"
        (folder / "todos.json").write_text(json.dumps(
            [{"text": f"check {NEEDLE} first", "owner": "you"}]
        ))
        (folder / "notes.md").write_text(f"ask about {NEEDLE}\n")
        self.window.sidebar.refresh()
        self.open_long_meeting()
        self.window.page_title.set_text(f"Long {NEEDLE}")
        self.window._mark_title()
        notes = self.page.notes
        buf = notes.view.get_buffer()
        start = buf.get_iter_at_offset(len("ask about "))
        self.assertTrue(start.has_tag(notes._hit_tag))
        self.assertFalse(buf.get_start_iter().has_tag(notes._hit_tag))
        self.assertFalse(notes._dirty)
        self.assertIsNotNone(self.page._todo_labels[0].get_attributes())
        self.assertIsNotNone(self.window.page_title.get_attributes())

        self.window.sidebar.search.set_text("")
        self.assertTrue(
            wait_for(lambda: self.page._todo_labels[0].get_attributes() is None)
        )
        self.assertFalse(start.has_tag(notes._hit_tag))
        self.assertIsNone(self.window.page_title.get_attributes())
        self.assertFalse(notes._dirty)

    def test_n_steps_through_the_matches_and_round_again(self):
        folder = self.root / "20260110-090000"
        records = [
            {"speaker": "them", "text": f"{NEEDLE} at {n}" if n in FOUND else f"line {n}",
             "ts": 1000.0 + 5 * n}
            for n in range(LINES)
        ]
        (folder / "transcript.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records)
        )
        self.window.sidebar.refresh()
        sidebar = self.window.sidebar
        sidebar.search.set_text(NEEDLE)
        self.assertTrue(
            wait_for(lambda: [i.title for i in sidebar.items] == ["Long"])
        )
        sidebar.list.select_row(sidebar.list.get_row_at_index(0))
        page = self.page
        self.assertEqual(page._hits, list(FOUND))
        self.assertEqual(page.match_note(), "match 1 of 3 (n, N)")
        current = page._rows[FOUND[0]]
        self.assertTrue(current.has_css_class("line-hit-current"))

        self.window._step_match(1)
        self.assertEqual(page.match_note(), "match 2 of 3 (n, N)")
        self.assertIn("match 2 of 3", self.window.page_subtitle.get_text())
        self.assertFalse(current.has_css_class("line-hit-current"))
        following = page._rows[FOUND[1]]
        self.assertTrue(following.has_css_class("line-hit-current"))
        self.assertTrue(wait_for(lambda: on_screen(following)))

        self.window._step_match(-1)
        self.window._step_match(-1)
        self.assertEqual(page.match_note(), "match 3 of 3 (n, N)")
        self.assertTrue(wait_for(lambda: on_screen(page._rows[FOUND[2]])))

        self.window.sidebar.search.set_text("")
        self.assertTrue(wait_for(lambda: page.match_note() == ""))
        self.assertNotIn("match", self.window.page_subtitle.get_text())
        self.assertFalse(page._rows[FOUND[2]].has_css_class("line-hit-current"))
        self.assertFalse(page.step_hit(1))

    def press(self, key: int, state: Gdk.ModifierType | None = None) -> bool:
        settle()
        return self.window._on_key(
            None, key, 0, state or Gdk.ModifierType(0)
        )

    def test_the_vim_keys_are_only_keys_while_you_are_typing(self):
        self.open_long_meeting()
        page, sidebar = self.page, self.window.sidebar
        self.window.set_focus(sidebar.search)
        self.assertFalse(self.press(Gdk.KEY_n))
        self.assertFalse(self.press(Gdk.KEY_slash))
        self.window.set_focus(page.notes.view)
        self.assertFalse(self.press(Gdk.KEY_n))

        page.focus_reading()
        self.assertTrue(self.press(Gdk.KEY_n))
        self.assertTrue(self.press(Gdk.KEY_N))
        self.assertFalse(self.press(Gdk.KEY_n, Gdk.ModifierType.CONTROL_MASK))

    def test_n_is_an_ordinary_key_with_no_search(self):
        self.window.set_focus(self.page.transcript_list)
        self.assertFalse(self.press(Gdk.KEY_n))

    def test_slash_goes_to_the_search_box(self):
        self.page.focus_reading()
        self.assertTrue(self.press(Gdk.KEY_slash))
        focus = self.window.get_focus()
        self.assertTrue(focus.is_ancestor(self.window.sidebar.search))

    def test_esc_on_the_page_clears_the_search(self):
        self.open_long_meeting()
        self.page.focus_reading()
        escape = self.window._on_escape
        self.assertTrue(escape(None, Gdk.KEY_Escape, 0, 0))
        self.assertEqual(self.window.sidebar.search.get_text(), "")
        self.assertFalse(escape(None, Gdk.KEY_Escape, 0, 0))

    def test_esc_stays_with_the_notes_while_you_type_in_them(self):
        self.open_long_meeting()
        self.window.set_focus(self.page.notes.view)
        settle()
        self.assertFalse(self.window._on_escape(None, Gdk.KEY_Escape, 0, 0))
        self.assertNotEqual(self.window.sidebar.search.get_text(), "")

    def test_a_search_hit_is_scrolled_to(self):
        scroller = self.open_long_meeting()
        hit = self.page._rows[HIT]
        self.assertTrue(hit.has_css_class("line-hit"))

        # The jump the page made when the meeting was opened from the
        # search, not a second one. Broadway lays the rows out after
        # the first idle pass, so a jump that asked once would have
        # moved a scroller with no extent yet and stayed at the top.
        self.assertTrue(
            wait_for(lambda: scroller.get_vadjustment().get_value() > 0),
            "the transcript stayed at the top",
        )
        self.assertTrue(on_screen(hit))

    def test_a_jump_to_a_line_of_a_meeting_left_stops_asking(self):
        short = self.page.current
        self.open_long_meeting()
        row = self.page._rows[HIT]
        soon = GLib.get_monotonic_time() + 10 * GLib.USEC_PER_SEC

        self.page.load(short)

        self.assertFalse(self.page._scroll_to_row(row, soon))

    def test_the_view_follows_the_audio_down_the_transcript(self):
        scroller = self.open_long_meeting()
        rows = self.page._rows
        scroller.get_vadjustment().set_value(0)
        self.assertFalse(on_screen(rows[FOLLOWED]))

        # What the player reports a few times a second.
        self.page._on_tick(self.page._offsets[FOLLOWED], 1000.0, True)

        self.assertTrue(rows[FOLLOWED].has_css_class("line-playing"))
        self.assertTrue(on_screen(rows[FOLLOWED]))

    def test_a_reader_who_scrolled_away_is_left_where_they_are(self):
        scroller = self.open_long_meeting()
        rows = self.page._rows
        adjustment = scroller.get_vadjustment()
        adjustment.set_value(0)
        self.page._on_tick(self.page._offsets[1], 1000.0, True)
        adjustment.set_value(adjustment.get_upper())
        self.assertFalse(on_screen(rows[1]))
        before = adjustment.get_value()

        self.page._on_tick(self.page._offsets[2], 1000.0, True)

        self.assertTrue(rows[2].has_css_class("line-playing"))
        self.assertEqual(adjustment.get_value(), before)

    def test_clicking_a_line_plays_from_it_and_marks_it(self):
        scroller = self.open_long_meeting()
        row = self.page._rows[HIT]
        scroller.get_vadjustment().set_value(y_in_content(row))

        self.page.transcript_list.emit("row-activated", row)

        self.assertTrue(row.has_css_class("line-playing"))
        self.assertEqual(self.page._playing_index, HIT)

    def test_a_row_is_found_at_its_place_in_the_scrolled_content(self):
        scroller = self.open_long_meeting()
        row = self.page._rows[FOLLOWED]

        frame = MeetingPage._row_frame(row)

        self.assertIsNotNone(frame)
        found_scroller, top, height = frame
        self.assertIs(found_scroller, scroller)
        self.assertEqual(height, row.get_height())
        self.assertAlmostEqual(top, y_in_content(row), delta=1.0)


class TestALongTranscriptComesInByFrames(OnTheShortMeeting):
    """A screenful at once, the rest a few lines a frame, and a line
    anything takes the page to goes in first."""

    def open_long(self) -> Gtk.ListBox:
        long = next(s for s in self.window.sidebar.sessions if s.title == "Long")
        self.page.load(long)
        return self.page.transcript_list

    def test_a_screenful_now_and_the_rest_by_frames(self):
        # Whether or not the window's own fade-in has ended by now.
        self.page.fill_held = False
        lines = self.open_long()
        self.assertEqual(rows_in(lines), FIRST_LINES)
        self.page._fill(lines, None)
        self.assertEqual(rows_in(lines), FIRST_LINES + LINES_PER_FRAME)

    def test_held_while_the_window_fades_in(self):
        self.page.fill_held = True
        lines = self.open_long()
        self.page._fill(lines, None)
        self.assertEqual(rows_in(lines), FIRST_LINES)

    def test_the_line_playing_goes_in_and_on_screen(self):
        self.open_long()
        row = self.page._rows[HIT]
        self.assertIsNone(row.get_parent())

        self.page._on_tick(self.page._offsets[HIT], 1000.0, True)

        self.assertIs(row.get_parent(), self.page.transcript_list)
        self.assertTrue(
            wait_for(lambda: row.get_height() > 0 and on_screen(row)),
            "the line playing was never brought into view",
        )


def rows_in(lines: Gtk.ListBox) -> int:
    count, row = 0, lines.get_first_child()
    while row is not None:
        count, row = count + 1, row.get_next_sibling()
    return count


if __name__ == "__main__":
    unittest.main()

