"""GTK checks for a debrief in the window: one voice and no far side.

A debrief has only `you.wav` and only lines labelled you. Run by
test_debrief_window.py, in a child process on a Broadway display and
never on a real one; the name keeps test discovery from importing this
file in the main process.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk

from steno import debrief
from steno.cost import prerecorded_cost, stream_cost
from steno.gui.meeting import MeetingPage
from steno.gui.sidebar import _session_cost
from steno.gui.window import Window
from steno.summarize import update_meta
from tests.gtk_meeting_cites import FakePlayer, silence
from tests.gtk_meeting_scroll import wait_for
from tests.gtk_sidebar_menu import StubApp, settle

LINES = 12
STEP_S = 5
STARTED = 1_700_000_000.0
TITLE = "Lab meeting debrief"
SUMMARY = f"""# {TITLE}

## TL;DR
- The talk was about turning. <!--cite 5-->

## Action items
**You**
- Rerun the query <!--cite 20-->

**Them**
- Sam sends the list by Friday <!--cite 10-->
"""


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


def make_debrief(root: Path) -> Path:
    """A transcribed, summarized debrief, as `steno debrief` leaves one."""
    session = debrief.create_session(root, STARTED)
    silence(session / debrief.TRACK)
    records = [
        {"speaker": "you", "text": f"line {n} of the debrief",
         "ts": STARTED + STEP_S * n + 4, "offset": float(STEP_S * n)}
        for n in range(LINES)
    ]
    (session / "transcript.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records))
    (session / "summary.md").write_text(SUMMARY)
    update_meta(session, mic_pending=None, asr="local:large-v3-turbo",
                title=TITLE)
    return session


class DebriefInTheWindow(unittest.TestCase):
    def setUp(self):
        # PyGObject reports an exception in a handler through
        # sys.excepthook and carries on; collect them instead.
        self.raised: list[BaseException] = []
        self._hook = sys.excepthook
        sys.excepthook = lambda _kind, value, _tb: self.raised.append(value)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.session = make_debrief(self.root)
        self.app = StubApp()
        self.app.register(None)
        patch = mock.patch("steno.gui.meeting.Player", FakePlayer)
        patch.start()
        self.addCleanup(patch.stop)
        self.window = Window(self.app, self.root)
        self.window.load()
        self.window.present()
        settle()
        self.page: MeetingPage = self.window.meeting

    def tearDown(self):
        sys.excepthook = self._hook
        self.window.destroy()
        settle()
        self._tmp.cleanup()

    def test_it_opens_on_its_lines_with_nothing_raised(self):
        self.assertEqual(self.page.current.title, TITLE)
        self.assertTrue(
            wait_for(lambda: len(self.page._rows) == LINES),
            "the transcript was never laid out")
        self.assertTrue(self.page.player_bar.get_visible())
        self.assertEqual(self.raised, [])

    def test_a_line_plays_your_track_alone(self):
        self.page._play_line(3)
        settle()
        self.assertEqual(set(self.page.player.tracks), {"you"})
        # From where the line starts, less the page's short lead-in.
        (at,) = self.page.player.played
        self.assertTrue(3 * STEP_S - 1 < at <= 3 * STEP_S, at)
        self.assertEqual(self.raised, [])

    def test_the_meter_counts_nothing_for_it_before_it_is_summarized(self):
        # `steno debrief --no-summary` stamps no cost_usd; the meter
        # derives one, and a debrief done here costs nothing.
        self.assertNotIn("cost_usd", json.loads(
            (self.session / "meta.json").read_text()))
        self.assertEqual(_session_cost(self.session, 10.0), 0.0)
        call = self.root / "20260101-090000"
        call.mkdir()
        (call / "meta.json").write_text("{}")
        self.assertAlmostEqual(_session_cost(call, 10.0),
                               stream_cost(10.0) + prerecorded_cost(10.0))

    def test_re_timing_it_says_it_stays_on_this_machine(self):
        shown: list[str] = []
        self.page._dialog = lambda _title, body, *_a, **_kw: shown.append(body)
        with mock.patch.dict(os.environ):
            os.environ.pop("STENO_ASR", None)
            self.page._confirm_realign()
        self.assertEqual(len(shown), 1)
        self.assertIn("on this machine, which costs nothing", shown[0])


if __name__ == "__main__":
    unittest.main()
