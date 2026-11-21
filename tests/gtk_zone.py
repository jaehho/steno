"""GTK checks that the window draws its days again in a new zone (#44),
and that the app's startup step listens for one on the system bus.

Run by test_zone.py, in a child process on a Broadway display and never
on a real one. The signal comes over a private bus, as timedated sends
it on the system bus.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
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
TO_AFTER = {"Timezone": GLib.Variant("s", AFTER)}
SYSTEM_BUS_ENV = "DBUS_SYSTEM_BUS_ADDRESS"


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


def set_env(name: str, value: str | None) -> None:
    """Set `name` in the environment, or unset it for None."""
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


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
        # Loading the open meeting again would stop its playback.
        loads = mock.patch.object(
            self.window.meeting, "load", wraps=self.window.meeting.load
        )
        self.load = loads.start()
        self.addCleanup(loads.stop)

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

        announce(self.timedated, TO_AFTER)

        self.assertTrue(wait_until(lambda: row_text(self.row())[0] == "Today"))
        self.assertEqual(
            row_text(self.row()), ("Today", f"{self.started:%H:%M}")
        )
        # The open meeting stays open, not loaded again.
        self.assertEqual(self.window.sidebar.selected.dir, self.folder)
        self.load.assert_not_called()


class TestFollowZone(unittest.TestCase):
    """The app's startup step, `App._follow_zone`, on an app made and
    never started. A private bus stands in for the system bus: GIO
    finds the system bus at DBUS_SYSTEM_BUS_ADDRESS."""

    def setUp(self):
        self.addCleanup(set_zone, os.environ.get("TZ"))
        self.addCleanup(
            set_env, SYSTEM_BUS_ENV, os.environ.get(SYSTEM_BUS_ENV)
        )
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bus = PrivateBus(self.tmp)
        self.timedated = self.bus.connect()
        self.told = 0
        self.app = self.make_app(self.bus.address)

    def tearDown(self):
        # Closed from this side, which never raises SIGTERM: only a
        # bus going away under the connection could.
        for connection in (self.app._zone_bus, self.timedated):
            if connection is not None and not connection.is_closed():
                connection.close_sync(None)
        self.bus.stop()
        self._tmp.cleanup()

    def make_app(self, system_bus: str) -> App:
        """An app whose window only counts what it is told, after the
        startup step ran with `system_bus` as the system bus."""
        os.environ[SYSTEM_BUS_ENV] = system_bus
        app = App(root=self.tmp / "sessions")
        app._window = SimpleNamespace(zone_changed=self._told)
        app._follow_zone()
        return app

    def _told(self) -> None:
        self.told += 1

    def test_startup_listens_for_timedated_on_the_system_bus(self):
        set_zone(BEFORE)
        steno = self.app._zone_bus
        assert steno is not None, "no connection to the stand-in bus"
        reach(steno)
        # The zone changes on disk; the process has not looked yet.
        os.environ["TZ"] = AFTER

        announce(self.timedated, TO_AFTER)

        self.assertTrue(wait_until(lambda: self.told == 1))
        self.assertEqual(time.localtime().tm_gmtoff, -11 * 3600)

    def test_losing_the_system_bus_keeps_steno_running(self):
        steno = self.app._zone_bus
        assert steno is not None, "no connection to the stand-in bus"
        # True would raise SIGTERM in this process once the bus stops.
        self.assertFalse(steno.get_exit_on_close())
        self.bus.stop()
        self.assertTrue(wait_until(steno.is_closed))

    def test_without_a_system_bus_steno_says_so_and_goes_on(self):
        missing = f"unix:path={self.tmp / 'no-bus'}"
        with self.assertLogs("steno.gui.app", "WARNING") as said:
            app = self.make_app(missing)
        self.assertIsNone(app._zone_bus)
        self.assertIn("no system bus", said.output[0])


if __name__ == "__main__":
    unittest.main()
