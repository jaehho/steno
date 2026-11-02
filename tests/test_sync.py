"""Pushing meetings to a remote: only finished ones, only once, never delete."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from itertools import pairwise
from pathlib import Path
from unittest import mock

from steno import sync

NOW = 1_800_000_000.0


def make_session(root: Path, name: str, *, age_s: float = 3600) -> Path:
    session = root / name
    session.mkdir(parents=True)
    for filename in ("transcript.jsonl", "you.wav", "meta.json"):
        path = session / filename
        path.write_bytes(b"x")
        os.utime(path, (NOW - age_s, NOW - age_s))
    return session


def done(code: int = 0, err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, "", err)


class SyncCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "sessions"
        self.root.mkdir()
        env = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(Path(tmp.name) / "state"),
            sync.REMOTE_ENV: "nextcloud:Steno/sessions/",
        })
        env.start()
        self.addCleanup(env.stop)
        status = mock.patch.object(sync, "read_state", return_value={})
        status.start()
        self.addCleanup(status.stop)
        which = mock.patch.object(sync.shutil, "which", return_value="/bin/rclone")
        which.start()
        self.addCleanup(which.stop)
        run = mock.patch.object(sync.subprocess, "run", return_value=done())
        self.run_ = run.start()
        self.addCleanup(run.stop)

    def commands(self) -> list[list[str]]:
        return [c.args[0] for c in self.run_.call_args_list]


class TestSync(SyncCase):
    def test_off_without_a_remote(self):
        with mock.patch.dict(os.environ, {sync.REMOTE_ENV: " "}):
            result = sync.sync(self.root, now=NOW)
        self.assertTrue(result.off)
        self.run_.assert_not_called()

    def test_pushes_a_finished_meeting_with_meta_last(self):
        make_session(self.root, "20261101-100000")
        result = sync.sync(self.root, now=NOW)
        self.assertEqual(result.pushed, ["20261101-100000"])
        first, last = self.commands()
        self.assertEqual(first[:2], ["rclone", "copy"])
        self.assertEqual(
            first[3], "nextcloud:Steno/sessions/20261101-100000")
        self.assertIn("--update", first)
        pairs = list(pairwise(first))
        self.assertIn(("--exclude", "meta.json"), pairs)
        self.assertEqual(last[last.index("--include") + 1], "meta.json")

    def test_never_deletes(self):
        make_session(self.root, "20261101-100000")
        sync.sync(self.root, now=NOW)
        for command in self.commands():
            self.assertEqual(command[1], "copy")
            self.assertNotIn("--delete-after", command)

    def test_a_second_run_sends_nothing(self):
        make_session(self.root, "20261101-100000")
        sync.sync(self.root, now=NOW)
        self.run_.reset_mock()
        result = sync.sync(self.root, now=NOW)
        self.assertEqual((result.pushed, result.unchanged), ([], 1))
        self.run_.assert_not_called()

    def test_an_edit_after_the_push_sends_it_again(self):
        session = make_session(self.root, "20261101-100000")
        sync.sync(self.root, now=NOW)
        edited = NOW - 600
        os.utime(session / "meta.json", (edited, edited))
        result = sync.sync(self.root, now=NOW)
        self.assertEqual(result.pushed, ["20261101-100000"])

    def test_waits_for_a_meeting_written_a_moment_ago(self):
        make_session(self.root, "20261101-100000", age_s=sync.QUIET_S - 1)
        result = sync.sync(self.root, now=NOW)
        self.assertEqual(result.busy, ["20261101-100000"])
        self.run_.assert_not_called()

    def test_waits_for_the_meeting_being_recorded(self):
        session = make_session(self.root, "20261101-100000")
        with mock.patch.object(sync, "read_state",
                               return_value={"status": "recording"}):
            result = sync.sync(self.root, now=NOW)
        self.assertEqual(result.busy, [session.name])
        self.run_.assert_not_called()

    def test_a_failed_push_is_retried_next_time(self):
        make_session(self.root, "20261101-100000")
        self.run_.return_value = done(1, "couldn't connect\n")
        result = sync.sync(self.root, now=NOW)
        self.assertEqual(result.failed,
                         {"20261101-100000": "couldn't connect"})
        self.run_.return_value = done()
        self.assertEqual(sync.sync(self.root, now=NOW).pushed,
                         ["20261101-100000"])

    def test_dry_run_sends_and_records_nothing(self):
        make_session(self.root, "20261101-100000")
        result = sync.sync(self.root, dry_run=True, now=NOW)
        self.assertEqual(result.pushed, ["20261101-100000"])
        self.run_.assert_not_called()
        self.assertEqual(sync.load_state(), {})

    def test_missing_rclone_is_said_not_raised(self):
        make_session(self.root, "20261101-100000")
        with mock.patch.object(sync.shutil, "which", return_value=None):
            result = sync.sync(self.root, now=NOW)
        self.assertIn("rclone", result.failed)

    def test_state_survives_a_broken_file(self):
        sync.state_dir().mkdir(parents=True)
        (sync.state_dir() / sync.STATE_FILENAME).write_text("{")
        self.assertEqual(sync.load_state(), {})

    def test_state_records_the_pushed_time(self):
        make_session(self.root, "20261101-100000")
        sync.sync(self.root, now=NOW)
        saved = json.loads((sync.state_dir() / sync.STATE_FILENAME).read_text())
        self.assertEqual(saved["pushed"], {"20261101-100000": NOW - 3600})


if __name__ == "__main__":
    unittest.main()
