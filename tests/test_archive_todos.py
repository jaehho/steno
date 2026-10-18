"""Putting a meeting away closes its open to-dos; bringing it back
opens them (issue #29).

The asst side runs against tests/fakeasst.py: a script that keeps its
tasks in a file and logs its calls, so a removal is a subprocess that
prints nothing and exits 0, as the real one does. The real asst is
never on the PATH of these tests.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steno import asstlink
from steno.notes import (
    Dropped,
    Restored,
    Todo,
    drop_open_todos,
    forget_asst,
    load_todos,
    move_with_asst,
    open_count,
    pull_todos,
    restore_dropped_todos,
    save_todos,
    set_done,
    sync_todos,
)
from steno.summarize import set_archived
from tests.fakeasst import FakeAsst

SUMMARY = """# Lab sync

## Action items
**You**
- Send the usage figures by Friday
- Draft the comparison table

**Them**
- Revised quote next week
"""


class AwayCase(unittest.TestCase):
    """A meeting of the lab project with two of your to-dos filed in
    asst and one of theirs kept here."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "20260801-090000"
        self.dir.mkdir()
        (self.dir / "meta.json").write_text(json.dumps({"project": "lab"}))
        self.asst = FakeAsst().start()
        self.addCleanup(self.asst.stop)
        sync_todos(self.dir, SUMMARY)

    def rows(self) -> dict[str, Todo]:
        return {t.text.split()[0]: t for t in load_todos(self.dir)}

    def put_away(self) -> Dropped:
        set_archived(self.dir, True)
        return drop_open_todos(self.dir)

    def bring_back(self) -> Restored:
        set_archived(self.dir, False)
        return restore_dropped_todos(self.dir)


class TestPuttingAway(AwayCase):
    def test_the_tasks_in_the_lab_list_are_what_it_starts_from(self):
        self.assertEqual(
            [t["list_name"] for t in self.asst.tasks()], ["lab", "lab"]
        )
        self.assertEqual(open_count(self.dir), 2)

    def test_its_open_tasks_are_removed_from_asst_and_the_rows_closed(self):
        before = self.rows()
        self.assertEqual(self.put_away(), Dropped(removed=2, stuck=0))

        self.assertEqual(self.asst.tasks(), [])
        removed = [c[1] for c in self.asst.calls("rm")]
        self.assertCountEqual(
            removed, [before["Send"].uid, before["Draft"].uid]
        )
        after = self.rows()
        for key, row in before.items():
            self.assertTrue(after[key].dropped, key)
            self.assertFalse(after[key].open, key)
            # What a re-summarize and an Unarchive need.
            self.assertEqual(
                (after[key].text, after[key].uid, after[key].src),
                (row.text, row.uid, row.src),
            )

    def test_none_of_it_is_counted(self):
        self.put_away()
        self.assertEqual(open_count(self.dir), 0)

    def test_a_pull_right_after_a_removal_finds_the_row_closed(self):
        """asst announces each change, and a window pulls on it. The
        row has to be closed before the task goes, or that pull finds
        it deleted in asst and takes it out of the file."""
        real = asstlink.rm_todo

        def remove_then_pull(uid: str) -> bool:
            done = real(uid)
            pull_todos(self.dir)
            return done

        with mock.patch.object(asstlink, "rm_todo", remove_then_pull):
            self.assertEqual(self.put_away(), Dropped(removed=2, stuck=0))
        self.assertEqual(len(load_todos(self.dir)), 3)

    def test_a_task_asst_would_not_delete_keeps_its_row_open(self):
        sends = self.rows()["Send"].uid
        self.asst.refuse_rm(sends)
        self.assertEqual(self.put_away(), Dropped(removed=1, stuck=1))

        rows = self.rows()
        self.assertTrue(rows["Draft"].dropped)
        self.assertFalse(rows["Send"].dropped)
        self.assertEqual(rows["Send"].uid, sends)
        self.assertEqual([t["id"] for t in self.asst.tasks()], [sends])
        # It is in a meeting that is put away, whatever asst said.
        self.assertEqual(open_count(self.dir), 0)

    def test_asst_down_is_an_archive_that_removed_nothing(self):
        self.asst.down = True
        self.assertEqual(self.put_away(), Dropped(removed=0, stuck=2))
        rows = self.rows()
        self.assertFalse(rows["Send"].dropped)
        self.assertFalse(rows["Draft"].dropped)
        self.assertTrue(rows["Revised"].dropped)  # never in asst
        self.assertEqual(len(self.asst.tasks()), 2)

        # Asked again once asst answers, it takes them out.
        self.asst.down = False
        self.assertEqual(drop_open_todos(self.dir), Dropped(2, 0))
        self.assertEqual(self.asst.tasks(), [])
        self.assertTrue(all(t.dropped for t in load_todos(self.dir)))

    def test_a_done_to_do_and_its_task_are_left_alone(self):
        set_done(self.dir, "send the usage figures by friday", True)
        sends = self.rows()["Send"].uid
        self.assertEqual(self.put_away(), Dropped(removed=1, stuck=0))

        rows = self.rows()
        self.assertFalse(rows["Send"].dropped)
        self.assertTrue(rows["Send"].done)
        self.assertEqual(
            [(t["id"], t["status"]) for t in self.asst.tasks()],
            [(sends, "completed")],
        )
        self.assertNotIn(["rm", sends], self.asst.calls("rm"))

    def test_a_task_deleted_in_asst_meanwhile_is_not_a_failure(self):
        gone = self.rows()["Send"].uid
        self.asst.remove(gone)
        self.assertEqual(self.put_away(), Dropped(removed=1, stuck=0))
        # Its row went with it, as a pull has it: deleted on purpose.
        self.assertNotIn("Send", self.rows())

    def test_a_to_do_with_no_task_is_closed_here_alone(self):
        save_todos(self.dir, [Todo(text="Call the vendor", owner="you")])
        self.assertEqual(self.put_away(), Dropped(0, 0))
        self.assertTrue(self.rows()["Call"].dropped)
        self.assertNotIn("Call", [c[1] for c in self.asst.calls("rm")])

    def test_asking_again_changes_nothing(self):
        self.put_away()
        calls = len(self.asst.calls("rm"))
        self.assertEqual(drop_open_todos(self.dir), Dropped(0, 0))
        self.assertEqual(len(self.asst.calls("rm")), calls)

    def test_a_pull_does_not_take_a_closed_row_for_a_deleted_task(self):
        """It is absent from asst on purpose; a pull used to delete a
        row that was filed and then vanished from there."""
        self.put_away()
        pull_todos(self.dir)
        self.assertEqual(len(load_todos(self.dir)), 3)
        self.assertTrue(all(t.dropped for t in load_todos(self.dir)))

    def test_a_new_summary_files_nothing_for_it(self):
        self.put_away()
        adds = len(self.asst.calls("add"))
        sync_todos(self.dir, SUMMARY.replace(
            "**Them**", "- Book the room\n\n**Them**"
        ))
        self.assertEqual(len(self.asst.calls("add")), adds)
        self.assertEqual(self.asst.tasks(), [])

    def test_changing_its_project_moves_no_closed_task(self):
        self.put_away()
        move_with_asst(self.dir, load_todos(self.dir), "thesis")
        self.assertEqual(self.asst.calls("edit"), [])

    def test_deleting_it_afterwards_removes_only_what_is_still_there(self):
        stuck = self.rows()["Send"].uid
        self.asst.refuse_rm(stuck)
        self.put_away()
        self.asst.refuse_rm()
        removals = len(self.asst.calls("rm"))

        self.assertEqual(forget_asst(self.dir), 1)
        self.assertEqual(
            [c[1] for c in self.asst.calls("rm")[removals:]], [stuck]
        )
        self.assertEqual(self.asst.tasks(), [])

    def test_a_new_summary_keeps_the_rows_closed(self):
        self.put_away()
        rows = sync_todos(self.dir, SUMMARY)
        self.assertTrue(all(t.dropped for t in rows))


class TestBringingBack(AwayCase):
    def setUp(self):
        super().setUp()
        self.before = self.rows()
        self.put_away()

    def test_the_rows_are_open_again_and_filed_by_the_same_source(self):
        self.assertEqual(self.bring_back(), Restored(filed=2, failed=0))

        rows = self.rows()
        for key, row in rows.items():
            self.assertTrue(row.open, key)
            self.assertEqual(row.src, self.before[key].src)
        self.assertEqual(rows["Revised"].uid, "")  # theirs, never filed
        self.assertCountEqual(
            [t["source"] for t in self.asst.tasks()],
            [self.before["Send"].src, self.before["Draft"].src],
        )
        self.assertEqual(
            sorted(t["id"] for t in self.asst.tasks()),
            sorted((rows["Send"].uid, rows["Draft"].uid)),
        )
        # Back in the project's list.
        self.assertEqual(
            [t["list_name"] for t in self.asst.tasks()], ["lab", "lab"]
        )
        self.assertEqual(open_count(self.dir), 2)

    def test_doing_it_twice_files_nothing_twice(self):
        self.bring_back()
        adds = len(self.asst.calls("add"))
        self.assertEqual(restore_dropped_todos(self.dir), Restored(0, 0))
        self.assertEqual(len(self.asst.calls("add")), adds)
        self.assertEqual(len(self.asst.tasks()), 2)

    def test_a_task_asst_still_holds_is_found_not_filed_again(self):
        """A row closed whose removal never happened (the window was
        killed between the two)."""
        self.bring_back()
        rows = load_todos(self.dir)
        for t in rows:
            t.dropped = True
        save_todos(self.dir, rows)
        held = [t["id"] for t in self.asst.tasks()]

        self.assertEqual(restore_dropped_todos(self.dir), Restored(2, 0))
        self.assertEqual([t["id"] for t in self.asst.tasks()], held)
        self.assertCountEqual(
            [t.uid for t in load_todos(self.dir) if t.owner == "you"], held
        )

    def test_asst_down_leaves_them_open_here_and_says_so(self):
        self.asst.down = True
        self.assertEqual(self.bring_back(), Restored(filed=0, failed=2))
        rows = self.rows()
        self.assertTrue(rows["Send"].open)
        self.assertEqual(rows["Send"].uid, "")
        self.assertEqual(open_count(self.dir), 2)

    def test_a_meeting_still_put_away_files_nothing(self):
        set_archived(self.dir, True)
        adds = len(self.asst.calls("add"))
        restore_dropped_todos(self.dir)
        self.assertEqual(len(self.asst.calls("add")), adds)


class TestWithNoAsst(unittest.TestCase):
    """The suite's default: asst is not there."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "meta.json").write_text("{}")
        save_todos(self.dir, [
            Todo(text="Send", owner="you"),
            Todo(text="Revised", owner="them"),
            Todo(text="Done", owner="you", done=True),
        ])

    def test_the_rows_are_closed_and_opened_here(self):
        set_archived(self.dir, True)
        self.assertEqual(drop_open_todos(self.dir), Dropped(0, 0))
        self.assertEqual(
            [t.dropped for t in load_todos(self.dir)], [True, True, False]
        )
        self.assertEqual(open_count(self.dir), 0)

        set_archived(self.dir, False)
        self.assertEqual(restore_dropped_todos(self.dir), Restored(0, 0))
        self.assertEqual(
            [t.open for t in load_todos(self.dir)], [True, True, False]
        )
        self.assertEqual(open_count(self.dir), 1)


if __name__ == "__main__":
    unittest.main()
