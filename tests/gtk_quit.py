"""GTK checks for Ctrl+Q and the main menu's Quit (issue #24).

Run by test_quit.py, in a child process on a Broadway display and
never on a real one. The name keeps test discovery from importing this
file in the main process, where `import gi` would connect to the desktop.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from steno.gui.menu import menu_actions
from steno.gui.window import Window


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


def settle() -> None:
    context = GLib.MainContext.default()
    while context.iteration(False):
        pass


def find(widget: Gtk.Widget, match) -> Gtk.Widget | None:
    """The first widget under `widget`, itself included, that `match` takes."""
    if match(widget):
        return widget
    child = widget.get_first_child()
    while child is not None:
        if (found := find(child, match)) is not None:
            return found
        child = child.get_next_sibling()
    return None


def press(dialog: Adw.AlertDialog, label: str) -> None:
    button = find(
        dialog, lambda w: isinstance(w, Gtk.Button) and w.get_label() == label
    )
    assert button is not None, f"no {label!r} button"
    button.emit("clicked")
    settle()


class RecordingStartedError(AssertionError):
    """A quit test must never start a recording."""


class StubApp(Adw.Application):
    """Enough of steno's App to build the window, and to see a quit."""

    def __init__(self) -> None:
        super().__init__(
            application_id="dev.jaeho.StenoQuitTest",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.status = "idle"
        self.detail = ""
        self.since = 0.0
        self.engine = None
        self.bridge = None
        self.republisher = None
        self.quits = 0

    def quit(self) -> None:
        self.quits += 1

    def toggle_record(self) -> None:
        raise RecordingStartedError

    stop_record = pause = toggle_record


class TestQuit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.app = StubApp()
        self.app.register(None)
        self.window = Window(self.app, Path(self._tmp.name))
        self.window.present()
        settle()

    def tearDown(self):
        self.window.destroy()
        settle()
        self._tmp.cleanup()

    def ask(self) -> Adw.AlertDialog:
        self.assertTrue(self.window.activate_action("win.quit", None))
        settle()
        dialog = self.window.get_visible_dialog()
        self.assertIsInstance(dialog, Adw.AlertDialog)
        self.assertEqual(self.app.quits, 0, "quit before it was confirmed")
        return dialog

    def test_ctrl_q_is_the_asking_quit_not_the_app_one(self):
        self.assertEqual(
            self.app.get_accels_for_action("win.quit"), ["<Control>q"]
        )
        self.assertEqual(self.app.get_accels_for_action("app.quit"), [])

    def test_idle_it_asks_and_keeping_listening_quits_nothing(self):
        dialog = self.ask()
        self.assertEqual(dialog.get_heading(), "Stop listening?")
        press(dialog, "Keep listening")
        self.assertEqual(self.app.quits, 0)

    def test_idle_it_quits_once_confirmed(self):
        press(self.ask(), "Stop listening")
        self.assertEqual(self.app.quits, 1)

    def test_mid_meeting_it_still_says_the_recording_ends(self):
        self.app.engine = SimpleNamespace(current=object())
        dialog = self.ask()
        self.assertEqual(dialog.get_heading(), "A meeting is still recording")
        press(dialog, "Stop and quit")
        self.assertEqual(self.app.quits, 1)

    def test_the_menu_sets_quit_apart_and_it_asks(self):
        button = find(
            self.window,
            lambda w: isinstance(w, Gtk.MenuButton)
            and w.get_tooltip_text() == "Main menu",
        )
        menu = button.get_menu_model()
        last = menu.get_item_link(menu.get_n_items() - 1, Gio.MENU_LINK_SECTION)
        self.assertEqual(menu_actions(last), ["win.quit"])
        self.assertNotIn("app.quit", menu_actions(menu))


if __name__ == "__main__":
    unittest.main()
