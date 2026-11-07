"""GTK checks for the call offer: its notification and its Record button.

Run by test_consent.py, in a child process on a Broadway display and
never on a real one. The name keeps test discovery from importing this
file in the main process, where `import gi` would connect to the desktop.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk

from steno.events import CallDetected, CallGone, MeetingStarted
from steno.gui import app as app_module

APP = "ZOOM VoiceEngine"


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


class Note:
    """Stands in for Gio.Notification, which cannot be read back."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.body = ""
        self.buttons: list[tuple[str, str]] = []

    @classmethod
    def new(cls, title: str) -> Note:
        return cls(title)

    def set_body(self, body: str) -> None:
        self.body = body

    def add_button(self, label: str, action: str) -> None:
        self.buttons.append((label, action))

    def set_default_action(self, *_a) -> None:
        pass

    def set_priority(self, *_a) -> None:
        pass


class TestCallOffer(unittest.TestCase):
    def setUp(self):
        self.accepted = 0
        self.sent: dict[str, Note] = {}
        self.withdrawn: list[str] = []
        engine = SimpleNamespace(record_call=self.accept, current=None)
        app = app_module.App()
        for patcher in (
            mock.patch.object(app_module.App, "engine", engine),
            mock.patch.object(app_module.Gio, "Notification", Note),
            mock.patch.object(app, "send_notification", self.sent.__setitem__),
            mock.patch.object(app, "withdraw_notification", self.withdrawn.append),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        app._install_actions()
        self.app = app

    def accept(self) -> None:
        self.accepted += 1

    def test_a_detected_call_asks_and_says_nothing_is_recorded(self):
        self.app._on_event(CallDetected(APP))
        note = self.sent["call"]
        self.assertEqual(note.title, "Call detected: record?")
        self.assertIn("Nothing is being recorded", note.body)
        self.assertEqual(note.buttons, [("Record", "app.record-call")])
        self.assertNotIn("recording", self.sent)

    def test_record_asks_the_engine_to_record_the_call(self):
        action = self.app.lookup_action("record-call")
        assert action is not None, "no app.record-call"
        action.activate(None)
        self.assertEqual(self.accepted, 1)

    def test_the_offer_goes_when_the_call_does(self):
        self.app._on_event(CallGone(APP))
        self.assertEqual(self.withdrawn, ["call"])

    def test_the_offer_gives_way_to_the_recording_notice(self):
        self.app._on_event(CallDetected(APP))
        self.app._on_event(MeetingStarted(Path("/nonexistent"), APP, 0.0))
        self.assertEqual(self.withdrawn, ["call"])
        self.assertEqual(self.sent["recording"].buttons, [("Stop", "app.stop-record")])


if __name__ == "__main__":
    unittest.main()
