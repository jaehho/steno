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

    def test_json_flag_leads(self):
        """`--json` after `--` is title text (clap), so it must come first.

        A trailing `--json` turned every filed item into
        "Send the figures by Friday --json" and left the caller without an id.
        """
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout='{"id":"0F27"}', stderr="")
            uid = asstlink.add_todo(Path("/tmp/s"), "Send the figures", "send the figures")
        self.assertEqual(uid, "0F27")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:2], ["asst", "--json"])
        self.assertIn("--", cmd)
        self.assertLess(cmd.index("--json"), cmd.index("--"))
        self.assertEqual(cmd[-1], "Send the figures")


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
             mock.patch.object(asstlink, "add_todo", side_effect=["0F27", "BAD"]) as add:
            out = self.notes.file_with_asst(self.dir, todos)
        self.assertEqual(out[0].uid, "0F27")
        self.assertEqual(out[1].uid, "")
        self.assertEqual(add.call_count, 1)

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
