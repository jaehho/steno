"""What the bar is told, and what it refuses to believe."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steno import state


class TestBarRendering(unittest.TestCase):
    def test_idle_is_quiet(self):
        out = state.bar_json({"status": "idle"})
        self.assertEqual(out["text"], "")
        self.assertEqual(out["class"], "idle")
        self.assertEqual(out["tooltip"], "Listening for a meeting")

    def test_recording_counts_up(self):
        out = state.bar_json({"status": "recording", "detail": "Zoom", "since": 100.0},
                             now=100.0 + 185)
        self.assertEqual(out["text"], "3m")
        self.assertIn("Zoom", out["tooltip"])

    def test_recording_seconds_before_a_minute(self):
        out = state.bar_json({"status": "recording", "since": 1000.0}, now=1042.0)
        self.assertEqual(out["text"], "42s")

    def test_long_meeting_reads_in_hours(self):
        out = state.bar_json({"status": "recording", "since": 1000.0},
                             now=1000.0 + 3600 + 300)
        self.assertEqual(out["text"], "1h05")

    def test_unknown_start_shows_no_clock(self):
        """A state file without a start time still has to render something
        sane, rather than counting up from 1970."""
        out = state.bar_json({"status": "recording", "since": 0.0}, now=42.0)
        self.assertEqual(out["text"], "")

    def test_paused_counts_down_to_the_deadline(self):
        """`since` is the pause expiry, not a start — the tooltip must not
        report a countdown as elapsed time."""
        out = state.bar_json({"status": "paused", "since": 1000.0}, now=1000.0 - 600)
        self.assertEqual(out["tooltip"], "Paused · 10m left")

    def test_class_is_always_stylable(self):
        for status in state.ICONS:
            self.assertEqual(state.bar_json({"status": status})["class"], status)


class TestTrayView(unittest.TestCase):
    def test_recording_offers_stop(self):
        view = state.tray_view("recording", "Zoom")
        self.assertEqual(view["record_label"], "Stop recording")
        self.assertEqual(view["tooltip"], "Recording · Zoom")
        self.assertNotEqual(view["icon"], state.tray_view("idle")["icon"])

    def test_finalizing_cannot_start_another(self):
        self.assertFalse(state.tray_view("finalizing")["can_record"])

    def test_paused_offers_resume(self):
        self.assertTrue(state.tray_view("paused")["paused"])
        self.assertFalse(state.tray_view("idle")["paused"])

    def test_every_tray_icon_ships(self):
        from steno import icon_dir

        for name in state.TRAY_ICONS.values():
            found = list(icon_dir().glob(f"hicolor/*/*/{name}.svg"))
            self.assertTrue(found, name)
        self.assertTrue(list(icon_dir().glob("hicolor/*/apps/dev.jaeho.Steno.svg")))

    def test_unknown_status_falls_back_to_idle_icon(self):
        self.assertEqual(state.tray_view("off")["icon"], state.TRAY_ICONS["idle"])


class TestStateFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_round_trip(self):
        state.write_state("recording", "Discord", since=1234.0)
        got = state.read_state()
        self.assertEqual(got["status"], "recording")
        self.assertEqual(got["detail"], "Discord")
        self.assertEqual(got["since"], 1234.0)

    def test_missing_file_is_off(self):
        self.assertEqual(state.read_state()["status"], "off")

    def test_cleared_state_is_off(self):
        state.write_state("recording")
        state.clear_state()
        self.assertEqual(state.read_state()["status"], "off")

    def test_dead_writer_is_not_believed(self):
        """A crashed app leaves `recording` on disk. A bar that repeated it
        would be telling the user they are being recorded when they are not."""
        state.write_state("recording")
        path = state.state_path()
        payload = json.loads(path.read_text())
        payload["pid"] = 999_999_999
        path.write_text(json.dumps(payload))
        self.assertEqual(state.read_state()["status"], "off")

    def test_clear_leaves_another_process_alone(self):
        """A second app quitting must not blank the state of the one that is
        actually recording."""
        state.write_state("recording")
        path = state.state_path()
        payload = json.loads(path.read_text())
        payload["pid"] = os.getpid() + 1
        path.write_text(json.dumps(payload))
        state.clear_state()
        self.assertTrue(path.is_file())

    def test_garbage_is_off(self):
        state.state_path().write_text("{not json")
        self.assertEqual(state.read_state()["status"], "off")

    def test_unknown_status_is_off(self):
        state.write_state("sideways")
        self.assertEqual(state.read_state()["status"], "off")

    def test_write_is_atomic(self):
        """The bar polls this file; it must never read half of one."""
        state.write_state("idle")
        self.assertEqual(list(Path(self.tmp.name).glob("steno/*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
