"""Steno's thin side of the asst handshake (issue 11)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steno import asstlink, notes
from steno.notes import save_todos, set_done, sync_todos


class TestSourceKey(unittest.TestCase):
    def test_stable_per_session_and_text(self):
        d = Path("/tmp/20260927-100000")
        a = asstlink.source_key(d, "send the report")
        b = asstlink.source_key(d, "send the report")
        self.assertEqual(a, b)
        self.assertEqual(a, "steno:20260927-100000/send the report")

    def test_different_sessions_differ(self):
        a = asstlink.source_key(Path("/tmp/a"), "x")
        b = asstlink.source_key(Path("/tmp/b"), "x")
        self.assertNotEqual(a, b)

    def test_prefix_is_the_session_folder(self):
        self.assertEqual(
            asstlink.source_prefix(Path("/tmp/20260927-100000")),
            "steno:20260927-100000/",
        )


class TestParse(unittest.TestCase):
    def test_add_returns_the_short_id(self):
        raw = json.dumps({"id": "0F27", "href": "h", "list": "l", "existed": False,
                          "uid": "0F276A13-FBF3", "summary": "Send", "status": "needs-action"})
        self.assertEqual(asstlink._parse_id(raw), "0F27")

    def test_add_garbage(self):
        self.assertIsNone(asstlink._parse_id(None))
        self.assertIsNone(asstlink._parse_id(""))
        self.assertIsNone(asstlink._parse_id("not json"))

    def test_completed_from_status(self):
        raw = json.dumps({"id": "0F27", "status": "completed"})
        self.assertTrue(asstlink._parse_completed(raw))

    def test_completed_from_timestamp(self):
        raw = json.dumps({"id": "0F27", "completed": "2026-09-27T10:00:00Z"})
        self.assertTrue(asstlink._parse_completed(raw))

    def test_open(self):
        raw = json.dumps({"id": "0F27", "status": "needs-action", "completed": None})
        self.assertFalse(asstlink._parse_completed(raw))

    def test_completed_garbage(self):
        self.assertIsNone(asstlink._parse_completed(None))
        self.assertIsNone(asstlink._parse_completed("[]"))


class TestRun(unittest.TestCase):
    def test_missing_asst_is_silent(self):
        with mock.patch.object(asstlink, "available", lambda: False):
            self.assertIsNone(asstlink._run(["add", "x"]))
            self.assertIsNone(asstlink.add_todo(Path("/tmp/s"), "Send", "send"))
            self.assertFalse(asstlink.set_done("0F27", True))
            self.assertIsNone(asstlink.completed("0F27"))

    def test_scratch_dir_never_files(self):
        """A temp session is not a meeting. asst on PATH must not be enough.

        Gotcha #13 filed fixture to-dos from `mkdtemp` dirs; the write is
        gated on `meta.json`, not on the test runner importing our package.
        """
        scratch = Path(tempfile.mkdtemp())
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink.subprocess, "run") as run:
            self.assertIsNone(asstlink.add_todo(scratch, "Send", "send"))
        run.assert_not_called()

    def test_json_flag_leads(self):
        """`--json` after `--` is title text (clap), so it must come first.

        A trailing `--json` turned every filed item into
        "Send the figures by Friday --json" and left the caller without an id.
        """
        session = Path(tempfile.mkdtemp())
        (session / "meta.json").write_text("{}\n")
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout='{"id":"0F27"}', stderr="")
            uid = asstlink.add_todo(session, "Send the figures", "send the figures")
        self.assertEqual(uid, "0F27")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:2], ["asst", "--json"])
        self.assertIn("--", cmd)
        self.assertLess(cmd.index("--json"), cmd.index("--"))
        self.assertEqual(cmd[-1], "Send the figures")

    def test_list_name_is_passed_only_when_asked(self):
        session = Path(tempfile.mkdtemp())
        (session / "meta.json").write_text("{}\n")
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout='{"id":"0F27"}', stderr="")
            asstlink.add_todo(session, "Send", "send")
            asstlink.add_todo(session, "Send", "send", "thesis")
        first, second = (c[0][0] for c in run.call_args_list)
        self.assertNotIn("--list", first)
        self.assertIn("--list", second)
        self.assertEqual(second[second.index("--list") + 1], "thesis")


class TestTargetList(unittest.TestCase):
    def setUp(self):
        self.session = Path(tempfile.mkdtemp())
        (self.session / "meta.json").write_text(
            json.dumps({"project": "thesis"}) + "\n"
        )

    def test_exact_list(self):
        with mock.patch.object(asstlink, "list_names", lambda: ["thesis", "steno"]):
            self.assertEqual(asstlink.target_list(self.session), "thesis")

    def test_unambiguous_prefix(self):
        with mock.patch.object(asstlink, "list_names", lambda: ["thesis-notes", "steno"]):
            self.assertEqual(asstlink.target_list(self.session), "thesis-notes")

    def test_unknown_project_falls_back_to_inbox(self):
        with mock.patch.object(asstlink, "list_names", lambda: ["steno", "dotfiles"]):
            self.assertIsNone(asstlink.target_list(self.session))

    def test_no_project_is_inbox(self):
        (self.session / "meta.json").write_text("{}\n")
        with mock.patch.object(asstlink, "list_names", lambda: ["thesis"]):
            self.assertIsNone(asstlink.target_list(self.session))


class TestPull(unittest.TestCase):
    def setUp(self):
        # fetch_session only uses the session folder name in the source prefix.
        self.session = Path("/tmp/20261002-163144")

    def _fetch(self, open_rows, done_rows=None, failed=False):
        def _run(args):
            if failed:
                return None
            if args[0] == "ls":
                return json.dumps(open_rows)
            return json.dumps(done_rows or [])

        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "_run", side_effect=_run):
            return asstlink.fetch_session(self.session)

    def test_indexes_by_source_and_marks_completed(self):
        open_rows = [{
            "id": "b0bd", "status": "needs-action",
            "list_name": "Inbox",
            "source": "steno:20261002-163144/send the report",
        }]
        done_rows = [{
            "id": "21e6", "status": "completed",
            "list_name": "thesis",
            "completed": "2026-10-08T05:05:47Z",
            "source": "steno:20261002-163144/think about it",
        }]
        out = self._fetch(open_rows, done_rows)
        self.assertEqual(out["steno:20261002-163144/send the report"]["completed"], False)
        self.assertEqual(out["steno:20261002-163144/think about it"]["completed"], True)
        self.assertEqual(out["steno:20261002-163144/think about it"]["id"], "21e6")
        self.assertEqual(out["steno:20261002-163144/send the report"]["list"], "Inbox")

    def test_other_sessions_are_ignored(self):
        out = self._fetch([{"id": "x", "status": "needs-action",
                            "source": "steno:other/send"}])
        self.assertEqual(out, {})

    def test_failure_is_none_not_empty(self):
        """A dead asst must not look like 'every task was deleted'."""
        self.assertIsNone(self._fetch([], failed=True))


class TestMove(unittest.TestCase):
    def test_moves_only_filed_uids(self):
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "_run") as run:
            run.return_value = "{}"
            moved = asstlink.move_todos(["0F27", "", "ABCD"], "thesis")
        self.assertEqual(moved, 2)
        cmds = [c[0][0] for c in run.call_args_list]
        self.assertEqual([c[1:3] for c in cmds],
                         [["0F27", "--list"], ["ABCD", "--list"]])
        self.assertTrue(all(c[-1] == "thesis" for c in cmds))


class TestRm(unittest.TestCase):
    def test_removes_only_filed_uids(self):
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "_run") as run:
            run.return_value = "{}"
            removed = asstlink.rm_todos(["0F27", "", "ABCD"])
        self.assertEqual(removed, 2)
        self.assertEqual([c[0][0] for c in run.call_args_list],
                         [["rm", "0F27"], ["rm", "ABCD"]])


class TestSuiteIsolation(unittest.TestCase):
    def test_asst_is_off_by_default(self):
        """tests/__init__ keeps the suite out of the user's real inbox."""
        self.assertFalse(asstlink.available())

    def test_sync_todos_never_shells_out(self):


        summary = "## Action items\n**You**\n- Send the usage figures by Friday\n"
        with mock.patch.object(asstlink.subprocess, "run") as run:
            sync_todos(Path(tempfile.mkdtemp()), summary)
        run.assert_not_called()


class TestFileWithAsst(unittest.TestCase):
    """Issue 11: your items go to asst; theirs stay local; uid sticks."""

    def setUp(self):


        self.notes = notes
        self.dir = Path(tempfile.mkdtemp())

    def test_only_your_items_are_filed(self):
        todos = [
            self.notes.Todo(text="Send the report", owner="you"),
            self.notes.Todo(text="They will reply", owner="them"),
        ]
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "target_list", return_value="thesis"), \
             mock.patch.object(asstlink, "add_todo", side_effect=["0F27", "BAD"]) as add:
            out = self.notes.file_with_asst(self.dir, todos)
        self.assertEqual(out[0].uid, "0F27")
        self.assertEqual(out[1].uid, "")
        self.assertEqual(add.call_count, 1)
        self.assertEqual(add.call_args[0][3], "thesis")

    def test_already_filed_is_skipped(self):
        todos = [self.notes.Todo(text="Send", owner="you", uid="ABCD")]
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "add_todo") as add:
            self.notes.file_with_asst(self.dir, todos)
        add.assert_not_called()

    def test_without_asst_nothing_happens(self):
        todos = [self.notes.Todo(text="Send", owner="you")]
        with mock.patch.object(asstlink, "available", lambda: False), \
             mock.patch.object(asstlink, "add_todo") as add:
            out = self.notes.file_with_asst(self.dir, todos)
        self.assertEqual(out[0].uid, "")
        add.assert_not_called()

    def test_checkbox_mirrors_to_asst(self):

        todos = [self.notes.Todo(text="Send", owner="you", uid="0F27")]
        save_todos(self.dir, todos)
        with mock.patch.object(asstlink, "set_done", return_value=True) as mirror:
            set_done(self.dir, "send", True)
        mirror.assert_called_once_with("0F27", True)

    def test_uid_survives_resummarize(self):

        save_todos(self.dir, [self.notes.Todo(text="Send the report", owner="you",
                                              uid="0F27", done=True)])
        summary = "## Action items\n**You**\n- Send the report\n"
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "add_todo") as add:
            merged = sync_todos(self.dir, summary)
        add.assert_not_called()
        self.assertEqual(merged[0].uid, "0F27")
        self.assertTrue(merged[0].done)
