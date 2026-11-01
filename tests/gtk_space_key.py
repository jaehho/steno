"""GTK checks for Space and the audio.

Run by test_space_key.py, in a child process on a Broadway display and
never on a real one. The name keeps test discovery from importing this
file in the main process, where `import gi` would connect to the
desktop.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk

from tests.gtk_meeting_scroll import OnTheShortMeeting
from tests.gtk_sidebar_menu import settle

NO_MODIFIERS = Gdk.ModifierType(0)


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


class TestSpace(OnTheShortMeeting):
    def setUp(self):
        super().setUp()
        self.toggles = mock.Mock()
        self.page.toggle_play = self.toggles

    def press(self, key: int = Gdk.KEY_space, state=NO_MODIFIERS) -> bool:
        settle()
        return self.window._on_space(None, key, 0, state)

    def start_audio(self) -> None:
        self.page.player.tracks = ["both.opus"]
        self.addCleanup(setattr, self.page.player, "tracks", [])

    def test_space_pauses_audio_that_has_been_started(self):
        self.start_audio()
        self.window.set_focus(self.page.transcript_list)
        self.assertTrue(self.press())
        self.toggles.assert_called_once()

    def test_a_stray_space_with_no_audio_stays_an_ordinary_key(self):
        self.window.set_focus(self.page.transcript_list)
        self.assertFalse(self.press())
        self.toggles.assert_not_called()

    def test_space_types_in_the_search_box(self):
        self.start_audio()
        self.window.set_focus(self.window.sidebar.search)
        self.assertFalse(self.press())
        self.toggles.assert_not_called()

    def test_space_types_in_the_notes(self):
        self.start_audio()
        text = self.page.notes.view
        self.assertIsInstance(text, Gtk.TextView)
        self.window.set_focus(text)
        self.assertFalse(self.press())

    def test_space_with_ctrl_is_left_to_the_shortcut(self):
        self.start_audio()
        self.assertFalse(
            self.press(state=Gdk.ModifierType.CONTROL_MASK)
        )
        self.toggles.assert_not_called()

    def test_space_is_left_to_a_dialog(self):
        self.start_audio()
        dialog = Adw.AlertDialog(heading="Sure?")
        dialog.add_response("ok", "OK")
        dialog.present(self.window)
        self.addCleanup(dialog.force_close)
        self.assertFalse(self.press())
        self.toggles.assert_not_called()

    def test_space_is_left_to_a_menu(self):
        self.start_audio()
        popover = Gtk.Popover()
        button = Gtk.Button(label="in a menu")
        popover.set_child(button)
        popover.set_parent(self.page.player_bar)
        self.addCleanup(popover.unparent)
        popover.popup()
        settle()
        button.grab_focus()
        self.assertFalse(self.press())

    def test_other_keys_are_left_alone(self):
        self.start_audio()
        self.assertFalse(self.press(key=Gdk.KEY_a))


if __name__ == "__main__":
    unittest.main()
