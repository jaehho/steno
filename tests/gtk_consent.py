"""GTK checks for the recording notice: its Keep and Don't record buttons.

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

from steno.events import MeetingDiscarded, MeetingKept, MeetingStarted
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


class TestRecordingNotice(unittest.TestCase):
    def setUp(self):
        self.calls: list[str] = []
        self.sent: dict[str, Note] = {}
        self.withdrawn: list[str] = []
        engine = SimpleNamespace(
            keep_now=lambda: self.calls.append("keep"),
            discard_now=lambda: self.calls.append("discard"),
            current=object(),
        )
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

    def started(self, app: str = APP, *, kept: bool = True) -> Note:
        self.app._on_event(
            MeetingStarted(Path("/nonexistent"), app, 0.0, app == "manual", kept)
        )
        return self.sent["recording"]

    def test_a_detected_call_offers_dont_record(self):
        note = self.started()
        self.assertEqual(note.title, "Recording")
        self.assertEqual(note.buttons, [
            ("Stop", "app.stop-record"),
            ("Don't record", "app.discard-record"),
        ])

    def test_a_recording_you_started_offers_only_stop(self):
        note = self.started("manual")
        self.assertEqual(note.buttons, [("Stop", "app.stop-record")])

    def test_a_call_not_kept_yet_says_it_will_be_deleted(self):
        note = self.started(kept=False)
        self.assertEqual(note.title, "Recording: keep it?")
        self.assertIn("deleted when the call ends", note.body)
        self.assertEqual(note.buttons, [
            ("Keep", "app.keep-record"),
            ("Don't record", "app.discard-record"),
        ])

    def test_keeping_turns_it_into_the_plain_notice(self):
        self.started(kept=False)
        self.app._on_event(MeetingKept(Path("/nonexistent"), APP))
        note = self.sent["recording"]
        self.assertEqual(note.title, "Recording")
        self.assertEqual(note.buttons[0], ("Stop", "app.stop-record"))

    def test_the_notice_goes_when_the_call_is_deleted(self):
        self.started()
        self.app._on_event(MeetingDiscarded(Path("/nonexistent"), APP))
        self.assertEqual(self.withdrawn, ["recording"])

    def test_the_buttons_reach_the_engine(self):
        for name in ("keep-record", "discard-record"):
            action = self.app.lookup_action(name)
            assert action is not None, f"no app.{name}"
            action.activate(None)
        self.assertEqual(self.calls, ["keep", "discard"])


if __name__ == "__main__":
    unittest.main()
