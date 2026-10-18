"""Notes and todos: extraction, and what survives a re-summarize."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steno import asstlink
from steno.notes import (
    Todo,
    _edit,
    ensure_todos,
    file_with_asst,
    forget_asst,
    load_notes,
    load_todos,
    move_with_asst,
    open_count,
    parse_action_items,
    pull_todos,
    save_notes,
    save_todos,
    set_done,
    set_done_local,
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

    def test_a_bullet_that_says_there_is_nothing_is_not_an_item(self):
        """A model padding an empty section writes one, and it is
        unassigned, so it would count as an open to-do of yours."""
        for line in (
            "No commitments were made. The next talk is in two weeks.",
            "No real action items came out of it",
            "No explicit follow-ups or deadlines were set",
            "None.",
            "None identified",
            "N/A",
            "Nothing was committed to",
        ):
            with self.subTest(line=line):
                body = f"## Action items\n**Unassigned**\n- {line}\n"
                self.assertEqual(parse_action_items(body), [])

    def test_items_that_only_start_alike_still_are(self):
        for line in (
            "Notify Bob by Friday",
            "Nominate someone to chair",
            "Nothing-burger review for the board",
            "No more than five slides in the deck",
            "Follow up on the quote",
        ):
            with self.subTest(line=line):
                body = f"## Action items\n**You**\n- {line}\n"
                self.assertEqual(
                    [t.text for t in parse_action_items(body)], [line]
                )

    def test_key_is_whitespace_insensitive(self):
        a, = parse_action_items("## Action items\n- Send   the  figures\n")
        b, = parse_action_items("## Action items\n- send the figures\n")
        self.assertEqual(a.key, b.key)


# SUMMARY with citation markers on its action items: with a space, with none,
# and with trailing spaces after.
CITED = (
    SUMMARY
    .replace("by Friday", "by Friday <!--cite 201-221,460-->")
    .replace("comparison table", "comparison table<!--cite 33-->")
    .replace("next week", "next week <!--cite 500-->  ")
)


class TestCitesInTodos(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_the_marker_is_not_part_of_the_wording(self):
        for todo in parse_action_items(CITED):
            self.assertNotIn("cite", todo.text)
            self.assertNotIn("<!--", todo.key)

    def test_text_key_and_owner_are_the_same_with_and_without_markers(self):
        """The asst `--source` and the tick state are made from these."""
        def shape(body: str) -> list[tuple[str, str, str]]:
            return [(t.text, t.key, t.owner) for t in parse_action_items(body)]

        self.assertEqual(shape(CITED), shape(SUMMARY))
        self.assertEqual(len(shape(CITED)), 3)

    def test_the_cites_ride_along(self):
        self.assertEqual(
            [t.cites for t in parse_action_items(CITED)],
            [[(201, 221), (460, 460)], [(33, 33)], [(500, 500)]],
        )
        self.assertEqual(
            [t.cites for t in parse_action_items(SUMMARY)], [[], [], []]
        )

    def test_the_same_source_is_filed_with_or_without_markers(self):
        a = Path(tempfile.mkdtemp()) / "20260101-090000"
        b = Path(tempfile.mkdtemp()) / "20260101-090000"
        for folder, body in ((a, CITED), (b, SUMMARY)):
            folder.mkdir()
            sync_todos(folder, body)
        rows = [[(t.text, t.key, t.src) for t in load_todos(f)] for f in (a, b)]
        self.assertEqual(rows[0], rows[1])

    def test_cites_are_stored_as_pairs(self):
        sync_todos(self.dir, CITED)
        raw = json.loads((self.dir / "todos.json").read_text())
        self.assertEqual(raw[0]["cites"], [[201, 221], [460, 460]])
        self.assertEqual(
            load_todos(self.dir)[0].cites, [(201, 221), (460, 460)]
        )

    def test_a_todos_file_from_before_cites_still_loads(self):
        (self.dir / "todos.json").write_text(json.dumps([
            {"text": "Send the report", "owner": "you", "done": True},
        ]))
        todo, = load_todos(self.dir)
        self.assertEqual((todo.text, todo.done, todo.cites), (
            "Send the report", True, []))

    def test_a_damaged_cites_field_costs_the_cite_not_the_todo(self):
        (self.dir / "todos.json").write_text(json.dumps([
            {"text": "One", "cites": "oops"},
            {"text": "Two", "cites": [[1], [3, 4], [9, 2]]},
        ]))
        self.assertEqual([(t.text, t.cites) for t in load_todos(self.dir)],
                         [("One", []), ("Two", [(3, 4)])])

    def test_a_resync_refreshes_cites_and_keeps_everything_else(self):
        sync_todos(self.dir, CITED)
        rows = load_todos(self.dir)
        rows[0].done, rows[0].uid, rows[0].list_name = True, "AB12", "thesis"
        rows[0].done_at = 123.0
        save_todos(self.dir, rows)
        before = load_todos(self.dir)[0]
        again = sync_todos(self.dir, CITED.replace("201-221,460", "70-75"))
        after = again[0]
        self.assertEqual(after.cites, [(70, 75)])
        for field in ("text", "owner", "done", "done_at", "uid", "src",
                      "list_name"):
            self.assertEqual(getattr(after, field), getattr(before, field))

    def test_a_resync_without_markers_clears_them(self):
        """summary.md is the source of truth; the summary lost its cites."""
        sync_todos(self.dir, CITED)
        again = sync_todos(self.dir, SUMMARY)
        self.assertEqual([t.cites for t in again], [[], [], []])

    def test_ensure_reads_a_cited_summary(self):
        (self.dir / "summary.md").write_text(CITED)
        todos = ensure_todos(self.dir)
        self.assertEqual(
            [t.text for t in todos],
            [t.text for t in parse_action_items(SUMMARY)],
        )
        self.assertEqual(todos[0].cites, [(201, 221), (460, 460)])


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

    def test_a_meeting_with_no_commitments_has_no_open_to_do(self):
        sync_todos(
            self.dir,
            "# T\n\n## Action items\n"
            "- No commitments were made. The next talk is in two weeks.\n",
        )
        self.assertEqual(open_count(self.dir), 0)

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


def _action_items(*lines: str) -> str:
    """A summary whose Action items are `lines` (owner headers included)."""
    return "# T\n\n## Action items\n" + "\n".join(lines) + "\n"


class TestResummarizeRewording(unittest.TestCase):
    """A re-summarize words the same promise differently. The row the user
    ticked, or that is already on the phone, must stay the one row."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.old = Todo(
            text="Send the usage figures by Friday", owner="you", done=True,
            done_at=5.0, uid="0F27", list_name="thesis",
            src="steno:x/send the usage figures by friday",
        )

    def resync(self, *lines: str, old: list[Todo] | None = None):
        save_todos(self.dir, old if old is not None else [self.old])
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "target_list", return_value=None), \
             mock.patch.object(asstlink, "add_todo",
                               return_value="BEEF") as add:
            merged = sync_todos(self.dir, _action_items(*lines))
        self.add = add
        return merged

    def test_a_reworded_ticked_item_is_still_one_row_and_still_ticked(self):
        merged = self.resync(
            "**You**",
            "- Send usage figures to Alice by Friday <!--cite 21-24-->",
        )
        row, = merged
        self.assertEqual(row.text, "Send usage figures to Alice by Friday")
        self.assertEqual(row.cites, [(21, 24)])
        self.assertEqual((row.done, row.done_at), (True, 5.0))
        self.assertEqual((row.uid, row.src, row.list_name),
                         ("0F27", self.old.src, "thesis"))
        self.add.assert_not_called()
        self.assertEqual(load_todos(self.dir), merged)

    def test_a_reworded_open_item_keeps_its_phone_task(self):
        self.old.done, self.old.done_at = False, None
        merged = self.resync("**You**", "- Send usage figures to Alice by Friday")
        row, = merged
        self.assertEqual((row.done, row.uid), (False, "0F27"))
        self.add.assert_not_called()

    def test_a_tick_not_yet_delivered_survives_a_reword(self):
        self.old.dirty = True
        row, = self.resync("**You**", "- Send usage figures to Alice by Friday")
        self.assertEqual((row.done, row.dirty), (True, True))

    def test_a_different_promise_is_a_new_row(self):
        merged = self.resync("**You**", "- Send the contract by Friday")
        self.assertEqual(
            [(t.text, t.done, t.uid) for t in merged],
            [("Send the contract by Friday", False, "BEEF"),
             ("Send the usage figures by Friday", True, "0F27")],
        )
        self.assertEqual(self.add.call_count, 1)

    def test_a_word_swapped_for_another_is_a_different_promise(self):
        """Each pair reads at least 80% alike. A new promise that took
        the ticked row of the old would show as done and never be filed."""
        for old, new in (
            ("Review the first draft", "Review the final draft"),
            ("Send the draft to the client", "Send the draft to the team"),
            ("Pay the first installment", "Pay the second installment"),
            ("Call the landlord about the leak",
             "Call the landlord about the heating"),
            ("Cancel the old subscription", "Cancel the new subscription"),
            ("Send the draft", "Do not send the draft"),
        ):
            with self.subTest(new=new):
                self.old.text = old
                merged = self.resync("**You**", f"- {new}")
                by_text = {t.text: t for t in merged}
                self.assertEqual(sorted(by_text), sorted([old, new]))
                self.assertEqual(
                    (by_text[old].done, by_text[old].uid), (True, "0F27")
                )
                self.assertEqual(
                    (by_text[new].done, by_text[new].uid), (False, "BEEF")
                )
                self.add.assert_called_once()

    def test_small_words_and_a_plural_do_not_make_a_new_promise(self):
        for old, new in (
            ("Send the usage figures by Friday",
             "Send usage figure by Friday"),
            ("Book the venue for the offsite", "Book a venue for the offsite"),
            ("Share the slides before the meeting",
             "Share slides before the meeting"),
            ("Call the landlord about the leak",
             "Call the landlord regarding the leak"),
        ):
            with self.subTest(new=new):
                self.old.text = old
                row, = self.resync("**You**", f"- {new}")
                self.assertEqual(row.text, new)
                self.assertEqual((row.done, row.uid), (True, "0F27"))
                self.add.assert_not_called()

    def test_a_different_name_or_day_is_a_different_promise(self):
        for old, new in (
            ("Send the report to Alice", "Send the report to Bob"),
            ("Send the usage figures by Friday",
             "Send the usage figures by Monday"),
            ("Pay 40 euros for the room", "Pay 90 euros for the room"),
        ):
            with self.subTest(new=new):
                self.old.text = old
                merged = self.resync("**You**", f"- {new}")
                self.assertEqual(
                    sorted(t.text for t in merged), sorted([old, new])
                )

    def test_another_owners_item_is_not_taken_for_yours(self):
        self.old.owner, self.old.text = "them", "Revised quote next week"
        merged = self.resync("**You**", "- Send the revised quote next week")
        self.assertEqual(len(merged), 2)
        self.assertEqual([t.done for t in merged if t.owner == "you"], [False])

    def test_one_old_row_is_taken_by_one_new_item(self):
        merged = self.resync(
            "**You**",
            "- Send usage figures to Alice by Friday",
            "- Send the usage figures to Alice by Friday, please",
        )
        self.assertEqual(sorted(t.done for t in merged), [False, True])
        self.assertEqual(len(merged), 2)

    def test_an_exact_match_is_not_stolen_by_a_look_alike(self):
        merged = self.resync(
            "**You**",
            "- Send usage figures to Alice by Friday",
            "- Send the usage figures by Friday",
        )
        by_text = {t.text: t for t in merged}
        self.assertTrue(by_text["Send the usage figures by Friday"].done)
        self.assertEqual(
            by_text["Send the usage figures by Friday"].uid, "0F27"
        )
        self.assertFalse(by_text["Send usage figures to Alice by Friday"].done)

    def test_the_old_wording_is_still_found_by_its_frozen_source(self):
        """The row keeps `src` across the reword, so asst's pull (which
        looks tasks up by it) still finds the phone task."""
        row, = self.resync("**You**", "- Send usage figures to Alice by Friday")
        remote = {self.old.src: {"id": "0F27", "completed": True,
                                 "list": "thesis"}}
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "fetch_session", return_value=remote):
            pulled, = pull_todos(self.dir)
        self.assertEqual((pulled.text, pulled.uid, pulled.done),
                         (row.text, "0F27", True))


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

    def test_a_tick_made_during_a_slow_pull_is_not_lost(self):
        """The pull reads, waits on asst, then writes. A tick in between must
        survive the write; it used to be replaced by the older list."""
        key = asstlink.source_key(self.dir, "send")
        save_todos(self.dir, [Todo(text="Send", owner="you", uid="0F27", src=key)])
        remote = {key: {"id": "0F27", "completed": False, "list": "Inbox"}}

        def slow_fetch(_dir):
            set_done_local(self.dir, "send", True)  # the user ticks meanwhile
            return remote

        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "set_done", return_value=False), \
             mock.patch.object(asstlink, "fetch_session", side_effect=slow_fetch):
            out = pull_todos(self.dir)
        self.assertTrue(out[0].done)
        self.assertTrue(load_todos(self.dir)[0].done)
        self.assertTrue(load_todos(self.dir)[0].dirty)

    def test_a_row_filed_during_the_fetch_is_not_dropped(self):
        """A summary landing files rows after the pull took its snapshot of
        the phone. They are absent from it without having been deleted."""
        save_todos(self.dir, [Todo(text="Send", owner="you")])

        def slow_fetch(_dir):
            def file(current):
                current[0].uid = "U9"
                return True
            _edit(self.dir, file)  # the filing lands while we wait on asst
            return {}

        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "add_todo", return_value=None), \
             mock.patch.object(asstlink, "fetch_session", side_effect=slow_fetch):
            out = pull_todos(self.dir)
        self.assertEqual([(t.text, t.uid) for t in out], [("Send", "U9")])

    def test_a_tick_while_a_row_is_being_filed_is_not_undone(self):
        save_todos(self.dir, [Todo(text="Send", owner="you")])

        def add(*_a, **_k):
            set_done_local(self.dir, "send", True)  # ticked during the call
            return "U9"

        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "target_list", return_value=None), \
             mock.patch.object(asstlink, "add_todo", side_effect=add):
            file_with_asst(self.dir, load_todos(self.dir))
        row = load_todos(self.dir)[0]
        self.assertEqual((row.uid, row.done, row.dirty), ("U9", True, True))

    def test_asst_timing_out_returns_what_is_on_disk_now(self):
        key = asstlink.source_key(self.dir, "send")
        save_todos(self.dir, [Todo(text="Send", owner="you", uid="U1", src=key)])

        def slow_none(_dir):
            set_done_local(self.dir, "send", True)

        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "set_done", return_value=False), \
             mock.patch.object(asstlink, "fetch_session", side_effect=slow_none):
            out = pull_todos(self.dir)
        self.assertTrue(out[0].done)

    def test_a_tick_during_a_move_survives(self):
        key = asstlink.source_key(self.dir, "send")
        save_todos(self.dir, [Todo(text="Send", owner="you", uid="U1", src=key)])
        held = load_todos(self.dir)

        def move(_uids, _list):
            set_done_local(self.dir, "send", True)
            return 1

        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "move_todos", side_effect=move):
            move_with_asst(self.dir, held, "thesis")
        row = load_todos(self.dir)[0]
        self.assertTrue(row.done)
        self.assertEqual(row.list_name, "thesis")

    def test_listing_a_meeting_does_not_file_to_asst(self):
        """ensure_todos runs for every row when the list is built."""
        (self.dir / "summary.md").write_text(SUMMARY)
        (self.dir / "meta.json").write_text("{}")
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "add_todo") as add:
            todos = ensure_todos(self.dir)
        self.assertEqual(len(todos), 3)
        add.assert_not_called()

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

    def test_a_pull_does_not_file_unfiled_items(self):
        """Filing is for when a summary lands. A pull runs whenever a meeting
        is opened and for every meeting in `steno todos`, and it used to file
        the items of a two-month-old meeting the first time anyone looked."""
        save_todos(self.dir, [
            Todo(text="Send", owner="you",
                 src=asstlink.source_key(self.dir, "send")),
        ])
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "add_todo", return_value="BEEF") as add, \
             mock.patch.object(asstlink, "fetch_session", return_value={}):
            out = pull_todos(self.dir)
        add.assert_not_called()
        self.assertEqual([(t.text, t.uid) for t in out], [("Send", "")])

    def test_a_summary_landing_files_your_items(self):
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "target_list", return_value=None), \
             mock.patch.object(asstlink, "add_todo", return_value="BEEF") as add:
            out = sync_todos(self.dir, SUMMARY)
        self.assertEqual(add.call_count, 2)  # the two "you"; not "them"
        self.assertEqual([t.uid for t in out], ["BEEF", "BEEF", ""])


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
