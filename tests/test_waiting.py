"""What someone else promised, filed in asst's Waiting list.

Filing runs against tests/fakeasst.py, so a call is a subprocess and one
asst does not promise steno fails the test. The real asst is never on
the PATH of these tests. The meeting is the made-up one asst's contract
uses (consumers.json): a vendor call on Monday, October 12, 2026.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from steno import asstlink, cli
from steno.notes import (
    ask_day,
    ask_due,
    decide_owner,
    deliver_title,
    drop_open_todos,
    load_todos,
    meeting_day,
    move_with_asst,
    open_count,
    plus_working_days,
    pull_todos,
    rename_todo,
    sync_todos,
    waiting_plan,
)
from tests.fakeasst import FakeAsst

# A Monday.
STARTED = datetime(2026, 10, 12, 10, 0).timestamp()
SESSION = "20261012-100000"
FIGURES = "Send the usage figures to Alice"
QUOTE = "Send the signed quote tomorrow"
SHEET = "Share the price sheet"
SUMMARY = f"""\
# Vendor call

## Action items
**You**
- {FIGURES}
**Them**
- {QUOTE} (due: 2026-10-13)
- {SHEET}
"""
# The same meeting summarized again, with the sheet now yours and the
# figures now theirs.
SWAPPED = f"""\
# Vendor call

## Action items
**You**
- {SHEET}
**Them**
- {QUOTE} (due: 2026-10-13)
- {FIGURES}
"""
PREFIX = asstlink.THEIRS_PREFIX


def _meeting(root: Path, project: str | None = None) -> Path:
    session = root / SESSION
    session.mkdir()
    (session / "meta.json").write_text(json.dumps({
        "title": "Vendor call", "started_at": STARTED, "project": project,
    }))
    (session / "transcript.jsonl").write_text("")
    return session


class TestAskDay(unittest.TestCase):
    def test_two_working_days_after_the_day_they_gave(self):
        self.assertEqual(ask_day(date(2026, 10, 12), "2026-10-13"),
                         "2026-10-15")

    def test_a_weekend_does_not_count(self):
        # Friday's promise is asked about on Tuesday.
        self.assertEqual(ask_day(None, "2026-10-09"), "2026-10-13")
        self.assertEqual(plus_working_days(date(2026, 10, 10), 1),
                         date(2026, 10, 12))

    def test_no_day_given_is_a_working_week_after_the_meeting(self):
        self.assertEqual(ask_day(date(2026, 10, 12), ""), "2026-10-19")

    def test_a_damaged_date_falls_back_to_the_meeting(self):
        self.assertEqual(ask_day(date(2026, 10, 12), "soon"), "2026-10-19")

    def test_nothing_known_is_no_date(self):
        self.assertEqual(ask_day(None, ""), "")


class TestAskDue(unittest.TestCase):
    def test_the_day_to_ask_has_a_time_so_it_rings(self):
        self.assertEqual(ask_due("2026-10-15", today=None), "2026-10-15 9am")

    def test_a_day_already_past_goes_undated_when_told_today(self):
        self.assertEqual(ask_due("2026-10-15", today=date(2026, 10, 16)), "")
        self.assertEqual(ask_due("2026-10-15", today=date(2026, 10, 15)),
                         "2026-10-15 9am")

    def test_no_day_is_no_date(self):
        self.assertEqual(ask_due("", today=None), "")


class TestMeetingDay(unittest.TestCase):
    def test_from_meta_then_from_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _meeting(Path(tmp))
            self.assertEqual(meeting_day(session), date(2026, 10, 12))
            (session / "meta.json").write_text("{")
            self.assertEqual(meeting_day(session), date(2026, 10, 12))
            self.assertIsNone(meeting_day(Path(tmp)))


class TestWaitingList(unittest.TestCase):
    def test_the_whole_name_any_case(self):
        self.assertEqual(asstlink.waiting_list(["lab", "waiting"]), "waiting")
        self.assertIsNone(asstlink.waiting_list(["Waiting room", "lab"]))


class TestTitles(unittest.TestCase):
    def test_theirs_says_so_and_comes_back_without_it(self):
        title = asstlink.title_for(SHEET, theirs=True)
        self.assertEqual(title, PREFIX + SHEET)
        self.assertEqual(asstlink.words_of(title, theirs=True), SHEET)
        self.assertEqual(
            asstlink.words_of(title.lower(), theirs=True), SHEET.lower()
        )

    def test_yours_is_left_as_it_is(self):
        self.assertEqual(asstlink.title_for(SHEET, theirs=False), SHEET)
        self.assertEqual(
            asstlink.words_of(PREFIX + SHEET, theirs=False), PREFIX + SHEET
        )

    def test_the_prefix_alone_is_kept_whole(self):
        self.assertEqual(
            asstlink.words_of(PREFIX.strip(), theirs=True), PREFIX.strip()
        )


class FilingCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # "lab" stands for a list tied to a GitHub repo.
        self.dir = _meeting(self.root, project="lab")

    def _asst(self, *lists: str) -> FakeAsst:
        asst = FakeAsst(lists=lists).start()
        self.addCleanup(asst.stop)
        return asst

    def row(self, text: str):
        return next(t for t in load_todos(self.dir) if t.text == text)

    @staticmethod
    def task(asst: FakeAsst, uid: str) -> dict:
        return next(t for t in asst.tasks() if t["id"] == uid)


class TestFiling(FilingCase):
    def test_theirs_go_to_waiting_never_to_the_project_list(self):
        asst = self._asst("lab", "Waiting")
        sync_todos(self.dir, SUMMARY)
        by_list = {}
        for task in asst.tasks():
            by_list.setdefault(task["list_name"], []).append(task)
        self.assertEqual(len(by_list["lab"]), 1)
        quote, sheet = by_list["Waiting"]
        self.assertEqual(quote["title"], PREFIX + QUOTE)
        self.assertEqual(quote["due"], "2026-10-15 9am")
        self.assertEqual(sheet["due"], "2026-10-19 9am")
        self.assertEqual(quote["note"], "From Vendor call, October 12, 2026.")
        self.assertEqual(self.row(QUOTE).list_name, "Waiting")
        self.assertTrue(self.row(QUOTE).uid)

    def test_yours_keep_their_title_and_their_own_date(self):
        asst = self._asst("lab", "Waiting")
        sync_todos(self.dir, SUMMARY)
        (mine,) = [t for t in asst.tasks() if t["list_name"] == "lab"]
        self.assertEqual((mine["title"], mine["due"]), (FIGURES, ""))

    def test_the_source_is_the_rows_own_so_filing_twice_is_a_no_op(self):
        asst = self._asst("lab", "Waiting")
        sync_todos(self.dir, SUMMARY)
        sync_todos(self.dir, SUMMARY)
        self.assertEqual(len(asst.tasks()), 3)
        srcs = {t.src for t in load_todos(self.dir)}
        self.assertEqual({t["source"] for t in asst.tasks()}, srcs)

    def test_no_waiting_list_files_nothing_of_theirs(self):
        asst = self._asst("lab")
        sync_todos(self.dir, SUMMARY)
        (mine,) = asst.tasks()
        self.assertEqual(mine["list_name"], "lab")
        theirs = [t for t in load_todos(self.dir) if t.owner == "them"]
        self.assertTrue(all(not t.uid for t in theirs))

    def test_the_switch_still_keeps_everything_off_asst(self):
        asst = self._asst("lab", "Waiting")
        with mock.patch.dict(os.environ, {asstlink.OFF_ENV: "1"}):
            sync_todos(self.dir, SUMMARY)
        self.assertEqual(asst.calls(), [])

    def test_a_tick_on_the_phone_marks_it_received(self):
        asst = self._asst("lab", "Waiting")
        sync_todos(self.dir, SUMMARY)
        asst.complete(self.row(QUOTE).uid)
        rows = {t.text: t for t in pull_todos(self.dir)}
        self.assertTrue(rows[QUOTE].done)
        self.assertFalse(rows[SHEET].done)

    def test_still_not_counted_as_yours(self):
        self._asst("lab", "Waiting")
        sync_todos(self.dir, SUMMARY)
        self.assertEqual(open_count(self.dir), 1)

    def test_a_project_change_leaves_theirs_in_waiting(self):
        asst = self._asst("lab", "thesis", "Waiting")
        todos = sync_todos(self.dir, SUMMARY)
        move_with_asst(self.dir, todos, "thesis")
        lists = sorted(t["list_name"] for t in asst.tasks())
        self.assertEqual(lists, ["Waiting", "Waiting", "thesis"])
        local = sorted(t.list_name for t in load_todos(self.dir))
        self.assertEqual(local, ["Waiting", "Waiting", "thesis"])

    def test_putting_the_meeting_away_takes_theirs_off_the_phone(self):
        asst = self._asst("lab", "Waiting")
        sync_todos(self.dir, SUMMARY)
        drop_open_todos(self.dir)
        self.assertEqual(asst.tasks(), [])


class TestInARoom(FilingCase):
    """A room's items wait for the user's word, theirs included."""

    def setUp(self):
        super().setUp()
        meta = json.loads((self.dir / "meta.json").read_text())
        (self.dir / "meta.json").write_text(json.dumps({**meta, "room": True}))

    def test_nothing_is_filed_until_the_user_says_whose(self):
        asst = self._asst("lab", "Waiting")
        sync_todos(self.dir, SUMMARY)
        self.assertEqual(asst.tasks(), [])
        self.assertTrue(all(t.held for t in load_todos(self.dir)))

    def test_not_mine_goes_to_waiting(self):
        asst = self._asst("lab", "Waiting")
        sync_todos(self.dir, SUMMARY)
        decide_owner(self.dir, [self.row(QUOTE).key], mine=False)
        (task,) = asst.tasks()
        self.assertEqual((task["list_name"], task["title"]),
                         ("Waiting", PREFIX + QUOTE))

    def test_mine_goes_to_the_meetings_list(self):
        asst = self._asst("lab", "Waiting")
        sync_todos(self.dir, SUMMARY)
        decide_owner(self.dir, [self.row(QUOTE).key], mine=True)
        (task,) = asst.tasks()
        self.assertEqual((task["list_name"], task["title"]), ("lab", QUOTE))


class TestTitlesInAsst(FilingCase):
    """asst's title says whose a promise is; the row's words do not."""

    def test_a_pull_does_not_take_the_prefix_as_new_words(self):
        self._asst("lab", "Waiting")
        sync_todos(self.dir, SUMMARY)
        pull_todos(self.dir)
        row = self.row(QUOTE)
        self.assertEqual((row.text, row.edited), (QUOTE, False))

    def test_a_title_changed_in_asst_comes_back_without_it(self):
        asst = self._asst("lab", "Waiting")
        sync_todos(self.dir, SUMMARY)
        uid = self.row(SHEET).uid
        asst.retitle(uid, PREFIX + "Share the 2027 price sheet")
        pull_todos(self.dir)
        (row,) = [t for t in load_todos(self.dir) if t.uid == uid]
        self.assertEqual((row.text, row.edited),
                         ("Share the 2027 price sheet", True))

    def test_a_rewording_here_reaches_asst_with_it(self):
        asst = self._asst("lab", "Waiting")
        sync_todos(self.dir, SUMMARY)
        uid = self.row(SHEET).uid
        new = "Share the 2027 price sheet"
        row = rename_todo(self.dir, SHEET.lower(), new)
        self.assertTrue(deliver_title(self.dir, uid, row.text, owner="them"))
        self.assertEqual(self.task(asst, uid)["title"], PREFIX + new)
        self.assertFalse(self.row(new).retitle)

    def test_a_summary_again_that_rewords_one_keeps_it(self):
        asst = self._asst("lab", "Waiting")
        sync_todos(self.dir, SUMMARY)
        uid = self.row(SHEET).uid
        sheets = "Share the price sheets"
        sync_todos(self.dir, SUMMARY.replace(SHEET, sheets))
        self.assertEqual(self.task(asst, uid)["title"], PREFIX + sheets)


class TestChangingHands(FilingCase):
    """A summary again that gives a filed row to the other side moves
    its task and changes its title."""

    def test_theirs_now_yours_goes_to_the_project_list(self):
        asst = self._asst("lab", "Waiting")
        sync_todos(self.dir, SUMMARY)
        uid = self.row(SHEET).uid
        sync_todos(self.dir, SWAPPED)
        task = self.task(asst, uid)
        self.assertEqual((task["list_name"], task["title"]), ("lab", SHEET))
        self.assertEqual(self.row(SHEET).list_name, "lab")

    def test_yours_now_theirs_goes_to_waiting(self):
        asst = self._asst("lab", "Waiting")
        sync_todos(self.dir, SUMMARY)
        uid = self.row(FIGURES).uid
        sync_todos(self.dir, SWAPPED)
        task = self.task(asst, uid)
        self.assertEqual((task["list_name"], task["title"]),
                         ("Waiting", PREFIX + FIGURES))
        self.assertEqual(self.row(FIGURES).list_name, "Waiting")
        self.assertEqual(len(asst.tasks()), 3)

    def test_with_no_waiting_list_it_stays_where_it_was_filed(self):
        asst = self._asst("lab")
        sync_todos(self.dir, SUMMARY)
        uid = self.row(FIGURES).uid
        sync_todos(self.dir, SWAPPED)
        task = self.task(asst, uid)
        self.assertEqual((task["list_name"], task["title"]),
                         ("lab", PREFIX + FIGURES))

    def test_a_task_moved_by_hand_is_not_moved_back(self):
        asst = self._asst("lab", "thesis", "Waiting")
        sync_todos(self.dir, SUMMARY)
        uid = self.row(QUOTE).uid
        # The user puts it in another list, in asst.
        asstlink.move_todos([uid], "thesis")
        pull_todos(self.dir)
        sync_todos(self.dir, SUMMARY)
        self.assertEqual(self.task(asst, uid)["list_name"], "thesis")
        self.assertEqual(len(asst.calls("edit")), 1)


class TestCli(unittest.TestCase):
    """`steno todos --waiting`, for meetings summarized before the list."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dir = _meeting(self.root, project="lab")
        # Summarized while asst had no Waiting list: theirs never went.
        sync_todos(self.dir, SUMMARY, file=False)

    def _run(
        self, today: date, *, file: bool = False, dry_run: bool = False
    ) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._cmd_waiting(argparse.Namespace(
                dir=self.root, all=False, waiting=True,
                file=file, dry_run=dry_run,
            ), today=today)
        return out.getvalue()

    def _fake(self, *lists: str) -> FakeAsst:
        asst = FakeAsst(lists=lists).start()
        self.addCleanup(asst.stop)
        return asst

    @staticmethod
    def _adds(out: str) -> list[str]:
        return [x.strip() for x in out.splitlines() if "asst --json add" in x]

    def test_dry_run_prints_the_commands_and_calls_nothing(self):
        asst = self._fake("lab", "Waiting")
        out = self._run(date(2026, 10, 14), dry_run=True)
        self.assertEqual(asst.calls(), [])
        quote, sheet = self._adds(out)
        self.assertIn("--list Waiting --due '2026-10-15 9am'", quote)
        self.assertIn(f"-- '{PREFIX}{QUOTE}'", quote)
        self.assertIn("--due '2026-10-19 9am'", sheet)
        self.assertIn("2 to file: 2 due on their day to ask, 0 undated", out)
        self.assertTrue(all(not t.uid for t in load_todos(self.dir)))

    def test_one_whose_day_has_passed_would_go_undated(self):
        self._fake("lab", "Waiting")
        out = self._run(date(2026, 10, 16), dry_run=True)
        quote, sheet = self._adds(out)
        self.assertNotIn("--due", quote)
        self.assertIn("--due '2026-10-19 9am'", sheet)
        self.assertIn("1 undated because that day has passed", out)

    def test_file_puts_them_in_waiting_once(self):
        asst = self._fake("lab", "Waiting")
        self._run(date(2026, 10, 16), file=True)
        self._run(date(2026, 10, 16), file=True)
        waiting = [t for t in asst.tasks() if t["list_name"] == "Waiting"]
        self.assertEqual(sorted(t["due"] for t in waiting),
                         ["", "2026-10-19 9am"])
        # Only theirs: an unfiled row of yours is not this command's.
        self.assertEqual(len(asst.tasks()), 2)

    def test_file_without_the_list_says_how_to_make_it(self):
        asst = self._fake("lab")
        err = io.StringIO()
        with (
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as stop,
        ):
            self._run(date(2026, 10, 14), file=True)
        self.assertEqual(stop.exception.code, 1)
        self.assertIn("asst list new Waiting", err.getvalue())
        self.assertEqual(asst.calls("add"), [])

    def test_listing_says_how_late_each_is(self):
        out = self._run(date(2026, 10, 20))
        self.assertIn("ask 2026-10-15, 5 days ago; not in asst", out)


class TestPlan(unittest.TestCase):
    def test_only_open_unfiled_promises_of_theirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _meeting(Path(tmp))
            todos = sync_todos(session, SUMMARY, file=False)
            todos[1].uid = "T9"
            plan = waiting_plan(
                todos, list_name="Waiting", meeting=date(2026, 10, 12)
            )
        self.assertEqual([f.todo.text for f in plan], [SHEET])


if __name__ == "__main__":
    unittest.main()
