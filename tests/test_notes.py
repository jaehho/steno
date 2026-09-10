"""Notes and todos: extraction, and what survives a re-summarize."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from steno.notes import (
    ensure_todos,
    load_notes,
    load_todos,
    open_count,
    parse_action_items,
    save_notes,
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

    def test_completed_item_kept_when_summary_drops_it(self):
        """Deleting the record of finished work is the one unrecoverable thing."""
        sync_todos(self.dir, SUMMARY)
        first = load_todos(self.dir)[0]
        set_done(self.dir, first.key, True)
        sync_todos(self.dir, "# T\n\n## Action items\n- Something else entirely\n")
        texts = [t.text for t in load_todos(self.dir) if t.done]
        self.assertEqual(texts, [first.text])

    def test_unchecking_clears_the_timestamp(self):
        sync_todos(self.dir, SUMMARY)
        key = load_todos(self.dir)[0].key
        set_done(self.dir, key, True)
        set_done(self.dir, key, False)
        self.assertIsNone(load_todos(self.dir)[0].done_at)

    def test_open_count(self):
        sync_todos(self.dir, SUMMARY)
        set_done(self.dir, load_todos(self.dir)[0].key, True)
        self.assertEqual(open_count(self.dir), 2)

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
