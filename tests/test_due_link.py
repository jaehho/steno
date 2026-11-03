"""A filed task's due date and its way back to the meeting (issue 21).

Filing runs against tests/fakeasst.py, so a call is a subprocess and one
asst does not promise steno fails the test. The real asst is never on
the PATH of these tests.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from steno import asstlink, citations, claude, summarize
from steno.notes import (
    add_todo_row,
    load_todos,
    parse_action_items,
    split_due,
    sync_todos,
)
from tests.fakeasst import FakeAsst

STARTED = datetime(2026, 10, 12, 10, 0).timestamp()
SUMMARY = """\
# Vendor call

## Action items
**You**
- Send the metadata by Wednesday (due: 2026-10-14) <!--cite 21-->
- Book the room
**Them**
- Reply by Friday (due: 2026-10-16)
"""


class TestSplitDue(unittest.TestCase):
    def test_the_date_is_not_part_of_the_wording(self):
        self.assertEqual(
            split_due("Send it by Friday (due: 2026-10-16)"),
            ("Send it by Friday", "2026-10-16"),
        )

    def test_no_date(self):
        self.assertEqual(split_due("Send it (soon)"), ("Send it (soon)", ""))

    def test_what_is_not_a_date_is_still_not_wording(self):
        for bad in ("Friday", "2026-02-30", ""):
            with self.subTest(bad):
                self.assertEqual(
                    split_due(f"Send it (due: {bad})"), ("Send it", "")
                )


class TestParse(unittest.TestCase):
    def test_a_date_changes_neither_key_nor_cites(self):
        (send, room, reply) = parse_action_items(SUMMARY)
        self.assertEqual(send.text, "Send the metadata by Wednesday")
        self.assertEqual(send.key, "send the metadata by wednesday")
        self.assertEqual(send.due, "2026-10-14")
        self.assertEqual(send.cites, citations.parse(
            "- x <!--cite 21-->"
        ))
        self.assertEqual(room.due, "")
        self.assertEqual((reply.owner, reply.due), ("them", "2026-10-16"))

    def test_the_models_tag_after_the_date_still_cites(self):
        reply = "## Action items\n**You**\n- Send it (due: 2026-10-14) [L0]\n"
        (todo,) = parse_action_items(citations.from_model(reply, [21]))
        self.assertEqual((todo.text, todo.due), ("Send it", "2026-10-14"))
        self.assertTrue(todo.cites)


class TestOriginNote(unittest.TestCase):
    def test_title_and_date(self):
        self.assertEqual(
            asstlink.origin_note("20261012-100000", "Vendor call", STARTED),
            "From Vendor call, October 12, 2026.",
        )

    def test_what_is_missing_is_left_out(self):
        self.assertEqual(
            asstlink.origin_note("s", None, STARTED), "From October 12, 2026.")
        self.assertEqual(
            asstlink.origin_note("s", None, None), "From meeting s.")


class TestDuePrompt(unittest.TestCase):
    def test_it_counts_from_the_day_of_the_meeting(self):
        self.assertIn(
            "Monday, 2026-10-12", summarize.due_prompt(STARTED)
        )

    def test_no_day_no_dates(self):
        self.assertEqual(summarize.due_prompt(0.0), "")

    def test_the_model_is_told_the_day(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        session = Path(tmp.name)
        (session / "meta.json").write_text(
            json.dumps({"started_at": STARTED})
        )
        (session / "transcript.jsonl").write_text(json.dumps({
            "ts": STARTED, "speaker": "them", "text": "by Wednesday",
        }) + "\n")
        reply = {
            "summary": "# Vendor call\n\n## TL;DR\n- Talked. [L1]\n",
            "project": None,
        }
        with mock.patch.object(
            claude, "run_structured",
            new=mock.AsyncMock(return_value=claude.Reply(reply, "m")),
        ) as run:
            asyncio.run(summarize.summarize_session(
                session, "m", None, session / "no-projects"
            ))
        self.assertIn("Monday, 2026-10-12", run.call_args.kwargs["system"])


class TestFiling(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "20261012-100000"
        self.dir.mkdir()
        (self.dir / "meta.json").write_text(json.dumps({
            "title": "Vendor call", "started_at": STARTED,
        }))
        self.asst = FakeAsst().start()
        self.addCleanup(self.asst.stop)

    def test_your_items_go_with_their_date_and_a_way_back(self):
        sync_todos(self.dir, SUMMARY)
        send, room = self.asst.tasks()
        self.assertEqual(send["due"], "2026-10-14")
        self.assertEqual(room["due"], "")
        for task in (send, room):
            self.assertEqual(
                task["note"], "From Vendor call, October 12, 2026.")

    def test_a_row_added_by_hand_has_the_way_back_too(self):
        (self.dir / "todos.json").write_text("[]\n")
        add_todo_row(self.dir, "Mine")
        sync_todos(self.dir, "")
        (task,) = self.asst.tasks()
        self.assertEqual(task["due"], "")
        self.assertEqual(task["note"], "From Vendor call, October 12, 2026.")

    def test_a_date_changed_in_asst_stays_changed(self):
        sync_todos(self.dir, SUMMARY)
        (send, _room) = self.asst.tasks()
        self.asst.change_due(send["id"], "2026-11-01")
        filed = len(self.asst.calls("add"))

        again = SUMMARY.replace("2026-10-14", "2026-10-21")
        sync_todos(self.dir, again)

        self.assertEqual(self.asst.tasks()[0]["due"], "2026-11-01")
        self.assertEqual(len(self.asst.calls("add")), filed)
        self.assertEqual(self.asst.calls("edit"), [])

    def test_a_summary_that_drops_the_date_does_not_unsay_it(self):
        sync_todos(self.dir, SUMMARY)
        sync_todos(self.dir, SUMMARY.replace(" (due: 2026-10-14)", ""))
        self.assertEqual(load_todos(self.dir)[0].due, "2026-10-14")

    def test_a_refused_date_still_files_the_promise(self):
        real = asstlink._run

        def refuse_extras(args):
            return None if "--note" in args else real(args)

        with mock.patch.object(asstlink, "_run", side_effect=refuse_extras):
            uid = asstlink.add_todo(self.dir, "Send", "send", due="2026-10-14")
        self.assertTrue(uid)
        self.assertEqual(self.asst.tasks()[0]["due"], "")


if __name__ == "__main__":
    unittest.main()
