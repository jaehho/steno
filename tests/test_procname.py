"""The process goes by "Steno" on the desktop, from the first call on.

Run in a child process: GLib's names are set once per process, and
other tests may set them first.
"""
from __future__ import annotations

import subprocess
import sys
import unittest

SCRIPT = (
    "from gi.repository import GLib\n"
    "from steno.procname import name_process\n"
    "name_process()\n"
    "name_process()\n"
    "print(GLib.get_application_name(), GLib.get_prgname())\n"
)


class TestName(unittest.TestCase):
    def test_named_once_without_a_warning(self):
        done = subprocess.run(
            [sys.executable, "-c", SCRIPT],
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), ["Steno", "steno"])
        self.assertNotIn("multiple times", done.stderr)


if __name__ == "__main__":
    unittest.main()
