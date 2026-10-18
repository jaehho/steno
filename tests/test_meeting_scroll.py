"""Jumping to a search hit and following the audio, on a real window.

Both move a scroller by where a row sits in it, and a wrong reading of
that placement raises inside a signal handler or an idle callback, where
the headless suite never goes. tests/gtk_meeting_scroll.py runs them on
a private Broadway display, with no pointer and no screen, the way
test_sidebar_menu.py does; it is skipped without gtk4-broadwayd.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

from tests.test_sidebar_menu import ROOT, SCRUBBED, start_broadway


class TestOnBroadway(unittest.TestCase):
    def test_search_jump_and_playback_follow_in_a_window_on_broadway(self):
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
                     "-m", "unittest", "-v", "tests.gtk_meeting_scroll"],
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
