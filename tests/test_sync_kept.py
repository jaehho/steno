"""A meeting kept on this laptop stays off the sync server.

Room mode keeps a room recording's microphone on the laptop until the
user says everyone agreed (`mic_pending`), and the window says nothing
has left the laptop. Sync must agree: a kept meeting is not uploaded,
audio or text, until the mic pass has run.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from steno import sync

NOW = datetime(2026, 11, 12, 12, 0).timestamp()
ROOM = "20261112-100000"


@unittest.skipUnless(shutil.which("rclone"), "needs rclone")
class KeptMeeting(unittest.TestCase):
    """One device and a plain folder standing in for the server."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.server = self.tmp / "server"
        self.server.mkdir()
        self.root = self.tmp / "a" / "sessions"
        self.root.mkdir(parents=True)
        (self.tmp / "a" / "state").mkdir()
        for patcher in (
            mock.patch.object(sync, "read_state", return_value={}),
            mock.patch.dict(os.environ, {
                sync.REMOTE_ENV: str(self.server),
                "XDG_DATA_HOME": str(self.tmp / "data"),
                "XDG_STATE_HOME": str(self.tmp / "a" / "state"),
            }),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def room(self, **meta) -> Path:
        """A room recording that ended an hour ago, quiet since."""
        session = self.root / ROOM
        session.mkdir()
        files = {
            "you.wav": b"room audio",
            "meta.json": json.dumps(meta).encode(),
        }
        for name, content in files.items():
            path = session / name
            path.write_bytes(content)
            os.utime(path, (NOW - 3600, NOW - 3600))
        return session

    def on_server(self) -> set[str]:
        return {
            p.relative_to(self.server).as_posix()
            for p in self.server.rglob("*") if p.is_file()
        }

    def test_a_kept_room_meeting_stays_off_the_server(self):
        self.room(manual=True, room=True, mic_pending=True)
        result = sync.sync(self.root, now=NOW)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.pushed, [])
        self.assertEqual(
            {f for f in self.on_server() if f.startswith(ROOM)}, set())

    def test_it_goes_up_once_everyone_agreed(self):
        session = self.room(manual=True, room=True, mic_pending=True)
        sync.sync(self.root, now=NOW)
        meta = session / "meta.json"
        meta.write_text(json.dumps({"manual": True, "room": True}))
        os.utime(meta, (NOW - 3600, NOW - 3600))
        result = sync.sync(self.root, now=NOW)
        self.assertEqual(len(result.pushed), 2)
        self.assertIn(f"{ROOM}/you.wav", self.on_server())

    def test_a_call_whose_mic_pass_has_not_run_goes_up(self):
        """Only a room waits: a call's mic pass that failed is no reason
        to keep it off the server."""
        self.room(manual=False, mic_pending=True)
        result = sync.sync(self.root, now=NOW)
        self.assertEqual(result.busy, [])
        self.assertIn(f"{ROOM}/you.wav", self.on_server())


if __name__ == "__main__":
    unittest.main()
