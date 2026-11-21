"""steno follows a change of time zone without a restart (issue #44).

timedated says so on the system bus; here the signal is sent on a
private bus, and the change of zone is a change of `TZ`, which, like a
new /etc/localtime, a running process sees only after `time.tzset()`.
What the window draws again, and the app's startup step that listens on
the system bus, need a display, so tests/gtk_zone.py runs them on a
private Broadway one; that part is skipped without gtk4-broadwayd.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path

from gi.repository import Gio, GLib

from steno import zone
from steno.session import new_session_dir
from tests.test_sidebar_menu import ROOT, SCRUBBED, start_broadway

# 2026-01-15 10:00 UTC: 05:00 in New York, 10:00 in Lisbon (winter).
MOMENT = 1768471200
TO_LISBON = {"Timezone": GLib.Variant("s", "Europe/Lisbon")}
# A bus that starts nothing: its only service folder is empty.
BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:dir={folder}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
  <servicedir>{folder}/services</servicedir>
</busconfig>
"""


class PrivateBus:
    """A dbus-daemon of its own, in `folder`, standing in for the
    system bus; `connect()` gives a client of it."""

    def __init__(self, folder: Path) -> None:
        (folder / "services").mkdir()
        config = folder / "bus.conf"
        config.write_text(BUS_CONFIG.format(folder=folder))
        self.process = subprocess.Popen(
            ["dbus-daemon", f"--config-file={config}", "--nofork",
             "--print-address=1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        out = self.process.stdout
        assert out is not None  # stdout=PIPE
        # It prints the address once and nothing after.
        self.address = out.readline().strip()
        out.close()
        if not self.address:
            self.process.wait()
            msg = f"dbus-daemon did not start in {folder} (path too long?)"
            raise RuntimeError(msg)

    def connect(self) -> Gio.DBusConnection:
        return Gio.DBusConnection.new_for_address_sync(
            self.address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait()


def reach(bus: Gio.DBusConnection) -> None:
    """Wait for the bus to answer: what `bus` sent before, a watch's
    match rule among them, is in place once it has."""
    bus.call_sync(
        "org.freedesktop.DBus", "/org/freedesktop/DBus",
        "org.freedesktop.DBus", "GetId", None, None,
        Gio.DBusCallFlags.NONE, -1, None,
    )


def announce(
    bus: Gio.DBusConnection,
    changed: dict[str, GLib.Variant],
    interface: str = zone.TIMEDATE_INTERFACE,
) -> None:
    """Send what timedated sends after `SetTimezone`."""
    bus.emit_signal(
        None,
        zone.TIMEDATE_PATH,
        zone.PROPERTIES_INTERFACE,
        "PropertiesChanged",
        GLib.Variant("(sa{sv}as)", (interface, changed, [])),
    )
    bus.flush_sync(None)


def wait_until(done: Callable[[], bool], seconds: float = 5.0) -> bool:
    """Run the main loop until `done()` holds or `seconds` pass."""
    context = GLib.MainContext.default()
    give_up = time.monotonic() + seconds
    while not done() and time.monotonic() < give_up:
        if not context.iteration(False):
            time.sleep(0.01)
    return done()


def settle(seconds: float = 0.3) -> None:
    """Let anything already sent arrive."""
    wait_until(lambda: False, seconds)


def set_zone(name: str | None) -> None:
    """Set `TZ`, or unset it for None, and read it in."""
    if name is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = name
    time.tzset()


def clock_at(moment: float) -> str:
    return time.strftime("%H:%M %Z", time.localtime(moment))


class TestNamesZone(unittest.TestCase):
    def test_the_new_zone_in_the_changed_properties(self):
        self.assertTrue(zone.names_zone({"Timezone": "Europe/Lisbon"}, []))

    def test_the_zone_only_invalidated(self):
        self.assertTrue(zone.names_zone({}, ["Timezone"]))

    def test_another_property(self):
        self.assertFalse(zone.names_zone({"NTP": True}, []))
        self.assertFalse(zone.names_zone({}, ["LocalRTC"]))


@unittest.skipIf(shutil.which("dbus-daemon") is None, "no dbus-daemon")
class TestWatchZone(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        self.bus = PrivateBus(self.folder)
        self.listener = self.bus.connect()
        self.timedated = self.bus.connect()
        self.heard = 0
        self.watch = zone.watch_zone(self.listener, self._on_change)
        reach(self.listener)
        self.addCleanup(set_zone, os.environ.get("TZ"))
        set_zone("America/New_York")
        # The zone changes on disk; the process has not looked yet.
        os.environ["TZ"] = "Europe/Lisbon"

    def tearDown(self):
        self.listener.signal_unsubscribe(self.watch)
        self.listener.close_sync(None)
        self.timedated.close_sync(None)
        self.bus.stop()
        self._tmp.cleanup()

    def _on_change(self) -> None:
        self.heard += 1

    def test_unannounced_the_old_zone_stays(self):
        settle()
        self.assertEqual(clock_at(MOMENT), "05:00 EST")
        self.assertEqual(self.heard, 0)

    def test_the_announced_zone_is_read_and_said(self):
        announce(self.timedated, TO_LISBON)
        self.assertTrue(wait_until(lambda: self.heard == 1))
        self.assertEqual(clock_at(MOMENT), "10:00 WET")

    def test_a_new_meeting_folder_takes_the_new_zone(self):
        sessions = self.folder / "sessions"
        self.assertEqual(
            new_session_dir(sessions, MOMENT).name, "20260115-050000"
        )
        announce(self.timedated, TO_LISBON)
        self.assertTrue(wait_until(lambda: self.heard == 1))
        self.assertEqual(
            new_session_dir(sessions, MOMENT).name, "20260115-100000"
        )

    def test_other_properties_are_not_a_new_zone(self):
        announce(self.timedated, {"NTP": GLib.Variant("b", True)})
        announce(
            self.timedated, TO_LISBON, interface="org.freedesktop.locale1"
        )
        settle()
        self.assertEqual(self.heard, 0)
        self.assertEqual(clock_at(MOMENT), "05:00 EST")


@unittest.skipIf(shutil.which("dbus-daemon") is None, "no dbus-daemon")
class TestOnBroadway(unittest.TestCase):
    def test_the_window_redraws_its_days_on_broadway(self):
        with tempfile.TemporaryDirectory() as runtime:
            server, number = start_broadway(runtime)
            if server is None:
                self.skipTest("no gtk4-broadwayd to open windows on")
            env = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
            env.update(
                XDG_RUNTIME_DIR=runtime,
                XDG_DATA_HOME=runtime,
                GDK_BACKEND="broadway",
                BROADWAY_DISPLAY=f":{number}",
                GSETTINGS_BACKEND="memory",
                GTK_A11Y="none",
                NO_AT_BRIDGE="1",
            )
            try:
                done = subprocess.run(
                    [sys.executable, "-W", "ignore::DeprecationWarning",
                     "-m", "unittest", "-v", "tests.gtk_zone"],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            finally:
                server.terminate()
                server.wait()
        self.assertEqual(done.returncode, 0, done.stderr[-4000:])


if __name__ == "__main__":
    unittest.main()
