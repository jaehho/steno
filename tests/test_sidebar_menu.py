"""The menu on a sidebar row: one model, used by the header and the rows.

The model is checked here. What a right-click does to a widget needs a
display, so tests/gtk_sidebar_menu.py runs it on a private Broadway one,
with no pointer and no screen; that file is skipped without
gtk4-broadwayd.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from gi.repository import Gio

from steno.gui.menu import meeting_menu_model, menu_actions

ROOT = Path(__file__).resolve().parent.parent
BROADWAY_DISPLAYS = range(41, 61)
# Whatever could lead GTK to a real screen, a bus or a settings store.
SCRUBBED = ("DISPLAY", "WAYLAND_DISPLAY", "GDK_BACKEND", "BROADWAY_DISPLAY")


class TestMenuModel(unittest.TestCase):
    def test_every_item_is_a_meeting_action(self):
        for name in menu_actions(meeting_menu_model()):
            self.assertTrue(name.startswith("meeting."), name)

    def test_no_action_is_offered_twice(self):
        names = menu_actions(meeting_menu_model())
        self.assertEqual(len(names), len(set(names)))

    def test_deleting_is_set_apart_at_the_end(self):
        menu = meeting_menu_model()
        last = menu.get_item_link(menu.get_n_items() - 1, Gio.MENU_LINK_SECTION)
        self.assertEqual(
            menu_actions(last), ["meeting.delete-audio", "meeting.delete"]
        )

    def test_a_menu_is_built_per_call(self):
        self.assertIsNot(meeting_menu_model(), meeting_menu_model())

    def test_it_offers_one_of_archive_and_unarchive(self):
        put_away = menu_actions(meeting_menu_model(archived=True))
        kept = menu_actions(meeting_menu_model())
        self.assertIn("meeting.unarchive", put_away)
        self.assertNotIn("meeting.archive", put_away)
        self.assertIn("meeting.archive", kept)
        self.assertNotIn("meeting.unarchive", kept)

    def test_archiving_is_not_next_to_the_deletes(self):
        for archived in (False, True):
            menu = meeting_menu_model(archived)
            last = menu.get_item_link(
                menu.get_n_items() - 1, Gio.MENU_LINK_SECTION
            )
            self.assertEqual(
                menu_actions(last), ["meeting.delete-audio", "meeting.delete"]
            )


def listening(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
    except OSError:
        return False
    return True


def start_broadway(runtime: str) -> tuple[subprocess.Popen | None, int]:
    """A Broadway server on the first display number that is free.

    The server serves HTTP on 8080 plus the display number, which is also
    how a busy number shows: the server exits instead of listening.
    """
    exe = shutil.which("gtk4-broadwayd")
    if exe is None:
        return None, 0
    env = {**os.environ, "XDG_RUNTIME_DIR": runtime}
    for number in BROADWAY_DISPLAYS:
        if listening(8080 + number):
            continue
        server = subprocess.Popen(
            [exe, f":{number}"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if listening(8080 + number) or server.poll() is not None:
                break
            time.sleep(0.1)
        if server.poll() is None and listening(8080 + number):
            return server, number
        server.kill()
        server.wait()
    return None, 0


class TestOnBroadway(unittest.TestCase):
    def test_right_click_menu_in_a_window_on_broadway(self):
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
                     "-m", "unittest", "-v", "tests.gtk_sidebar_menu"],
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
