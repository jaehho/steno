"""A detected call is recorded from its start, then kept or deleted.

No audio, network or bus: capture and the notes job are faked, and the
detector's events are fed to the engine by hand.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steno import consent, session
from steno.detect import MeetingEvent
from steno.events import (
    MeetingDiscarded,
    MeetingEnded,
    MeetingKept,
    MeetingStarted,
)
from tests.test_sidebar_menu import ROOT, SCRUBBED, start_broadway

APP = "ZOOM VoiceEngine"


class TestMode(unittest.TestCase):
    def test_unset_asks_in_europe(self):
        self.assertEqual(consent.consent_mode("", "Europe/Lisbon"), "ask")

    def test_unset_records_in_new_york(self):
        self.assertEqual(consent.consent_mode("", "America/New_York"), "auto")

    def test_unset_records_where_the_zone_is_unknown(self):
        self.assertEqual(consent.consent_mode("", ""), "auto")

    def test_a_setting_beats_the_zone(self):
        self.assertEqual(consent.consent_mode("auto", "Europe/Lisbon"), "auto")
        self.assertEqual(consent.consent_mode(" ASK ", "America/New_York"), "ask")

    def test_a_setting_it_does_not_know_is_refused(self):
        with self.assertRaisesRegex(ValueError, "STENO_CONSENT='yes'"):
            consent.consent_mode("yes", "Europe/Lisbon")

    def test_the_running_app_asks_when_the_setting_is_unreadable(self):
        env = {"STENO_CONSENT": "yes", "TZ": "America/New_York"}
        with self.assertLogs("steno.consent", "WARNING"):
            self.assertEqual(consent.consent_from_env(env), "ask")


class TestZone(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.link = Path(tmp.name) / "localtime"

    def test_tz_wins(self):
        self.assertEqual(
            consent.machine_zone(":Europe/Lisbon", self.link), "Europe/Lisbon"
        )

    def test_the_link_timedatectl_writes(self):
        self.link.symlink_to("/usr/share/zoneinfo/Europe/Lisbon")
        self.assertEqual(consent.machine_zone("", self.link), "Europe/Lisbon")

    def test_no_link_names_no_zone(self):
        self.assertEqual(consent.machine_zone("", self.link), "")


class TestKeepOrDiscard(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "sessions"
        self.mode = "auto"
        self.events: list = []
        self.notes_jobs = 0
        self.engine = session.Engine(
            emit=self.events.append, root=self.root,
            brief_path=Path(tmp.name) / "brief.md", model="m",
            consent=lambda: self.mode,
        )

    def run_steps(self, *steps) -> None:
        """Feed detector events ("start"/"end") and commands to the engine,
        in order, with capture and the notes job faked."""

        async def opened():
            return "fake-device"

        async def idle(*_a, **_kw):
            return None

        async def notes(*_a, **_kw):
            self.notes_jobs += 1

        engine = self.engine

        async def scenario():
            for step in steps:
                if step in {"start", "end"}:
                    await engine._on_detect(MeetingEvent(step, APP))
                elif step == "toggle":  # `steno toggle`: a start you asked for
                    await engine._on_command("start")
                else:
                    await engine._on_command(step)
            await asyncio.gather(*engine._finishers)

        with mock.patch.object(session, "capture_mic", opened), \
             mock.patch.object(session, "default_sink_monitor", opened), \
             mock.patch.object(engine, "_record_mic", idle), \
             mock.patch.object(engine, "_stream_them", idle), \
             mock.patch.object(engine, "_pump", idle), \
             mock.patch.object(engine, "_run_notes_job", notes):
            asyncio.run(scenario())

    def sessions(self) -> list[Path]:
        return sorted(self.root.glob("*")) if self.root.exists() else []

    def of(self, kind) -> list:
        return [e for e in self.events if isinstance(e, kind)]

    def test_auto_records_a_call_from_its_start_and_keeps_it(self):
        self.run_steps("start")
        [made] = self.sessions()
        meta = json.loads((made / "meta.json").read_text())
        self.assertEqual((meta["app"], meta["manual"]), (APP, False))
        [started] = self.of(MeetingStarted)
        self.assertTrue(started.keep_at_end)
        self.run_steps("end")
        self.assertEqual(self.sessions(), [made])
        self.assertEqual(len(self.of(MeetingEnded)), 1)
        self.assertEqual(self.notes_jobs, 1)

    def test_dont_record_deletes_the_call_and_writes_no_notes(self):
        self.run_steps("start", "discard")
        self.assertEqual(self.sessions(), [])
        self.assertIsNone(self.engine.current)
        [gone] = self.of(MeetingDiscarded)
        self.assertEqual(gone.app, APP)
        self.assertEqual(self.of(MeetingEnded), [])
        self.assertEqual(self.notes_jobs, 0)

    def test_the_app_letting_go_after_dont_record_changes_nothing(self):
        self.run_steps("start", "discard", "end")
        self.assertEqual(self.sessions(), [])
        self.assertEqual(self.of(MeetingEnded), [])

    def test_ask_records_from_the_start_but_not_kept(self):
        self.mode = "ask"
        self.run_steps("start")
        self.assertEqual(len(self.sessions()), 1)
        [started] = self.of(MeetingStarted)
        self.assertFalse(started.keep_at_end)

    def test_ask_deletes_a_call_nobody_kept_when_it_ends(self):
        self.mode = "ask"
        self.run_steps("start", "end")
        self.assertEqual(self.sessions(), [])
        self.assertEqual(len(self.of(MeetingDiscarded)), 1)
        self.assertEqual(self.of(MeetingEnded), [])
        self.assertEqual(self.notes_jobs, 0)

    def test_ask_keeps_a_call_you_kept(self):
        self.mode = "ask"
        self.run_steps("start", "keep", "end")
        self.assertEqual(len(self.sessions()), 1)
        self.assertEqual(len(self.of(MeetingKept)), 1)
        self.assertEqual(len(self.of(MeetingEnded)), 1)
        self.assertEqual(self.notes_jobs, 1)

    def test_a_recording_you_started_is_always_kept(self):
        self.mode = "ask"
        self.run_steps("toggle", "end", "stop")
        [started] = self.of(MeetingStarted)
        self.assertTrue(started.keep_at_end)
        self.assertEqual(len(self.sessions()), 1)
        self.assertEqual(len(self.of(MeetingEnded)), 1)

    def test_the_mode_is_read_at_each_call(self):
        """Changing the zone on arrival must not need a restart."""
        self.run_steps("start", "end")
        self.mode = "ask"
        self.run_steps("start", "end")
        self.assertEqual(len(self.sessions()), 1)
        self.assertEqual(len(self.of(MeetingDiscarded)), 1)

    def test_keep_and_discard_with_nothing_recording_do_nothing(self):
        self.run_steps("keep", "discard")
        self.assertEqual(self.events, [])


class TestOnBroadway(unittest.TestCase):
    def test_the_recording_notice_in_the_app_on_broadway(self):
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
                     "-m", "unittest", "-v", "tests.gtk_consent"],
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
