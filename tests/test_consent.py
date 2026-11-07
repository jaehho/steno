"""Where every speaker must agree, a detected call is offered, not recorded.

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
from steno.events import CallDetected, CallGone, MeetingEnded, MeetingStarted
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


class TestAskFirst(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "sessions"
        self.mode = "ask"
        self.events: list = []
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

        engine = self.engine

        async def scenario():
            for step in steps:
                if step in {"start", "end"}:
                    await engine._on_detect(MeetingEvent(step, APP))
                else:
                    await engine._on_command(step)
            await asyncio.gather(*engine._finishers)

        with mock.patch.object(session, "capture_mic", opened), \
             mock.patch.object(session, "default_sink_monitor", opened), \
             mock.patch.object(engine, "_record_mic", idle), \
             mock.patch.object(engine, "_stream_them", idle), \
             mock.patch.object(engine, "_pump", idle), \
             mock.patch.object(engine, "_run_notes_job", idle):
            asyncio.run(scenario())

    def sessions(self) -> list[Path]:
        return sorted(self.root.glob("*")) if self.root.exists() else []

    def of(self, kind) -> list:
        return [e for e in self.events if isinstance(e, kind)]

    def test_a_detected_call_is_offered_and_nothing_is_recorded(self):
        self.run_steps("start")
        self.assertEqual(self.sessions(), [])
        self.assertIsNone(self.engine.current)
        self.assertEqual(self.of(CallDetected), [CallDetected(APP)])
        self.assertEqual(self.of(MeetingStarted), [])

    def test_accepting_records_the_call_until_the_app_lets_go(self):
        self.run_steps("start", "accept")
        [made] = self.sessions()
        meta = json.loads((made / "meta.json").read_text())
        self.assertEqual((meta["app"], meta["manual"]), (APP, False))
        self.assertIsNotNone(self.engine.current)
        self.run_steps("end")
        self.assertIsNone(self.engine.current)
        self.assertEqual(len(self.of(MeetingEnded)), 1)

    def test_a_call_nobody_accepted_leaves_nothing_behind(self):
        self.run_steps("start", "end", "accept")
        self.assertEqual(self.sessions(), [])
        self.assertEqual(self.of(CallGone), [CallGone(APP)])
        self.assertIsNone(self.engine.offered)

    def test_auto_records_at_once_as_before(self):
        self.mode = "auto"
        self.run_steps("start")
        self.assertEqual(len(self.sessions()), 1)
        self.assertEqual(self.of(CallDetected), [])
        self.assertEqual(len(self.of(MeetingStarted)), 1)

    def test_the_mode_is_read_at_each_call(self):
        """Changing the zone on arrival must not need a restart."""
        self.mode = "auto"
        self.run_steps("start", "end")
        self.mode = "ask"
        self.run_steps("start")
        self.assertEqual(len(self.sessions()), 1)
        self.assertEqual(self.of(CallDetected), [CallDetected(APP)])


class TestOnBroadway(unittest.TestCase):
    def test_the_call_offer_in_the_app_on_broadway(self):
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
