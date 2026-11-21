"""GTK checks that the window draws its days again in a new zone (#44).

Run by test_zone.py, in a child process on a Broadway display and never
on a real one. The signal comes over a private bus, as timedated sends
it on the system bus.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk

from steno import zone
from steno.gui.app import App
from steno.gui.sidebar import META_SEP
from steno.gui.window import Window
from tests.gtk_sidebar_menu import StubApp, labels_in, settle
from tests.test_zone import (
    PrivateBus,
    announce,
    reach,
    set_zone,
    wait_until,
)

# 25 hours apart: a meeting that has just started in Pago Pago was
# yesterday in Kiritimati, at any hour.
BEFORE = "Pacific/Kiritimati"  # UTC+14
AFTER = "Pacific/Pago_Pago"  # UTC-11


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


def make_meeting(root: Path, started: datetime) -> Path:
    folder = root / f"{started:%Y%m%d-%H%M%S}"
    folder.mkdir(parents=True)
    line = {"speaker": "them", "text": "hello", "ts": 1.0}
    (folder / "transcript.jsonl").write_text(json.dumps(line) + "\n")
    (folder / "meta.json").write_text(json.dumps({"title": "Standup"}))
    return folder


def row_text(row: Gtk.ListBoxRow) -> tuple[str, str]:
    """The heading over a row, and when its meeting was, as drawn."""
    header = row.get_header()
    heading = header.get_text() if isinstance(header, Gtk.Label) else ""
    _title, meta, *_ = labels_in(row)
    return heading, meta.split(META_SEP)[0]


class TestZoneChange(unittest.TestCase):
    def setUp(self):
        self.addCleanup(set_zone, os.environ.get("TZ"))
        set_zone(BEFORE)
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.started = datetime.now(ZoneInfo(AFTER)).replace(
            tzinfo=None, microsecond=0
        )
        self.folder = make_meeting(tmp / "sessions", self.started)
        (tmp / "bus").mkdir()
        self.bus = PrivateBus(tmp / "bus")
        self.listener = self.bus.connect()
        self.timedated = self.bus.connect()
        self.app = StubApp()
        self.app.register(None)
        self.window = Window(self.app, tmp / "sessions")
        self.window.present()
        settle()
        # What App.zone_changed reads of the app it runs on.
        self.app._window = self.window
        self.watch = zone.watch_zone(
            self.listener, lambda: App.zone_changed(self.app)
        )
        reach(self.listener)

    def tearDown(self):
        self.listener.signal_unsubscribe(self.watch)
        self.window.destroy()
        settle()
        self.listener.close_sync(None)
        self.timedated.close_sync(None)
        self.bus.stop()
        self._tmp.cleanup()

    def row(self) -> Gtk.ListBoxRow:
        row = self.window.sidebar.list.get_row_at_index(0)
        assert row is not None
        return row

    def test_the_list_draws_its_days_again_in_the_new_zone(self):
        self.assertEqual(
            row_text(self.row()), ("Last 7 days", f"{self.started:%a %H:%M}")
        )
        # The zone changes on disk; unannounced, the list stays.
        os.environ["TZ"] = AFTER
        wait_until(lambda: False, 0.3)
        self.assertEqual(row_text(self.row())[0], "Last 7 days")

        announce(self.timedated, {"Timezone": GLib.Variant("s", AFTER)})

        self.assertTrue(wait_until(lambda: row_text(self.row())[0] == "Today"))
        self.assertEqual(
            row_text(self.row()), ("Today", f"{self.started:%H:%M}")
        )
        # The open meeting stays open, not loaded again.
        self.assertEqual(self.window.sidebar.selected.dir, self.folder)


if __name__ == "__main__":
    unittest.main()
