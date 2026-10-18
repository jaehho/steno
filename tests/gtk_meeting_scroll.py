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

from steno.gui.meeting import MeetingPage
from steno.gui.window import Window
from tests.gtk_sidebar_menu import StubApp, settle

LINES = 200
HIT = 150
FOLLOWED = 120
NEEDLE = "zanzibar"
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


class TestTranscriptScroll(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

