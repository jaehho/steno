"""Starting at login is per-user, opt-in, and honest about who starts it."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steno import autostart


class TestAutostart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        env.start()
        self.addCleanup(env.stop)
        self.system = self.config / "etc-autostart" / autostart.FILENAME
        patched = mock.patch.object(autostart, "SYSTEM_ENTRY", self.system)
        patched.start()
        self.addCleanup(patched.stop)

    def test_off_by_default(self):
        self.assertFalse(autostart.is_enabled())

    def test_enable_writes_a_background_entry(self):
        autostart.enable()
        text = autostart.user_entry().read_text()
        self.assertIn("gui --background", text)
        self.assertNotIn("Hidden", text)
        self.assertTrue(autostart.is_enabled())

    def test_disable_removes_the_entry(self):
        autostart.enable()
        autostart.disable()
        self.assertFalse(autostart.user_entry().exists())
        self.assertFalse(autostart.is_enabled())

    def test_disable_hides_a_system_entry(self):
        """An older package put one in /etc; only a Hidden user entry beats it."""
        self.system.parent.mkdir(parents=True)
        self.system.write_text("[Desktop Entry]\n")
        self.assertTrue(autostart.is_enabled())
        autostart.disable()
        self.assertIn("Hidden=true", autostart.user_entry().read_text())
        self.assertFalse(autostart.is_enabled())

    def test_compositor_line_counts_as_on(self):
        conf = self.config / "hypr" / "hyprland.lua"
        conf.parent.mkdir(parents=True)
        conf.write_text('hl.exec_cmd("/usr/bin/steno gui --background")  -- listener\n')
        self.assertEqual(autostart.started_by_compositor(), conf)
        self.assertTrue(autostart.is_enabled())

    def test_commented_out_compositor_line_does_not(self):
        conf = self.config / "hypr" / "hyprland.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text("# exec-once = steno gui --background\n")
        self.assertIsNone(autostart.started_by_compositor())


if __name__ == "__main__":
    unittest.main()
