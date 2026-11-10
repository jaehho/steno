"""What the bar is told, and what it refuses to believe."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steno import icon_dir, livesrc, state


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

    def test_a_test_build_says_so_in_the_tooltip_and_a_field(self):
        out = state.bar_json({"status": "idle", "src": "feat/x@201e472"})
        self.assertEqual(out["src"], "feat/x@201e472")
        self.assertEqual(out["tooltip"],
                         "Listening for a meeting · test build feat/x@201e472")
        self.assertEqual(out["class"], "idle")

    def test_a_recording_test_build_keeps_its_clock_and_says_so(self):
        out = state.bar_json({"status": "recording", "detail": "Zoom",
                              "since": 100.0, "src": "feat/x@1"},
                             now=100.0 + 185)
        self.assertEqual(out["text"], "3m")
        self.assertEqual(out["tooltip"],
                         "Recording · Zoom · 3m · test build feat/x@1")

    def test_the_installed_build_prints_what_it_always_has(self):
        # Readers pinned to the old fields see no change on main.
        out = state.bar_json({"status": "idle"})
        self.assertNotIn("src", out)
        self.assertEqual(out["tooltip"], "Listening for a meeting")

    def test_off_names_no_build(self):
        out = state.bar_json({"status": "off", "src": "feat/x@1"})
        self.assertNotIn("src", out)
        self.assertEqual(out["tooltip"], "Steno is not running")

    def test_class_is_always_stylable(self):
        for status in state.ICONS:
            self.assertEqual(state.bar_json({"status": status})["class"], status)


class TestTrayView(unittest.TestCase):
    def test_recording_offers_stop(self):
        view = state.tray_view("recording", "Zoom")
        self.assertEqual(view["record_label"], "Stop recording")
        self.assertEqual(view["tooltip"], "Recording · Zoom")
        self.assertNotEqual(view["icon"], state.tray_view("idle")["icon"])

    def test_only_a_recording_asks_for_attention(self):
        # The bar greys every tray icon that is not NeedsAttention, and the
        # recording icon differs from idle by colour alone.
        self.assertEqual(state.tray_view("recording")["status"], "NeedsAttention")
        for status in ("idle", "paused", "finalizing", "off"):
            self.assertEqual(state.tray_view(status)["status"], "Active", status)

    def test_finalizing_cannot_start_another(self):
        self.assertFalse(state.tray_view("finalizing")["can_record"])

    def test_paused_offers_resume(self):
        self.assertTrue(state.tray_view("paused")["paused"])
        self.assertFalse(state.tray_view("idle")["paused"])

    def test_every_tray_icon_ships(self):

        for name in state.TRAY_ICONS.values():
            found = list(icon_dir().glob(f"hicolor/*/*/{name}.svg"))
            self.assertTrue(found, name)
        self.assertTrue(list(icon_dir().glob("hicolor/*/apps/dev.jaeho.Steno.svg")))
        # Waybar searches only IconThemePath; without the index every lookup misses.
        index = (icon_dir() / "hicolor/index.theme").read_text()
        for sub in {p.parent.relative_to(icon_dir() / "hicolor").as_posix()
                    for p in icon_dir().glob("hicolor/*/*/*.svg")}:
            self.assertIn(f"[{sub}]", index)

    def test_a_test_build_names_itself_in_the_title_and_menu(self):
        # Quickshell's bar shows an item's tooltip title (else its
        # Title), never the description, so the name goes in the title.
        view = state.tray_view("idle", src="feat/x@1")
        self.assertEqual(view["title"], "Steno · test build feat/x@1")
        self.assertEqual(view["tooltip"], "Listening for a meeting")
        self.assertEqual(view["status_line"],
                         "Listening for a meeting · test build feat/x@1")

    def test_a_test_build_asks_for_attention_in_every_state(self):
        # The bar turns its shut tray drawer's chevron orange only for
        # NeedsAttention, and an idle test build is the one to catch.
        for status in ("idle", "paused", "recording", "finalizing"):
            view = state.tray_view(status, src="feat/x@1")
            self.assertEqual(view["status"], "NeedsAttention", status)

    def test_the_installed_build_looks_as_it_always_has(self):
        view = state.tray_view("idle")
        self.assertEqual(view["title"], "Steno")
        self.assertEqual(view["status"], "Active")
        self.assertEqual(view["status_line"], "Listening for a meeting")

    def test_unknown_status_falls_back_to_idle_icon(self):
        self.assertEqual(state.tray_view("off")["icon"], state.TRAY_ICONS["idle"])


class TestStateFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": self.tmp.name,
            "XDG_STATE_HOME": str(Path(self.tmp.name) / "state"),
        })
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

    def lease(self, **fields):
        record = {"pid": os.getpid(), "test_build": True,
                  "label": "feat/x@201e472"}
        livesrc.write_lease(livesrc.lease_path(), {**record, **fields})

    def test_the_running_apps_test_build_lease_reaches_the_bar(self):
        state.write_state("idle")
        self.lease()
        self.assertEqual(state.read_state()["src"], "feat/x@201e472")
        self.assertIn("test build feat/x@201e472",
                      state.bar_json(state.read_state())["tooltip"])

    def test_a_lease_from_another_process_is_not_believed(self):
        # The test build was replaced by main, which has not leased yet,
        # or a stale lease survived a reboot.
        state.write_state("idle")
        self.lease(pid=os.getpid() + 1)
        self.assertNotIn("src", state.read_state())

    def test_the_installed_checkout_names_no_build(self):
        state.write_state("idle")
        self.lease(test_build=False, label="main@130d018")
        self.assertNotIn("src", state.read_state())

    def test_no_lease_is_no_build(self):
        state.write_state("idle")
        self.assertNotIn("src", state.read_state())

    def test_write_is_atomic(self):
        """The bar polls this file; it must never read half of one."""
        state.write_state("idle")
        self.assertEqual(list(Path(self.tmp.name).glob("steno/*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
