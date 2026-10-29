"""GTK checks for app.open-session, which other apps call by name.

asst's Open meeting sends a meeting's folder name, as the app contract
says; steno's own toasts send its path. Run by test_open_session.py, in
a child process on a Broadway display and never on a real one.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk

from steno.gui.app import App
from steno.gui.window import Window
from tests.gtk_sidebar_menu import StubApp, make_meetings, settle


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


class TestOpenSession(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make_meetings(self.root)
        self.app = StubApp()
        self.app.register(None)
        self.window = Window(self.app, self.root)
        self.window.present()
        settle()
        # What App.open_session reads of the app it runs on.
        self.app.root = self.root
        self.app._window = self.window
        self.app.present_window = lambda: self.window

    def tearDown(self):
        self.window.destroy()
        settle()
        self._tmp.cleanup()

    def open(self, name: str) -> None:
        App.open_session(self.app, name)
        settle()

    def shown(self) -> str:
        return self.window.sidebar.selected.title

    def test_a_folder_name_opens_that_meeting(self):
        self.assertEqual(self.shown(), "Charlie")
        self.open("20260110-090000")
        self.assertEqual(self.shown(), "Alpha")
        self.assertEqual(self.window.meeting.current.title, "Alpha")

    def test_a_path_still_opens_it(self):
        self.open(str(self.root / "20260111-090000"))
        self.assertEqual(self.shown(), "Bravo")

    def test_an_unknown_name_says_so_and_keeps_the_open_meeting(self):
        with mock.patch.object(self.window, "_toast") as toast:
            self.open("20250101-000000")
        toast.assert_called_once_with("No meeting 20250101-000000")
        self.assertEqual(self.shown(), "Charlie")

    def test_no_name_only_brings_the_window_up(self):
        self.open("20260110-090000")
        with mock.patch.object(self.window, "_toast") as toast:
            self.open("")
        toast.assert_not_called()
        self.assertEqual(self.shown(), "Alpha")


if __name__ == "__main__":
    unittest.main()
