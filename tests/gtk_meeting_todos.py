"""GTK check that opening a meeting files nothing into asst (issue #19).

Run by test_meeting_todos.py, in a child process on a Broadway display
and never on a real one. The name keeps test discovery from importing
this file in the main process, where `import gi` would connect to the
desktop.

asst is stood in for at `asstlink._run`, the one place every call to it
goes through, so a filing by any route shows up as an `add`.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")

from gi.repository import Gdk

from steno import asstlink
from steno.gui.meeting import MeetingPage
from steno.gui.window import Window
from steno.notes import load_todos
from tests.gtk_meeting_scroll import wait_for
from tests.gtk_sidebar_menu import StubApp, settle

SUMMARY = """# Old Call

## Action items
**You**
- Send the usage figures by Friday
- Draft the comparison table

**Them**
- Revised quote next week
"""
MEETINGS = ["20260801-090000", "20260802-090000"]


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


def make_meetings(root: Path) -> None:
    """Meetings from before the to-do list: a summary and no todos.json."""
    for stamp in MEETINGS:
        folder = root / stamp
        folder.mkdir(parents=True)
        line = {"speaker": "them", "text": "hello", "ts": 1.0}
        (folder / "transcript.jsonl").write_text(json.dumps(line) + "\n")
        (folder / "meta.json").write_text(json.dumps({"title": stamp}))
        (folder / "summary.md").write_text(SUMMARY)


class TestOpeningFilesNothing(unittest.TestCase):
    def setUp(self):
        self.raised: list[BaseException] = []
        self._hook = sys.excepthook
        sys.excepthook = lambda _kind, value, _tb: self.raised.append(value)

        # Pulls run on a thread and append from there; list.append is atomic.
        self.calls: list[list[str]] = []

        def fake_asst(args: list[str]) -> str:
            self.calls.append(args)
            return '{"id": "T1"}' if args[0] == "add" else "[]"

        for patch in (
            mock.patch.object(asstlink, "available", lambda: True),
            mock.patch.object(asstlink, "_run", side_effect=fake_asst),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make_meetings(self.root)
        self.app = StubApp()
        self.app.register(None)
        self.window = Window(self.app, self.root)
        self.window.present()
        settle()
        self.page: MeetingPage = self.window.meeting

    def tearDown(self):
        sys.excepthook = self._hook
        self.window.destroy()
        settle()
        self._tmp.cleanup()
        self.assertEqual(self.raised, [])

    def pulled(self, count: int) -> bool:
        """The page has asked asst `count` times and heard the answer."""
        asked = sum(1 for args in self.calls if args == ["ls"])
        return asked >= count and not self.page._pull_in_flight

    def filed(self) -> list[list[str]]:
        return [args for args in self.calls if args[0] == "add"]

    def test_opening_meetings_files_none_of_their_items(self):
        self.assertTrue(
            wait_for(lambda: self.pulled(1)), "the page never asked asst"
        )
        other = self.window.sidebar.list.get_row_at_index(1)
        self.window.sidebar.list.select_row(other)
        self.assertTrue(
            wait_for(lambda: self.pulled(2)), "the page never asked asst"
        )

        self.assertEqual(self.filed(), [])
        for stamp in MEETINGS:
            rows = load_todos(self.root / stamp)
            self.assertEqual(len(rows), 3, stamp)
            self.assertEqual([r.uid for r in rows], [""] * 3, stamp)


if __name__ == "__main__":
    unittest.main()
