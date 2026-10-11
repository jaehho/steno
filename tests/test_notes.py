"""Notes and todos: extraction, and what survives a re-summarize."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steno import asstlink
from steno.notes import (
    Todo,
    ensure_todos,
    file_with_asst,
    forget_asst,
    load_notes,
    load_todos,
    open_count,
    parse_action_items,
    pull_todos,
    save_notes,
    save_todos,
    set_done,
    sync_todos,
)

SUMMARY = """# Vendor Pricing Call

## TL;DR
- They will send revised numbers.

## Action items
**You**
- Send the usage figures by Friday
- Draft the comparison table

**Them**
- Revised quote next week

## Open questions
- Whether the discount applies retroactively
"""


class TestParseActionItems(unittest.TestCase):
    def test_finds_items(self):
        todos = parse_action_items(SUMMARY)
        self.assertEqual(len(todos), 3)

    def test_assigns_owners(self):
        todos = parse_action_items(SUMMARY)
        self.assertEqual([t.owner for t in todos], ["you", "you", "them"])

    def test_stops_at_next_section(self):
        """The Open questions bullet must not become a todo."""
        texts = [t.text for t in parse_action_items(SUMMARY)]
        self.assertNotIn("Whether the discount applies retroactively", texts)

    def test_owner_and_bullet_variants(self):
        body = (
            "## Action items\n"
            "**You:**\n- Send the deck\n"
            "**Them**:\n* Revised quote\n"
            "### Unassigned\n1. Book the room\n"
        )
        got = [(t.owner, t.text) for t in parse_action_items(body)]
        self.assertEqual(got, [
            ("you", "Send the deck"),
            ("them", "Revised quote"),
            ("unassigned", "Book the room"),
        ])

    def test_an_owner_heading_does_not_end_the_section(self):
        body = "## Action items\n### You\n- One\n## Open questions\n- Not a todo\n"
        self.assertEqual([t.text for t in parse_action_items(body)], ["One"])

    def test_no_section_yields_nothing(self):
        self.assertEqual(parse_action_items("# Title\n\n## TL;DR\n- nothing"), [])

    def test_checkbox_syntax_is_stripped(self):
        todos = parse_action_items("## Action items\n- [x] Already done\n")
        self.assertEqual(todos[0].text, "Already done")

    def test_key_is_whitespace_insensitive(self):
        a, = parse_action_items("## Action items\n- Send   the  figures\n")
        b, = parse_action_items("## Action items\n- send the figures\n")
        self.assertEqual(a.key, b.key)


class TestTodoState(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_sync_writes_todos(self):
        todos = sync_todos(self.dir, SUMMARY)
        self.assertEqual(len(todos), 3)
        self.assertTrue((self.dir / "todos.json").is_file())

    def test_checkmark_survives_resummarize(self):
        sync_todos(self.dir, SUMMARY)
        first = load_todos(self.dir)[0]
        set_done(self.dir, first.key, True)
        again = sync_todos(self.dir, SUMMARY)
        self.assertTrue(again[0].done)

    def test_unsent_tick_survives_resummarize(self):
        """A tick made while asst was down must not be undone by the pull."""
        sync_todos(self.dir, SUMMARY)
        todos = load_todos(self.dir)
        todos[0].done = True
        todos[0].dirty = True
        todos[0].uid = "AB12"
        todos[0].list_name = "thesis"
        save_todos(self.dir, todos)
        with mock.patch.object(asstlink, "available", lambda: False):
            again = sync_todos(self.dir, SUMMARY)
        self.assertTrue(again[0].dirty)
        self.assertEqual(again[0].list_name, "thesis")

    def test_completed_item_kept_when_summary_drops_it(self):
        """Deleting the record of finished work is the one unrecoverable thing."""
        sync_todos(self.dir, SUMMARY)
        first = load_todos(self.dir)[0]
        set_done(self.dir, first.key, True)
        sync_todos(self.dir, "# T\n\n## Action items\n- Something else entirely\n")
        texts = [t.text for t in load_todos(self.dir) if t.done]
        self.assertEqual(texts, [first.text])

    def test_open_filed_item_survives_a_resummarize(self):
        """A re-summarize must not orphan a task the phone is holding."""
        save_todos(self.dir, [
            Todo(text="Send the report", owner="you", uid="0F27",
                 src="steno:x/send the report"),
        ])
        with mock.patch.object(asstlink, "available", lambda: False):
            sync_todos(self.dir, "# T\n\n## Action items\n- Something else\n")
        texts = [t.text for t in load_todos(self.dir)]
        self.assertIn("Send the report", texts)
        self.assertIn("Something else", texts)

    def test_open_unfiled_orphan_is_dropped(self):
        """Nothing on the phone: the model dropping it is allowed."""
        save_todos(self.dir, [Todo(text="Vanish", owner="them")])
        with mock.patch.object(asstlink, "available", lambda: False):
            sync_todos(self.dir, "# T\n\n## Action items\n- Fresh\n")
        self.assertEqual([t.text for t in load_todos(self.dir)], ["Fresh"])

    def test_src_is_frozen_across_a_clean_rerun(self):
        with mock.patch.object(asstlink, "available", lambda: False):
            first = sync_todos(self.dir, SUMMARY)
            again = sync_todos(self.dir, SUMMARY)
        self.assertEqual(first[0].src, again[0].src)
        self.assertTrue(again[0].src.startswith("steno:"))

    def test_rephrase_keeps_both_rows_and_files_only_the_new_one(self):
        save_todos(self.dir, [
            Todo(text="Send the report", owner="you", uid="0F27",
                 src="steno:x/send the report"),
        ])
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "target_list", return_value=None), \
             mock.patch.object(asstlink, "add_todo", return_value="BEEF") as add:
            merged = sync_todos(
                self.dir,
                "# T\n\n## Action items\n**You**\n- Email the report\n",
            )
        self.assertEqual([t.text for t in merged],
                         ["Email the report", "Send the report"])
        self.assertEqual(merged[0].uid, "BEEF")
        self.assertEqual(merged[1].uid, "0F27")
        self.assertEqual(add.call_count, 1)

    def test_unchecking_clears_the_timestamp(self):
        sync_todos(self.dir, SUMMARY)
        key = load_todos(self.dir)[0].key
        set_done(self.dir, key, True)
        set_done(self.dir, key, False)
        self.assertIsNone(load_todos(self.dir)[0].done_at)

    def test_open_count_is_only_your_work(self):
        """What they owe is not a to-do on this machine."""
        sync_todos(self.dir, SUMMARY)
        self.assertEqual(open_count(self.dir), 2)  # two "you", not the "them"
        set_done(self.dir, load_todos(self.dir)[0].key, True)
        self.assertEqual(open_count(self.dir), 1)

    def test_corrupt_file_is_not_fatal(self):
        (self.dir / "todos.json").write_text("{not json")
        self.assertEqual(load_todos(self.dir), [])

    def test_ensure_derives_from_summary_without_a_model(self):
        (self.dir / "summary.md").write_text(SUMMARY)
        self.assertEqual(len(ensure_todos(self.dir)), 3)

    def test_ensure_is_empty_without_a_summary(self):
        self.assertEqual(ensure_todos(self.dir), [])

    def test_ensure_prefers_existing_state(self):
        (self.dir / "summary.md").write_text(SUMMARY)
        sync_todos(self.dir, SUMMARY)
        set_done(self.dir, load_todos(self.dir)[0].key, True)
        self.assertTrue(ensure_todos(self.dir)[0].done)


class TestPullTodos(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_adopts_remote_done_and_uid(self):
        save_todos(self.dir, [
            Todo(text="Send the report", owner="you",
                 src=asstlink.source_key(self.dir, "send the report")),
        ])
        remote = {
            asstlink.source_key(self.dir, "send the report"): {
                "id": "0F27", "completed": True, "list": "thesis",
            },
        }
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "fetch_session", return_value=remote):
            out = pull_todos(self.dir)
        self.assertTrue(out[0].done)
        self.assertEqual(out[0].uid, "0F27")
        self.assertEqual(out[0].list_name, "thesis")

    def test_missing_remote_drops_the_row_that_reached_asst(self):
        """Delete on the phone means delete here. Keeping it re-files it later."""
        save_todos(self.dir, [
            Todo(text="Send", owner="you", uid="0F27",
                 src=asstlink.source_key(self.dir, "send")),
            Todo(text="They will reply", owner="them"),
        ])
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "fetch_session", return_value={}):
            out = pull_todos(self.dir)
        self.assertEqual([t.text for t in out], ["They will reply"])

    def test_unfiled_rows_stay_when_remote_is_empty(self):
        save_todos(self.dir, [Todo(text="Yours", owner="you")])
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "fetch_session", return_value={}):
            out = pull_todos(self.dir)
        self.assertEqual([t.text for t in out], ["Yours"])

    def test_asst_down_leaves_everything_alone(self):
        save_todos(self.dir, [
            Todo(text="Send", owner="you", uid="0F27",
                 src=asstlink.source_key(self.dir, "send")),
        ])
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "fetch_session", return_value=None):
            out = pull_todos(self.dir)
        self.assertEqual(out[0].uid, "0F27")
        self.assertFalse(out[0].done)

    def test_legacy_text_source_reattaches(self):
        """A row filed before `src` existed still finds its phone task."""
        save_todos(self.dir, [Todo(text="Send the report", owner="you")])
        remote = {
            asstlink.source_key(self.dir, "send the report"): {
                "id": "0F27", "completed": False,
            },
        }
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "fetch_session", return_value=remote):
            out = pull_todos(self.dir)
        self.assertEqual(out[0].uid, "0F27")


class TestOutageFallback(unittest.TestCase):
    """asstd can vanish for a minute; nothing the user did may be lost."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_failed_tick_is_dirty_and_survives_a_pull(self):
        save_todos(self.dir, [
            Todo(text="Send", owner="you", uid="0F27",
                 src=asstlink.source_key(self.dir, "send")),
        ])
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "set_done", return_value=False):
            set_done(self.dir, "send", True)
        self.assertTrue(load_todos(self.dir)[0].dirty)

        # asstd is back but still says open; the local tick must win until
        # the push lands.
        remote = {
            asstlink.source_key(self.dir, "send"): {
                "id": "0F27", "completed": False, "list": "Inbox",
            },
        }
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "fetch_session", return_value=remote), \
             mock.patch.object(asstlink, "set_done", return_value=False):
            out = pull_todos(self.dir)
        self.assertTrue(out[0].done)

    def test_dirty_tick_is_retried_when_asstd_returns(self):
        save_todos(self.dir, [
            Todo(text="Send", owner="you", uid="0F27", done=True, dirty=True,
                 src=asstlink.source_key(self.dir, "send")),
        ])
        remote = {
            asstlink.source_key(self.dir, "send"): {
                "id": "0F27", "completed": True, "list": "Inbox",
            },
        }
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "set_done", return_value=True) as push, \
             mock.patch.object(asstlink, "fetch_session", return_value=remote):
            out = pull_todos(self.dir)
        push.assert_called_once_with("0F27", True)
        self.assertFalse(out[0].dirty)
        self.assertTrue(out[0].done)

    def test_unfiled_items_are_retried_on_pull(self):
        save_todos(self.dir, [
            Todo(text="Send", owner="you",
                 src=asstlink.source_key(self.dir, "send")),
        ])
        remote = {
            asstlink.source_key(self.dir, "send"): {
                "id": "BEEF", "completed": False, "list": "Inbox",
            },
        }
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "target_list", return_value=None), \
             mock.patch.object(asstlink, "add_todo", return_value="BEEF") as add, \
             mock.patch.object(asstlink, "fetch_session", return_value=remote):
            out = pull_todos(self.dir)
        self.assertEqual(add.call_count, 1)
        self.assertEqual(out[0].uid, "BEEF")


class TestForgetAsst(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_meeting_delete_removes_phone_tasks_and_clears_uids(self):
        todos = [
            Todo(text="Send", owner="you", uid="0F27",
                 src=asstlink.source_key(self.dir, "send")),
            Todo(text="They", owner="them"),
        ]
        save_todos(self.dir, todos)
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "rm_todos", return_value=1) as rm:
            removed = forget_asst(self.dir)
        self.assertEqual(removed, 1)
        rm.assert_called_once_with(["0F27"])
        after = load_todos(self.dir)
        self.assertEqual(after[0].uid, "")
        self.assertEqual(after[0].text, "Send")

    def test_done_rows_are_not_refiled(self):
        save_todos(self.dir, [Todo(text="Done", owner="you", done=True)])
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "add_todo") as add:
            file_with_asst(self.dir, load_todos(self.dir))
        add.assert_not_called()


class TestNotes(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_roundtrip(self):
        save_notes(self.dir, "ask about pricing")
        self.assertEqual(load_notes(self.dir).strip(), "ask about pricing")

    def test_missing_notes_read_as_empty(self):
        self.assertEqual(load_notes(self.dir), "")

    def test_emptying_removes_the_file(self):
        save_notes(self.dir, "x")
        save_notes(self.dir, "   ")
        self.assertFalse((self.dir / "notes.md").is_file())

    def test_notes_are_never_rewritten_by_sync(self):
        save_notes(self.dir, "my own words")
        sync_todos(self.dir, SUMMARY)
        self.assertEqual(load_notes(self.dir).strip(), "my own words")


if __name__ == "__main__":
    unittest.main()
