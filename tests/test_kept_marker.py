"""A room kept on this laptop is kept out of the backup too.

Room mode tells the user nothing of a room has left the laptop until
everyone agreed, and sync leaves such a meeting alone
(`room.awaits_agreement`). A backup of the home folder reads no
meta.json, so steno writes an empty `.steno-kept` beside a room for as
long as it waits, and restic's `--exclude-if-present .steno-kept` leaves
it out. A call never carries it, not even one whose mic pass failed.

The engine runs as in tests/test_room.py (Deepgram an
httpx.MockTransport, the model a mock); the backup is the real restic
against a throwaway repository in a temporary folder.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from steno import claude, kept, merge, realign, session, sync
from steno.summarize import load_meta, update_meta
from tests.test_merge import write_meta, write_records
from tests.test_room import (
    STARTED,
    SUMMARY,
    THREE_VOICES,
    Deepgram,
    Scratch,
    make_session,
    write_wav,
)


class TestMarkerFollowsTheFlag(Scratch):
    """Each writer of `mic_pending` writes or removes the marker."""

    def test_a_recording_is_marked_from_its_first_second(self):
        folder = self.root / "20261112-100000"
        folder.mkdir()
        session._rewrite_meta(folder, STARTED, "manual", manual=True)
        self.assertTrue(kept.has_marker(folder))
        self.assertTrue(load_meta(folder)["mic_pending"])

    def test_a_call_app_recording_is_never_marked(self):
        folder = self.root / "20261112-110000"
        folder.mkdir()
        session._rewrite_meta(folder, STARTED, "ZOOM", manual=False)
        self.assertFalse(kept.has_marker(folder))
        self.assertTrue(load_meta(folder)["mic_pending"])

    def test_a_call_whose_mic_pass_has_not_run_is_not_kept(self):
        """A call's mic pass that failed, offline say, leaves
        `mic_pending`: that is no reason to keep it from sync or the
        backup."""
        for manual, far in ((False, []), (True, ["Can you hear me?"])):
            with self.subTest(manual=manual):
                folder = make_session(self.root, f"call-{manual}",
                                      manual=manual, far=far)
                update_meta(folder, mic_pending=True)
                self.assertFalse(kept.has_marker(folder))
                self.assertFalse(sync.is_kept(folder))

    def test_one_voice_is_a_voice_memo_and_loses_the_marker(self):
        folder = make_session(self.root, "memo", manual=True, far=[])
        update_meta(folder, mic_pending=True)
        self.assertTrue(kept.has_marker(folder))
        update_meta(folder, room=False)
        self.assertFalse(kept.has_marker(folder))

    def test_update_meta_sets_and_clears_it(self):
        folder = make_session(self.root, "room", manual=True, far=[])
        update_meta(folder, mic_pending=True)
        self.assertTrue(kept.has_marker(folder))
        update_meta(folder, title="Weekly sync")
        self.assertTrue(kept.has_marker(folder))
        update_meta(folder, mic_pending=None)
        self.assertFalse(kept.has_marker(folder))

    def test_realign_clears_it(self):
        folder = make_session(self.root, "old", manual=False, far=["hi"],
                              meta={"mic_pending": True})
        kept.set_kept(folder, kept=True)
        realign._stamp(folder, STARTED, 3, "deepgram:nova-3")
        self.assertNotIn("mic_pending", load_meta(folder))
        self.assertFalse(kept.has_marker(folder))

    def test_a_merge_keeps_it_on_the_meeting_kept(self):
        keep, absorb = self.root / "20260101-100000", self.root / "20260101-110000"
        for folder, start in ((keep, 1_000_000.0), (absorb, 1_000_030.0)):
            folder.mkdir()
            write_wav(folder / "you.wav")
            write_wav(folder / "them.wav")
            write_records(folder, [])
            write_meta(folder, started_at=start, app="manual", manual=True)
        update_meta(absorb, mic_pending=True)
        env = mock.patch.dict(os.environ, {
            "XDG_DATA_HOME": str(self.scratch / "data")})
        env.start()
        self.addCleanup(env.stop)
        merge.merge_sessions(keep, absorb)
        self.assertTrue(kept.has_marker(keep))

    def test_startup_puts_drift_right_and_leaves_an_unreadable_meta(self):
        pending = make_session(self.root, "a", manual=True, far=[],
                               meta={"mic_pending": True})
        done = make_session(self.root, "b", manual=True, far=[])
        kept.set_kept(done, kept=True)
        broken = make_session(self.root, "c", manual=True, far=[])
        kept.set_kept(broken, kept=True)
        (broken / "meta.json").write_text("{half")
        call = make_session(self.root, "d", manual=False, far=["hi"],
                            meta={"mic_pending": True})
        kept.set_kept(call, kept=True)
        changed = kept.mark_all(sync.session_dirs(self.root))
        self.assertEqual(sorted(p.name for p in changed), ["a", "b", "d"])
        self.assertFalse(kept.has_marker(call))
        self.assertTrue(kept.has_marker(pending))
        self.assertFalse(kept.has_marker(done))
        self.assertTrue(kept.has_marker(broken))

    def test_sync_never_carries_the_marker_and_honours_it(self):
        folder = make_session(self.root, "room", manual=True, far=[])
        kept.set_kept(folder, kept=True)
        self.assertNotIn(kept.MARKER, sync.local_files(self.root)["room"])
        self.assertTrue(sync.is_kept(folder))


class Engine(Scratch):
    """A real engine ending meetings, as in test_room.TestKeptUntilAgreed."""

    def setUp(self):
        super().setUp()
        self.engine = session.Engine(
            emit=lambda _e: None, root=self.root,
            brief_path=self.root / "brief.md", model="m",
        )
        for patcher in (
            mock.patch.dict(os.environ, {
                "STENO_PROJECTS_DIR": str(self.scratch / "projects")}),
            mock.patch.object(claude, "run_structured", mock.AsyncMock(
                return_value=({"summary": SUMMARY, "project": None,
                               "voices": []}, "fake-model"))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        Deepgram(self, THREE_VOICES)

    def end(self, folder: Path, *, manual: bool) -> None:
        rec = session.Recording(
            session_dir=folder, started_at=STARTED,
            app="manual" if manual else "ZOOM", manual=manual,
        )
        asyncio.run(self.engine._run_notes_job(folder, rec=rec))

    def agree(self, folder: Path) -> None:
        """The user says everyone in the room agreed."""
        asyncio.run(self.engine.summarize_now(folder))


class TestRoomIsMarkedUntilEveryoneAgreed(Engine):
    def test_a_room_carries_the_marker_until_the_user_says_yes(self):
        folder = make_session(self.root, "room", manual=True, far=[])
        self.end(folder, manual=True)
        self.assertTrue(kept.has_marker(folder))
        self.agree(folder)
        self.assertFalse(kept.has_marker(folder))

    def test_a_call_ends_without_one(self):
        folder = make_session(self.root, "call", manual=False,
                              far=["Can you hear me?"],
                              meta={"mic_pending": True})
        kept.set_kept(folder, kept=True)
        self.end(folder, manual=False)
        self.assertFalse(kept.has_marker(folder))

    def test_a_call_started_by_hand_loses_it_even_if_its_mic_pass_fails(self):
        folder = make_session(self.root, "call", manual=True,
                              far=["Can you hear me?"],
                              meta={"mic_pending": True})
        kept.set_kept(folder, kept=True)  # as the first second left it
        with mock.patch.object(self.engine, "_mic_pass", mock.AsyncMock(
                return_value=([], False))):
            self.end(folder, manual=True)
        self.assertTrue(load_meta(folder)["mic_pending"])
        self.assertFalse(kept.has_marker(folder))
        self.assertFalse(sync.is_kept(folder))


@unittest.skipUnless(shutil.which("restic"), "needs restic")
class TestBackupLeavesTheRoomOut(Engine):
    """The flag the dotfiles' backup unit would pass, on a real repo."""

    def setUp(self):
        super().setUp()
        self.env = {
            **os.environ,
            "RESTIC_REPOSITORY": str(self.scratch / "repo"),
            "RESTIC_PASSWORD": "throwaway-test-only",
            "RESTIC_CACHE_DIR": str(self.scratch / "cache"),
        }
        self.restic("init")

    def restic(self, *args: str) -> str:
        done = subprocess.run(["restic", "-q", *args], env=self.env,
                              capture_output=True, text=True, check=True)
        return done.stdout

    def backed_up(self) -> set[str]:
        """Every file in a fresh snapshot, relative to the sessions."""
        self.restic("backup", "--exclude-if-present", kept.MARKER,
                    str(self.root))
        prefix = f"{self.root}/"
        return {
            line.removeprefix(prefix)
            for line in self.restic("ls", "latest").splitlines()
            if line.startswith(prefix)
        }

    def test_the_room_is_left_out_until_everyone_agreed(self):
        room = make_session(self.root, "room", manual=True, far=[])
        call = make_session(self.root, "call", manual=False,
                            far=["Can you hear me?"],
                            meta={"mic_pending": True})
        self.end(room, manual=True)
        self.end(call, manual=False)

        held = self.backed_up()
        self.assertIn("call/you.wav", held)
        self.assertIn("call/them.wav", held)
        self.assertEqual(
            {f for f in held if f.startswith("room/")}, {"room/.steno-kept"})

        self.agree(room)
        agreed = self.backed_up()
        self.assertIn("room/you.wav", agreed)
        self.assertIn("room/summary.md", agreed)

    def test_a_meeting_mid_recording_is_left_out(self):
        folder = self.root / "20261112-100000"
        folder.mkdir()
        session._rewrite_meta(folder, STARTED, "manual", manual=True)
        write_wav(folder / "you.wav")
        held = self.backed_up()
        self.assertNotIn("20261112-100000/you.wav", held)
        self.assertEqual(json.loads((folder / "meta.json").read_text())[
            "mic_pending"], True)


if __name__ == "__main__":
    unittest.main()
