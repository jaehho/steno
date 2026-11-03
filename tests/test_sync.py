"""Keeping meetings in step with a main store, and never losing one.

`plan` is checked on its own, as a table of cases. The rest runs the real
rclone against a plain folder standing in for the server, with two devices
sharing it.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from steno import cli, sync
from steno.sync import (
    CONFLICT,
    PULL,
    PUSH,
    TRASH_LOCAL,
    TRASH_REMOTE,
    Action,
    Snapshot,
)

NOW = datetime(2026, 11, 2, 12, 0).timestamp()
OLD = "20261101-100000"
LONG_AGO = "20260101-100000"


def kinds(plan: sync.Plan) -> set[tuple[str, str, str]]:
    return {(a.kind, a.session, a.rel) for a in plan.actions}


def planned(local, server, base, *, busy=(), audio=(), allow=False):  # noqa: PLR0913
    return sync.plan(Snapshot(local, server, base), busy=set(busy),
                     audio_pull=set(audio), allow_deletes=allow)


class TestPlan(unittest.TestCase):
    def test_a_new_meeting_here_is_pushed(self):
        found = planned({OLD: {"a.md": "1", "meta.json": "2"}}, {}, {})
        self.assertEqual(kinds(found), {
            (PUSH, OLD, "a.md"), (PUSH, OLD, "meta.json")})

    def test_a_new_meeting_there_is_pulled_when_complete(self):
        there = {OLD: {"a.md": "1", "meta.json": "2", "you.wav": "9"}}
        found = planned({}, there, {})
        self.assertEqual(kinds(found), {
            (PULL, OLD, "a.md"), (PULL, OLD, "meta.json")})

    def test_its_audio_comes_with_it_inside_the_window(self):
        there = {OLD: {"meta.json": "2", "you.wav": "9"}}
        found = planned({}, there, {}, audio={OLD})
        self.assertIn((PULL, OLD, "you.wav"), kinds(found))

    def test_a_meeting_without_meta_is_not_pulled_yet(self):
        self.assertEqual(kinds(planned({}, {OLD: {"a.md": "1"}}, {})), set())

    def test_equal_files_need_nothing(self):
        files = {OLD: {"a.md": "1"}}
        found = planned(files, files, files)
        self.assertEqual((found.actions, found.agreed), ([], files))

    def test_one_side_changed_wins(self):
        base = {OLD: {"a.md": "1", "b.md": "1"}}
        local = {OLD: {"a.md": "2", "b.md": "1"}}
        server = {OLD: {"a.md": "1", "b.md": "3"}}
        self.assertEqual(kinds(planned(local, server, base)), {
            (PUSH, OLD, "a.md"), (PULL, OLD, "b.md")})

    def test_both_changed_is_a_conflict(self):
        found = planned({OLD: {"a.md": "2"}}, {OLD: {"a.md": "3"}},
                        {OLD: {"a.md": "1"}})
        self.assertEqual(kinds(found), {(CONFLICT, OLD, "a.md")})

    def test_differing_with_no_history_is_a_conflict(self):
        found = planned({OLD: {"a.md": "2"}}, {OLD: {"a.md": "3"}}, {})
        self.assertEqual(kinds(found), {(CONFLICT, OLD, "a.md")})

    def test_a_missing_text_file_is_restored_not_deleted(self):
        base = {OLD: {"a.md": "1"}}
        found = planned({OLD: {}}, {OLD: {"a.md": "1"}}, base)
        self.assertEqual(kinds(found), {(PULL, OLD, "a.md")})
        found = planned({OLD: {"a.md": "1"}}, {OLD: {}}, base)
        self.assertEqual(kinds(found), {(PUSH, OLD, "a.md")})

    def test_audio_trimmed_here_stays_trimmed(self):
        base = {OLD: {"you.wav": "9"}}
        found = planned({OLD: {}}, {OLD: {"you.wav": "9"}}, base, audio={OLD})
        self.assertEqual(found.actions, [])

    def test_audio_from_elsewhere_follows_the_window(self):
        local, server = {OLD: {}}, {OLD: {"you.wav": "9"}}
        self.assertEqual(kinds(planned(local, server, {}, audio={OLD})),
                         {(PULL, OLD, "you.wav")})
        self.assertEqual(planned(local, server, {}).actions, [])

    def test_audio_changed_on_both_sides_is_reported_as_a_conflict(self):
        found = planned({OLD: {"you.wav": "8"}}, {OLD: {"you.wav": "9"}},
                        {OLD: {"you.wav": "7"}})
        self.assertEqual(kinds(found), {(CONFLICT, OLD, "you.wav")})

    def test_trashed_here_is_trashed_there_and_the_reverse(self):
        other = "20261031-100000"
        base = {OLD: {"a.md": "1"}, other: {"a.md": "1"}}
        found = planned({other: {"a.md": "1"}}, {OLD: {"a.md": "1"}}, base,
                        allow=True)
        self.assertEqual(kinds(found), {
            (TRASH_REMOTE, OLD, ""), (TRASH_LOCAL, other, "")})

    def test_an_empty_server_never_trashes_the_replica(self):
        base = {OLD: {"a.md": "1"}}
        found = planned({OLD: {"a.md": "1"}}, {}, base)
        self.assertEqual(found.actions, [])
        self.assertEqual(found.held, [Action(TRASH_LOCAL, OLD)])

    def test_many_deletions_at_once_are_held_until_allowed(self):
        names = [f"2026110{n}-100000" for n in range(1, 9)]
        base = {n: {"a.md": "1"} for n in names}
        local = {n: {"a.md": "1"} for n in names}
        # Seven of the eight were trashed on the server since the last sync.
        server = {names[-1]: {"a.md": "1"}}
        held = planned(local, server, base)
        self.assertEqual(len(held.held), 7)
        self.assertEqual(held.actions, [])
        allowed = planned(local, server, base, allow=True)
        self.assertEqual(len(allowed.actions), 7)

    def test_a_busy_meeting_is_left_alone(self):
        found = planned({OLD: {"a.md": "2"}}, {OLD: {"a.md": "1"}},
                        {OLD: {"a.md": "1"}}, busy={OLD})
        self.assertEqual((found.actions, found.busy), ([], [OLD]))

    def test_uploads_end_with_meta_and_downloads_put_audio_last(self):
        push = sync.order_actions([
            Action(PUSH, OLD, "meta.json"), Action(PUSH, OLD, "you.wav"),
            Action(PUSH, OLD, "a.md")])
        self.assertEqual([a.rel for a in push],
                         ["a.md", "you.wav", "meta.json"])
        pull = sync.order_actions([
            Action(PULL, OLD, "you.wav"), Action(PULL, OLD, "meta.json"),
            Action(PULL, OLD, "a.md")])
        self.assertEqual([a.rel for a in pull],
                         ["a.md", "meta.json", "you.wav"])


class TestAudioWindow(unittest.TestCase):
    def test_recent_meetings_only(self):
        self.assertEqual(sync.audio_window([OLD, LONG_AGO], NOW, 30), {OLD})

    def test_zero_days_is_text_only(self):
        self.assertEqual(sync.audio_window([OLD], NOW, 0), set())


@unittest.skipUnless(shutil.which("rclone"), "needs rclone")
class Devices(unittest.TestCase):
    """Two devices and a server folder, through the real rclone."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.server = self.tmp / "server"
        self.server.mkdir()
        self.a = self.device("a")
        self.b = self.device("b")
        for patcher in (
            mock.patch.object(sync, "read_state", return_value={}),
            mock.patch.dict(os.environ, {
                sync.REMOTE_ENV: str(self.server),
                "XDG_DATA_HOME": str(self.tmp / "data"),
            }),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def device(self, name: str) -> Path:
        root = self.tmp / name / "sessions"
        root.mkdir(parents=True)
        (self.tmp / name / "state").mkdir()
        return root

    def state_file(self, root: Path) -> Path:
        return root.parent / "state" / "steno" / sync.STATE_FILENAME

    def run_sync(self, root: Path, **kwargs) -> sync.Result:
        kwargs.setdefault("now", NOW)
        state = {"XDG_STATE_HOME": str(root.parent / "state")}
        with mock.patch.dict(os.environ, state):
            return sync.sync(root, **kwargs)

    def touch(self, path: Path, content: bytes) -> None:
        path.write_bytes(content)
        os.utime(path, (NOW - 3600, NOW - 3600))

    def meeting(self, root: Path, name: str = OLD, **files: bytes) -> Path:
        session = root / name
        session.mkdir()
        for filename, content in (files or {
            "transcript.jsonl": b"hi", "summary.md": b"# S\n",
            "you.wav": b"audio", "meta.json": b"{}",
        }).items():
            self.touch(session / filename, content)
        return session

    def test_a_meeting_goes_up_and_comes_down(self):
        self.meeting(self.a)
        up = self.run_sync(self.a)
        self.assertEqual((up.status, len(up.pushed)), ("ok", 4))
        down = self.run_sync(self.b)
        self.assertEqual(len(down.pulled), 4)
        self.assertEqual((self.b / OLD / "summary.md").read_bytes(), b"# S\n")
        self.assertEqual((self.b / OLD / "you.wav").read_bytes(), b"audio")

    def test_a_second_round_changes_nothing(self):
        self.meeting(self.a)
        self.run_sync(self.a)
        again = self.run_sync(self.a)
        self.assertEqual((again.pushed, again.pulled, again.unchanged),
                         ([], [], 4))

    def test_an_edit_here_reaches_the_other_device(self):
        self.meeting(self.a)
        self.run_sync(self.a)
        self.run_sync(self.b)
        self.touch(self.a / OLD / "summary.md", b"# Edited\n")
        self.run_sync(self.a)
        self.run_sync(self.b)
        self.assertEqual((self.b / OLD / "summary.md").read_bytes(),
                         b"# Edited\n")

    def test_both_editing_keeps_the_server_version_and_your_own_aside(self):
        self.meeting(self.a)
        self.run_sync(self.a)
        self.run_sync(self.b)
        self.touch(self.a / OLD / "summary.md", b"from a")
        self.touch(self.b / OLD / "summary.md", b"from b")
        self.run_sync(self.a)
        got = self.run_sync(self.b)
        self.assertEqual(len(got.conflicts), 1)
        self.assertEqual((self.b / OLD / "summary.md").read_bytes(), b"from a")
        kept = list((self.b / OLD).glob("summary.md.conflict-*"))
        self.assertEqual([p.read_bytes() for p in kept], [b"from b"])
        self.assertEqual(sync.conflicts_on_disk(self.b),
                         [f"{OLD}/{kept[0].name}"])
        self.run_sync(self.b)
        self.assertEqual(list(self.server.glob("*/*.conflict-*")), [])

    def test_trashing_a_meeting_moves_it_aside_here_and_elsewhere(self):
        self.meeting(self.a)
        self.meeting(self.a, "20261031-100000")
        self.run_sync(self.a)
        self.run_sync(self.b)
        sync.delete_session(self.a / OLD)
        gone = self.run_sync(self.a)
        self.assertEqual(gone.trashed_remote, [OLD])
        self.assertFalse((self.server / OLD).exists())
        self.assertEqual(len(list((self.server / ".trash").iterdir())), 1)
        followed = self.run_sync(self.b)
        self.assertEqual(followed.trashed_local, [OLD])
        self.assertFalse((self.b / OLD).exists())
        self.assertTrue((self.b / "20261031-100000").exists())

    def test_trimming_audio_here_leaves_it_on_the_server(self):
        self.meeting(self.a)
        self.run_sync(self.a)
        (self.a / OLD / "you.wav").unlink()
        again = self.run_sync(self.a)
        self.assertEqual((again.pulled, again.pushed), ([], []))
        self.assertTrue((self.server / OLD / "you.wav").exists())
        self.assertFalse((self.a / OLD / "you.wav").exists())

    def test_fetch_brings_the_audio_back_and_clears_the_marks(self):
        self.meeting(self.a, **{
            "meta.json": json.dumps({"audio_pruned_at": 1}).encode(),
            "transcript.jsonl": b"hi", "you.wav": b"audio"})
        self.run_sync(self.a)
        (self.a / OLD / "you.wav").unlink()
        state = {"XDG_STATE_HOME": str(self.a.parent / "state")}
        with mock.patch.dict(os.environ, state):
            got = sync.fetch_audio(OLD, self.a, now=NOW)
        self.assertEqual(got.pulled, [f"{OLD}/you.wav"])
        self.assertEqual((self.a / OLD / "you.wav").read_bytes(), b"audio")
        meta = json.loads((self.a / OLD / "meta.json").read_text())
        self.assertNotIn("audio_pruned_at", meta)

    def test_fetch_leaves_a_meta_without_marks_byte_for_byte(self):
        original = b'{"title":"x",  "lines": 3}'
        self.meeting(self.a, **{"meta.json": original, "you.wav": b"audio",
                                "transcript.jsonl": b"hi"})
        self.run_sync(self.a)
        (self.a / OLD / "you.wav").unlink()
        state = {"XDG_STATE_HOME": str(self.a.parent / "state")}
        with mock.patch.dict(os.environ, state):
            sync.fetch_audio(OLD, self.a, now=NOW)
        self.assertEqual((self.a / OLD / "meta.json").read_bytes(), original)

    def test_audio_outside_the_window_is_left_on_the_server(self):
        self.meeting(self.a, LONG_AGO)
        self.run_sync(self.a)
        got = self.run_sync(self.b)
        self.assertFalse((self.b / LONG_AGO / "you.wav").exists())
        self.assertTrue((self.b / LONG_AGO / "summary.md").exists())
        self.assertEqual(len(got.pulled), 3)

    def test_a_meeting_just_written_to_waits(self):
        session = self.meeting(self.a)
        os.utime(session / "summary.md", (NOW - 10, NOW - 10))
        got = self.run_sync(self.a)
        self.assertEqual((got.pushed, got.busy), ([], [OLD]))

    def test_a_file_changed_after_the_plan_is_not_overwritten(self):
        self.meeting(self.a)
        self.run_sync(self.a)
        self.run_sync(self.b)
        self.touch(self.a / OLD / "summary.md", b"new on a")
        self.run_sync(self.a)
        real_copy = sync.copy_file

        def edit_then_copy(source, target):
            if "summary.md" in target:
                self.touch(self.b / OLD / "summary.md", b"typed meanwhile")
            real_copy(source, target)

        with mock.patch.object(sync, "copy_file", edit_then_copy):
            got = self.run_sync(self.b)
        self.assertEqual((got.failed, got.busy), ({}, [f"{OLD}/summary.md"]))
        self.assertEqual((self.b / OLD / "summary.md").read_bytes(),
                         b"typed meanwhile")

    def test_an_unreachable_server_ends_the_round_quietly(self):
        self.meeting(self.a)
        failure = sync.SyncError("no route to host", code=1)
        with mock.patch.object(sync, "_rclone", side_effect=failure):
            got = self.run_sync(self.a)
        self.assertEqual((got.status, got.detail),
                         ("unreachable", "no route to host"))
        self.assertTrue((self.a / OLD / "summary.md").exists())
        last = json.loads(self.state_file(self.a).read_text())["last"]
        self.assertEqual(last["status"], "unreachable")

    def test_an_unreachable_server_never_trashes_anything(self):
        self.meeting(self.a)
        self.run_sync(self.a)
        failure = sync.SyncError("timeout", code=1)
        with mock.patch.object(sync, "_rclone", side_effect=failure):
            self.run_sync(self.a)
        self.assertTrue((self.a / OLD).exists())
        self.assertTrue((self.server / OLD).exists())

    def test_a_server_folder_that_was_emptied_does_not_empty_the_replica(self):
        self.meeting(self.a)
        self.run_sync(self.a)
        shutil.rmtree(self.server / OLD)
        got = self.run_sync(self.a)
        self.assertEqual((got.status, got.held), ("held", [OLD]))
        self.assertTrue((self.a / OLD).exists())

    def test_a_dry_run_changes_nothing(self):
        self.meeting(self.a)
        got = self.run_sync(self.a, dry_run=True)
        self.assertEqual(len(got.pushed), 4)
        self.assertEqual(list(self.server.iterdir()), [])
        self.assertFalse(self.state_file(self.a).exists())

    def test_uploads_put_meta_last(self):
        self.meeting(self.a)
        got = self.run_sync(self.a)
        self.assertEqual(got.pushed[-1], f"{OLD}/meta.json")
        self.assertEqual(got.pushed[-2], f"{OLD}/you.wav")

    def test_a_failed_step_is_retried_next_round(self):
        self.meeting(self.a)
        real = sync.copy_file

        def fail_summary(source, target):
            if "summary.md" in target:
                raise sync.SyncError("boom")
            real(source, target)

        with mock.patch.object(sync, "copy_file", fail_summary):
            first = self.run_sync(self.a)
        self.assertEqual(first.status, "error")
        self.assertEqual(list(first.failed), [f"{OLD}/summary.md"])
        second = self.run_sync(self.a)
        self.assertEqual(second.pushed, [f"{OLD}/summary.md"])

    def test_off_without_a_remote(self):
        with mock.patch.dict(os.environ, {sync.REMOTE_ENV: " "}):
            self.assertEqual(self.run_sync(self.a).status, "off")

    def test_scratch_and_aside_files_are_never_synced(self):
        session = self.meeting(self.a)
        self.touch(session / "x.sync.tmp", b"half")
        self.touch(session / "summary.md.conflict-h-1", b"aside")
        self.run_sync(self.a)
        self.assertEqual(
            sorted(p.name for p in (self.server / OLD).iterdir()),
            ["meta.json", "summary.md", "transcript.jsonl", "you.wav"])


class TestCommand(unittest.TestCase):
    def test_the_command_reads_the_remote_from_the_env_file(self):
        seen = {}

        def fake_sync(**_kwargs):
            seen["remote"] = cli.meeting_sync.remote()
            return sync.Result(status="off")

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config" / "steno"
            config.mkdir(parents=True)
            (config / ".env").write_text(
                f"{sync.REMOTE_ENV}=nextcloud:Steno/from-file\n")
            with (
                mock.patch.dict(os.environ,
                                {"XDG_CONFIG_HOME": str(config.parent)}),
                mock.patch.object(cli.meeting_sync, "sync", fake_sync),
            ):
                os.environ.pop(sync.REMOTE_ENV, None)
                cli.cmd_sync(argparse.Namespace(
                    dry_run=True, allow_deletes=False))
        self.assertEqual(seen["remote"], "nextcloud:Steno/from-file")


if __name__ == "__main__":
    unittest.main()
