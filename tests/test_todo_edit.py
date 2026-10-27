"""Rewording, removing, adding and moving a to-do by hand (issue #22).

The local half is a write to todos.json and is checked there; the asst
half runs against tests/fakeasst.py, so a call is a subprocess with the
exit status and silence the real one has. The real asst is never on the
PATH of these tests.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from steno import asstlink
from steno.notes import (
    add_todo_row,
    deliver_remove,
    deliver_title,
    file_with_asst,
    load_todos,
    move_todo,
    pull_todos,
    remove_todo,
    rename_todo,
    sync_todos,
)
from tests.fakeasst import FakeAsst

SUMMARY = """# Lab sync

## Action items
**You**
- Send the usage figures by Friday
- Draft the comparison table

**Them**
- Revised quote next week
"""
SEND = "send the usage figures by friday"
THEIRS = "revised quote next week"


class EditCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "20260801-090000"
        self.dir.mkdir()
        (self.dir / "meta.json").write_text(json.dumps({"project": "lab"}))
        self.asst = FakeAsst().start()
        self.addCleanup(self.asst.stop)
        sync_todos(self.dir, SUMMARY)

    def row(self, key: str):
        return next(t for t in load_todos(self.dir) if t.key == key)


class TestRewording(EditCase):
    def test_the_row_takes_the_new_words_and_keeps_its_task(self):
        before = self.row(SEND)
        done = rename_todo(self.dir, SEND, "Send the Q3 usage figures")
        self.assertIsNotNone(done)
        after = next(t for t in load_todos(self.dir) if "Q3" in t.text)
        self.assertEqual(after.text, "Send the Q3 usage figures")
        self.assertEqual((after.uid, after.src), (before.uid, before.src))
        self.assertEqual(len(load_todos(self.dir)), 3)

    def test_asst_is_told_the_new_title_of_that_task(self):
        uid = self.row(SEND).uid
        rename_todo(self.dir, SEND, "Send the Q3 usage figures")
        self.assertTrue(deliver_title(uid, "Send the Q3 usage figures"))
        (task,) = [t for t in self.asst.tasks() if t["id"] == uid]
        self.assertEqual(task["title"], "Send the Q3 usage figures")

    def test_a_title_that_starts_with_a_dash_is_text_not_an_option(self):
        uid = self.row(SEND).uid
        self.assertTrue(deliver_title(uid, "--list nonsense"))
        (task,) = [t for t in self.asst.tasks() if t["id"] == uid]
        self.assertEqual(task["title"], "--list nonsense")
        self.assertEqual(task["list_name"], "lab")

    def test_a_pull_still_finds_the_reworded_task(self):
        rename_todo(self.dir, SEND, "Send the Q3 usage figures")
        self.asst.complete(self.row("send the q3 usage figures").uid)
        pull_todos(self.dir)
        reworded = next(t for t in load_todos(self.dir) if "Q3" in t.text)
        self.assertTrue(reworded.done)
        self.assertEqual(len(load_todos(self.dir)), 3)

    def test_empty_wording_or_wording_another_row_has_is_refused(self):
        self.assertIsNone(rename_todo(self.dir, SEND, "   "))
        self.assertIsNone(
            rename_todo(self.dir, SEND, "draft the  Comparison table")
        )
        self.assertEqual(self.row(SEND).text, "Send the usage figures by Friday")

    def test_a_row_that_is_gone_is_not_made_up(self):
        self.assertIsNone(rename_todo(self.dir, "no such row", "anything"))

    def test_an_unfiled_row_has_nothing_to_tell_asst(self):
        self.assertTrue(deliver_title("", "anything"))
        self.assertEqual(self.asst.calls("edit"), [])

    def test_a_refusal_by_asst_is_said(self):
        self.asst.down = True
        self.assertFalse(deliver_title(self.row(SEND).uid, "x"))


class TestRemoving(EditCase):
    def test_the_row_goes_and_so_does_its_task(self):
        uid = self.row(SEND).uid
        gone = remove_todo(self.dir, SEND)
        self.assertEqual(gone.uid, uid)
        self.assertEqual(len(load_todos(self.dir)), 2)
        self.assertTrue(deliver_remove(gone.uid))
        self.assertEqual(len(self.asst.tasks()), 1)
        self.assertEqual([c[1] for c in self.asst.calls("rm")], [uid])

    def test_a_row_of_theirs_is_local_and_asst_is_not_called(self):
        gone = remove_todo(self.dir, THEIRS)
        self.assertEqual(gone.uid, "")
        self.assertTrue(deliver_remove(gone.uid))
        self.assertEqual(self.asst.calls("rm"), [])
        self.assertEqual(len(self.asst.tasks()), 2)

    def test_a_pull_does_not_bring_it_back(self):
        remove_todo(self.dir, SEND)
        pull_todos(self.dir)
        self.assertEqual(
            sorted(t.key for t in load_todos(self.dir)),
            ["draft the comparison table", THEIRS],
        )

    def test_a_task_asst_would_not_delete_is_reported(self):
        gone = remove_todo(self.dir, SEND)
        self.asst.refuse_rm(gone.uid)
        self.assertFalse(deliver_remove(gone.uid))

    def test_a_row_that_is_gone_is_not_made_up(self):
        self.assertIsNone(remove_todo(self.dir, "no such row"))


class TestAdding(EditCase):
    def test_it_is_yours_and_open_and_files_into_the_projects_list(self):
        row = add_todo_row(self.dir, "  Email   the committee ")
        self.assertEqual((row.text, row.owner), ("Email the committee", "you"))
        self.assertEqual(self.row("email the committee").uid, "")
        before = len(self.asst.tasks())

        file_with_asst(self.dir, [row])

        after = self.row("email the committee")
        self.assertTrue(after.uid)
        self.assertEqual(after.list_name, "lab")
        self.assertEqual(len(self.asst.tasks()), before + 1)

    def test_filing_it_files_nothing_else(self):
        # Something of yours that never reached asst stays where it is.
        waiting = add_todo_row(self.dir, "Left over")
        row = add_todo_row(self.dir, "Asked for")
        file_with_asst(self.dir, [row])
        self.assertEqual(self.row("left over").uid, "")
        self.assertTrue(self.row("asked for").uid)
        self.assertIsNotNone(waiting)

    def test_without_asst_it_stays_here(self):
        self.asst.down = True
        row = add_todo_row(self.dir, "Email the committee")
        file_with_asst(self.dir, [row])
        self.assertEqual(self.row("email the committee").uid, "")

    def test_a_summary_again_keeps_it(self):
        add_todo_row(self.dir, "Email the committee")
        sync_todos(self.dir, SUMMARY)
        self.assertEqual(
            len([t for t in load_todos(self.dir) if t.owner == "you"]), 3
        )

    def test_empty_wording_or_wording_already_there_is_refused(self):
        self.assertIsNone(add_todo_row(self.dir, ""))
        self.assertIsNone(add_todo_row(self.dir, "Draft the comparison TABLE"))
        self.assertEqual(len(load_todos(self.dir)), 3)


class TestMoving(EditCase):
    def test_the_task_and_the_row_change_lists(self):
        uid = self.row(SEND).uid
        self.assertTrue(move_todo(self.dir, SEND, "thesis"))
        self.assertEqual(self.row(SEND).list_name, "thesis")
        (task,) = [t for t in self.asst.tasks() if t["id"] == uid]
        self.assertEqual(task["list_name"], "thesis")

    def test_the_inbox_is_no_list_at_all(self):
        self.assertTrue(move_todo(self.dir, SEND, None))
        self.assertEqual(self.row(SEND).list_name, "Inbox")
        self.assertEqual(self.asst.calls("edit")[-1][2:], ["--list", "inbox"])

    def test_only_that_row_moves(self):
        move_todo(self.dir, SEND, "thesis")
        self.assertEqual(
            self.row("draft the comparison table").list_name, "lab"
        )

    def test_an_unfiled_row_has_no_task_to_move(self):
        self.assertFalse(move_todo(self.dir, THEIRS, "thesis"))
        self.assertEqual(self.asst.calls("edit"), [])

    def test_a_task_asst_would_not_move_leaves_the_row_alone(self):
        self.asst.down = True
        self.assertFalse(move_todo(self.dir, SEND, "thesis"))
        self.assertEqual(self.row(SEND).list_name, "lab")


class TestOpening(EditCase):
    def test_it_asks_asst_to_show_that_task(self):
        uid = self.row(SEND).uid
        self.assertTrue(asstlink.open_todo(uid))
        deadline = time.monotonic() + 5
        while not self.asst.calls("open") and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.asst.calls("open"), [["open", uid]])

    def test_an_unfiled_row_opens_nothing(self):
        self.assertFalse(asstlink.open_todo(""))


if __name__ == "__main__":
    unittest.main()
