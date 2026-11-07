"""`steno erase`: a deleted meeting gone from every copy.

Deleting moves, never removes (#39): the meeting goes to this device's
trash, the server's copy to `.trash/`, and every backup taken while it
was here keeps it. For someone who said no, or took back a yes, erase
removes each of those. Everything is real but the server: a plain
folder rclone treats as one, or, for Nextcloud's trash bin, rclone's own
WebDAV server laid out as Nextcloud's addresses are (TestTrashBin); and
the backup, a throwaway restic repository in a temporary folder.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from steno import cli, erase, sync
from steno.retention import delete_session

ROOM = "20261112-100000"
OTHER = "20261020-101400"
ROOM_PAGE = "2026-11-12-1000-weekly-sync.md"
OTHER_PAGE = "2026-10-20-1014-vendor-call.md"
RECORDED = datetime(2026, 11, 12, 12, 0).timestamp()
DELETED = RECORDED + 86400


def meeting(root: Path, name: str, audio: bytes) -> Path:
    """A finished, settled meeting with its own audio."""
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "you.wav").write_bytes(audio)
    (folder / "transcript.jsonl").write_text(
        json.dumps({"ts": RECORDED, "speaker": "them", "text": "hi"}) + "\n")
    (folder / "meta.json").write_text(json.dumps({"title": name}))
    for path in folder.iterdir():
        os.utime(path, (RECORDED - 3600, RECORDED - 3600))
    return folder


@unittest.skipUnless(shutil.which("rclone"), "needs rclone")
class Copies(unittest.TestCase):
    """A meeting synced, backed up, deleted, then synced again: one copy
    in the trash, one in the server's `.trash/`, one in a snapshot."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "data" / "steno" / "sessions"
        self.server = self.tmp / "server"
        self.server.mkdir()
        self.env = {
            sync.REMOTE_ENV: str(self.server),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_DATA_HOME": str(self.tmp / "data"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "RESTIC_REPOSITORY": str(self.tmp / "repo"),
            "RESTIC_PASSWORD": "throwaway-test-only",
            "RESTIC_CACHE_DIR": str(self.tmp / "cache"),
            "STENO_PROJECTS_DIR": str(self.tmp / "projects"),
            "STENO_OBSIDIAN_DIR": str(self.tmp / "vault"),
        }
        for patcher in (
            mock.patch.dict(os.environ, self.env),
            mock.patch.object(sync, "read_state", return_value={}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.room_audio = os.urandom(200_000)
        self.room = meeting(self.root, ROOM, self.room_audio)
        meeting(self.root, OTHER, os.urandom(200_000))
        self.assertEqual(sync.sync(self.root, now=RECORDED).status, "ok")

    def deleted_and_synced(self) -> None:
        self.assertEqual(delete_session(self.room)[0], "trashed")
        result = sync.sync(self.root, now=DELETED)
        self.assertEqual(result.trashed_remote, [ROOM])

    def steno(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", ["steno", *argv]), \
             contextlib.redirect_stdout(out), \
             contextlib.redirect_stderr(out):
            try:
                cli.main()
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue()

    def on_server(self) -> list[str]:
        return sorted(p.relative_to(self.server).as_posix()
                      for p in self.server.rglob("*") if p.is_file())

    def in_trash(self) -> list[str]:
        trash = self.tmp / "data" / "Trash"
        return sorted(p.relative_to(trash).as_posix()
                      for p in trash.rglob("*") if p.is_file())


class TestErase(Copies):
    def test_it_finds_every_copy_and_a_dry_run_removes_none(self):
        self.deleted_and_synced()
        before = (self.on_server(), self.in_trash())
        code, said = self.steno("erase", ROOM, "--dry-run")
        self.assertEqual(code, 0, said)
        self.assertIn(
            f"in the trash:          {self.tmp}/data/Trash/files/{ROOM}", said)
        self.assertIn(
            f"on the server:         {self.server}/.trash/{ROOM}-", said)
        self.assertIn("steno knows only Nextcloud's trash bin", said)
        self.assertIn("add --restic", said)
        self.assertEqual((self.on_server(), self.in_trash()), before)

    def test_it_removes_the_trashed_copies_and_nothing_else(self):
        self.deleted_and_synced()
        code, said = self.steno("erase", ROOM, "--yes")
        self.assertEqual(code, 0, said)
        self.assertEqual([f for f in self.on_server() if ROOM in f], [])
        self.assertEqual(self.in_trash(), [])
        self.assertIn(f"{OTHER}/you.wav", self.on_server())
        self.assertTrue((self.root / OTHER / "you.wav").is_file())
        self.assertNotIn(ROOM, sync.load_base())
        again = sync.sync(self.root, now=DELETED + 600)
        self.assertEqual((again.status, again.pushed, again.pulled),
                         ("ok", [], []))

    def test_erased_before_the_next_sync_the_live_server_copy_goes(self):
        self.assertEqual(delete_session(self.room)[0], "trashed")
        code, said = self.steno("erase", ROOM, "--yes")
        self.assertEqual(code, 0, said)
        self.assertEqual([f for f in self.on_server() if ROOM in f], [])
        again = sync.sync(self.root, now=DELETED)
        self.assertEqual((again.trashed_remote, again.trashed_local,
                          again.pulled), ([], [], []))

    def test_a_meeting_still_in_the_list_is_refused(self):
        code, said = self.steno("erase", ROOM, "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Delete it first", said)
        self.assertTrue(self.room.is_dir())
        self.assertIn(f"{ROOM}/you.wav", self.on_server())


def page(folder: Path, name: str, session: str) -> Path:
    """A project page steno wrote for `session`, as projectnotes does."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(
        f"---\ntitle: x\nsession: {session}\ngenerated_by: steno\n---\n"
        f"<!-- Written by steno from session {session}. -->\n\nwords\n")
    return path


@unittest.skipUnless(shutil.which("restic"), "needs restic")
class TestEraseFromBackups(Copies):
    def restic(self, *args: str) -> str:
        done = subprocess.run(["restic", "-q", *args], capture_output=True,
                              text=True, check=True)
        return done.stdout

    def blobs_of(self, path: Path) -> list[str]:
        """The data blobs a file was stored as in the only snapshot."""
        (snapshot,) = json.loads(self.restic("snapshots", "--json"))
        tree = json.loads(
            self.restic("cat", "tree", f"{snapshot['id']}:{path.parent}"))
        (node,) = (n for n in tree["nodes"] if n["name"] == path.name)
        return node["content"]

    def blobs_left(self) -> set[str]:
        """Every blob the repository still stores, used or not."""
        return {line.split()[-1]
                for line in self.restic("list", "blobs").splitlines()}

    def backed_up_with_pages(self) -> Path:
        """One snapshot of the sessions and the projects, where each
        meeting has a summary page and the room an old transcript page."""
        meetings = self.tmp / "projects" / "team" / "notes" / "meetings"
        page(meetings, ROOM_PAGE, ROOM)
        page(meetings, ROOM_PAGE.replace(".md", ".transcript.md"), ROOM)
        page(meetings, OTHER_PAGE, OTHER)
        self.restic("init")
        self.restic("backup", str(self.root), str(self.tmp / "projects"))
        return meetings

    def test_the_snapshots_and_the_data_behind_them_go(self):
        meetings = self.backed_up_with_pages()
        blobs = self.blobs_of(self.root / ROOM / "you.wav")
        blobs += self.blobs_of(meetings / ROOM_PAGE)
        self.assertLessEqual(set(blobs), self.blobs_left())
        self.deleted_and_synced()
        for name in (ROOM_PAGE, ROOM_PAGE.replace(".md", ".transcript.md")):
            (meetings / name).unlink()  # delete takes the pages out

        code, said = self.steno("erase", ROOM, "--yes", "--restic")
        self.assertEqual(code, 0, said)
        listed = self.restic("ls", "latest")
        self.assertNotIn(ROOM, listed)
        self.assertNotIn("2026-11-12-1000", listed)
        self.assertIn(f"{OTHER}/you.wav", listed)
        self.assertIn(OTHER_PAGE, listed)
        self.assertEqual(
            len(json.loads(self.restic("snapshots", "--json"))), 1)
        self.assertEqual(set(blobs) & self.blobs_left(), set())

    def test_a_dry_run_says_what_the_backups_hold_and_changes_nothing(self):
        self.backed_up_with_pages()
        self.deleted_and_synced()
        before = self.restic("list", "blobs")
        code, said = self.steno("erase", ROOM, "--dry-run", "--restic")
        self.assertEqual(code, 0, said)
        self.assertIn("in 1 backup snapshots:", said)
        self.assertIn(f"/{ROOM}/you.wav", said)
        self.assertIn(f"/notes/meetings/{ROOM_PAGE}", said)
        self.assertNotIn(OTHER_PAGE, said)
        self.assertIn("taking it out repacks at least", said)
        self.assertEqual(self.restic("list", "blobs"), before)

    def test_without_a_repository_in_the_shell_it_reads_the_backup_unit(self):
        os.environ.pop("RESTIC_REPOSITORY")
        os.environ.pop("RESTIC_PASSWORD")
        env_file = self.tmp / "rest.env"
        env_file.write_text(f"RESTIC_REPOSITORY={self.tmp / 'repo'}\n")
        password = self.tmp / "password"
        password.write_text("throwaway-test-only\n")
        shown = (f"Environment=RESTIC_PASSWORD_FILE={password}\n"
                 f"EnvironmentFiles={env_file} (ignore_errors=no)\n")
        real = subprocess.run

        def systemctl(command, *args, **kwargs):
            if command[0] == "systemctl":
                self.assertEqual(command[3], "backup.service")
                return subprocess.CompletedProcess(command, 0, shown, "")
            return real(command, *args, **kwargs)

        with mock.patch.dict(os.environ,
                             {erase.RESTIC_UNIT_ENV: "backup.service"}), \
                mock.patch.object(erase.subprocess, "run", systemctl):
            env = erase.restic_environment(os.environ)
        self.assertEqual(env["RESTIC_REPOSITORY"], str(self.tmp / "repo"))
        self.assertEqual(env["RESTIC_PASSWORD_FILE"], str(password))
        with mock.patch.dict(os.environ, env):
            self.restic("init")  # the unit's settings reach a repository

    def test_with_no_repository_anywhere_it_says_so_and_removes_nothing(self):
        os.environ.pop("RESTIC_REPOSITORY")
        self.deleted_and_synced()
        with mock.patch.object(erase, "restic_environment", return_value={}):
            code, said = self.steno("erase", ROOM, "--yes", "--restic")
        self.assertEqual(code, 1)
        self.assertIn("no restic repository", said)
        self.assertTrue(self.in_trash())


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@unittest.skipUnless(shutil.which("rclone"), "needs rclone")
class TestTrashBin(unittest.TestCase):
    """A Nextcloud keeps what a WebDAV DELETE removes in its trash bin,
    at `remote.php/dav/trashbin/<user>/trash/<name>.d<time>`.

    The server here is rclone's own WebDAV server over a folder laid out
    as Nextcloud's addresses are, and the remote is set up as the
    owner's is (webdav, vendor nextcloud, url ending in
    /dav/files/<user>/). Moving what `rclone purge` removes into the
    bin, as Nextcloud does, is done by the test: rclone's server deletes
    outright.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        dav = self.tmp / "web" / "remote.php" / "dav"
        self.files = dav / "files" / "u" / "steno"
        self.bin = dav / "trashbin" / "u" / "trash"
        self.bin.mkdir(parents=True)
        (self.files / sync.TRASH_DIR / f"{ROOM}-20261113-120000").mkdir(
            parents=True)
        (self.files / sync.TRASH_DIR / f"{ROOM}-20261113-120000" /
         "you.wav").write_bytes(b"room")
        (self.files / OTHER).mkdir()
        (self.files / OTHER / "you.wav").write_bytes(b"other")
        # Already in the bin: the room's folder from an earlier erase
        # that stopped part way, its pages the vault's sync deleted, and
        # a page of another meeting begun in the same minute.
        (self.bin / f"{ROOM}.d1762966800").mkdir()
        (self.bin / f"{ROOM}.d1762966800" / "you.wav").write_bytes(b"x")
        page(self.bin, f"{ROOM_PAGE}.d1762966900", ROOM)
        page(self.bin, ROOM_PAGE.replace(".md", ".transcript.md")
             + ".d1762966900", ROOM)
        page(self.bin, "2026-11-12-1000-standup.md.d1762966900",
             "20261112-100030")
        page(self.bin, f"{OTHER_PAGE}.d1762966900", OTHER)
        (self.bin / "notes.md.d1762966900").write_text("not steno's\n")

        self.port = port = free_port()
        config = self.tmp / "rclone.conf"
        config.write_text(
            "[nc]\ntype = webdav\n"
            f"url = http://127.0.0.1:{port}/remote.php/dav/files/u/\n"
            "vendor = nextcloud\n")
        server = subprocess.Popen(
            ["rclone", "serve", "webdav", "--addr", f"127.0.0.1:{port}",
             "--dir-cache-time", "0s", str(self.tmp / "web")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(server.wait)
        self.addCleanup(server.terminate)
        for _ in range(100):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        env = {
            "RCLONE_CONFIG": str(config),
            sync.REMOTE_ENV: "nc:steno",
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_DATA_HOME": str(self.tmp / "data"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "STENO_PROJECTS_DIR": str(self.tmp / "projects"),
            "STENO_OBSIDIAN_DIR": str(self.tmp / "vault"),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def in_bin(self) -> list[str]:
        return sorted(p.name for p in self.bin.iterdir())

    def erase(self, *argv: str) -> tuple[int, str]:
        return Copies.steno(self, "erase", ROOM, *argv)

    def nextcloud_purge(self, real):
        """`rclone purge` as Nextcloud takes it: into the trash bin."""
        def run(args, *flags):
            if args[0] == "purge" and args[1].startswith("nc:steno/"):
                where = self.files / args[1].removeprefix("nc:steno/")
                where.rename(self.bin / f"{where.name}.d1763053200")
                return ""
            return real(args, *flags)
        return run

    def test_the_bin_address_is_worked_out_from_the_remote(self):
        self.assertEqual(
            erase.server_bin("nc:steno"),
            f"nc,vendor=other,url='http://127.0.0.1:{self.port}"
            "/remote.php/dav/trashbin/u/trash/':")
        self.assertEqual(erase.server_bin(str(self.tmp / "folder")), "")

    def test_a_dry_run_lists_the_meeting_in_the_bin_and_removes_nothing(self):
        before = self.in_bin()
        code, said = self.erase("--dry-run")
        self.assertEqual(code, 0, said)
        self.assertIn(f"in its trash bin:      {ROOM}.d1762966800", said)
        self.assertIn(f"in its trash bin:      {ROOM_PAGE}.d1762966900", said)
        self.assertNotIn("standup", said)  # another meeting of that minute
        self.assertIn("what the server copies leave in its trash bin", said)
        self.assertEqual(self.in_bin(), before)

    def test_what_the_server_copies_leave_in_the_bin_goes_too(self):
        real = sync._rclone
        with mock.patch.object(sync, "_rclone", self.nextcloud_purge(real)):
            code, said = self.erase("--yes")
        self.assertEqual(code, 0, said)
        self.assertEqual(self.in_bin(), sorted([
            "2026-11-12-1000-standup.md.d1762966900",
            f"{OTHER_PAGE}.d1762966900",
            "notes.md.d1762966900",
        ]))
        self.assertFalse((self.files / sync.TRASH_DIR).exists()
                         and any((self.files / sync.TRASH_DIR).iterdir()))
        self.assertTrue((self.files / OTHER / "you.wav").is_file())


class TestNames(unittest.TestCase):
    def test_page_stamp_is_the_minute_the_meeting_began(self):
        self.assertEqual(erase.page_stamp(ROOM), "2026-11-12-1000")
        self.assertIsNone(erase.page_stamp("notes"))

    def test_backup_patterns_cover_the_folder_and_its_pages(self):
        self.assertEqual(
            erase.backup_patterns(ROOM, sessions=Path("/s"),
                                  projects=Path("/p"), vault=Path("/v")),
            ["/s/20261112-100000",
             "/p/*/notes/meetings/2026-11-12-1000*",
             "/v/*/meetings/2026-11-12-1000*"])

    def test_bin_folders_are_the_live_and_trashed_folder_only(self):
        entries = [f"{ROOM}.d1/", f"{ROOM}-20261113-120000.d22/",
                   f"{ROOM}.d1", f"{ROOM}x.d1/", f"{OTHER}.d1/", "you.wav.d1"]
        self.assertEqual(erase.bin_folders(entries, ROOM),
                         [f"{ROOM}.d1", f"{ROOM}-20261113-120000.d22"])

    def test_bin_pages_are_that_minute_s_pages(self):
        entries = ["2026-11-12-1000-a.md.d5", "2026-11-12-1000.md.d5",
                   "2026-11-12-100000-a.transcript.md.d5",
                   "2026-11-12-1001-a.md.d5", "2026-11-12-1000-a.md",
                   "2026-11-12-1000-dir.d5/"]
        self.assertEqual(erase.bin_pages(entries, "2026-11-12-1000"),
                         entries[:3])

    def test_trash_bin_url_for_each_nextcloud_address(self):
        base = {"type": "webdav", "vendor": "nextcloud"}
        self.assertEqual(
            erase.trash_bin_url({**base, "url": "https://h/remote.php/dav/files/ana/"}),
            "https://h/remote.php/dav/trashbin/ana/trash/")
        self.assertEqual(
            erase.trash_bin_url({**base, "url": "https://h/nc/remote.php/webdav",
                                 "user": "ana b"}),
            "https://h/nc/remote.php/dav/trashbin/ana%20b/trash/")
        self.assertIsNone(erase.trash_bin_url(
            {**base, "url": "https://h/remote.php/webdav/"}))
        self.assertIsNone(erase.trash_bin_url(
            {"type": "webdav", "vendor": "other",
             "url": "https://h/remote.php/dav/files/ana/"}))

    def test_remote_name(self):
        self.assertEqual(erase.remote_name("nextcloud:steno"), "nextcloud")
        self.assertEqual(erase.remote_name("nc,vendor=x:steno"), "nc")
        self.assertIsNone(erase.remote_name("/home/me/server"))
        self.assertIsNone(erase.remote_name(":webdav:steno"))

    def test_unit_environment_files_win_as_in_systemd(self):
        shown = ('Environment=RESTIC_PASSWORD_FILE=/p "A=b c"\n'
                 "EnvironmentFiles=/e (ignore_errors=no)\n"
                 "EnvironmentFiles=-/missing (ignore_errors=yes)\n")
        self.assertEqual(erase.unit_files(shown), ["/e", "/missing"])
        self.assertEqual(
            erase.unit_environment(shown, {"/e": {"RESTIC_REPOSITORY": "r",
                                                  "A": "file"}}),
            {"RESTIC_PASSWORD_FILE": "/p", "A": "file",
             "RESTIC_REPOSITORY": "r"})

    def test_backup_holds_reads_find_and_prune(self):
        found = json.dumps([
            {"snapshot": "a", "matches": [{"path": "/s/x"}, {"path": "/s/y"}]},
            {"snapshot": "b", "matches": [{"path": "/s/x"}]},
            {"snapshot": "c", "matches": None},
        ])
        prune = ("to repack:         43247 blobs / 34.876 MiB\n"
                 "this removes:        142 blobs / 1.044 MiB\n")
        self.assertEqual(erase.backup_holds(found, prune),
                         erase.BackupHolds(["/s/x", "/s/y"], 2,
                                           "43247 blobs / 34.876 MiB"))


if __name__ == "__main__":
    unittest.main()
