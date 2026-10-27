"""GTK checks for editing the to-dos on a meeting page (issue #22).

Run by test_meeting_todo_edit.py, in a child process on a Broadway
display and never on a real one. The name keeps test discovery from
importing this file in the main process, where `import gi` would connect
to the desktop.

asst is tests/fakeasst.py, a script on a PATH of its own. The prompts
are stood in for (a dialog needs someone to answer it); what they ask
and what the page does with the answer is checked.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk, Gio, Gtk

from steno.gui.meeting import MeetingPage
from steno.gui.window import Window
from steno.notes import load_todos, sync_todos
from steno.summarize import set_archived
from tests.fakeasst import FakeAsst
from tests.gtk_meeting_scroll import wait_for
from tests.gtk_sidebar_menu import StubApp, controllers, labels, settle

SUMMARY = """# Call

## Action items
**You**
- Send the usage figures by Friday
- Draft the comparison table

**Them**
- Revised quote next week
"""
STAMP = "20260801-090000"
SEND = "Send the usage figures by Friday"
DRAFT = "Draft the comparison table"
THEIRS = "Revised quote next week"


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


def make_meeting(root: Path) -> Path:
    folder = root / STAMP
    folder.mkdir(parents=True)
    line = {"speaker": "them", "text": "hello", "ts": 1.0}
    (folder / "transcript.jsonl").write_text(json.dumps(line) + "\n")
    (folder / "meta.json").write_text(json.dumps({"title": "Call"}))
    (folder / "summary.md").write_text(SUMMARY)
    return folder


def walk(widget: Gtk.Widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from walk(child)
        child = child.get_next_sibling()


class TodoEditCase(unittest.TestCase):
    def setUp(self):
        self.raised: list[BaseException] = []
        self._hook = sys.excepthook
        sys.excepthook = lambda _kind, value, _tb: self.raised.append(value)

        self.asst = FakeAsst().start()
        self.addCleanup(self.asst.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.dir = make_meeting(self.root)
        self.prepare()
        self.app = StubApp()
        self.app.register(None)
        self.window = Window(self.app, self.root)
        self.window.present()
        settle()
        self.page: MeetingPage = self.window.meeting
        self.toasts: list[str] = []
        self.page._toast = lambda text, **_kw: self.toasts.append(text)

    def prepare(self) -> None:
        """Your two items are filed in asst; theirs stays here."""
        sync_todos(self.dir, SUMMARY)

    def tearDown(self):
        sys.excepthook = self._hook
        self.window.destroy()
        settle()
        self._tmp.cleanup()
        self.assertEqual(self.raised, [])

    def rows(self) -> dict[str, Gtk.Box]:
        """The to-do rows by their words."""
        found = {}
        for row in self.page.todo_box:
            if not isinstance(row, Gtk.Box):
                continue
            for w in walk(row):
                if isinstance(w, Gtk.Label) and w.get_label():
                    found.setdefault(w.get_label(), row)
        return found

    def row(self, text: str) -> Gtk.Box:
        return self.rows()[text]

    def right_click(self, text: str) -> Gtk.PopoverMenu:
        row = self.row(text)
        secondary = [
            g for g in controllers(row, Gtk.GestureClick)
            if g.get_button() == Gdk.BUTTON_SECONDARY
        ]
        self.assertEqual(len(secondary), 1)
        secondary[0].emit("pressed", 1, 8.0, 8.0)
        popover = self.page._row_menus.popover
        self.assertIsInstance(popover, Gtk.PopoverMenu)
        self.assertIs(popover.get_parent(), row)
        return popover

    def local(self, text: str):
        return next(t for t in load_todos(self.dir) if t.text == text)

    def task(self, uid: str) -> dict:
        (found,) = [t for t in self.asst.tasks() if t["id"] == uid]
        return found

    def answers(self, name: str, text: str):
        """Stand in for a prompt: it answers `text`, and what it asked
        is kept."""
        asked: list[dict] = []

        def ask(_parent, heading, body, on_text, **kw):
            asked.append({"heading": heading, "body": body, **kw})
            on_text(text)

        patch = mock.patch(f"steno.gui.meeting.{name}", ask)
        patch.start()
        self.addCleanup(patch.stop)
        return asked


class TestTheMenu(TodoEditCase):
    def actions(self, text: str) -> list[str]:
        popover = self.right_click(text)
        model = popover.get_menu_model()
        found = []

        def collect(menu: Gio.MenuModel) -> None:
            for i in range(menu.get_n_items()):
                action = menu.get_item_attribute_value(i, "action", None)
                if action is not None:
                    found.append(action.get_string())
                section = menu.get_item_link(i, Gio.MENU_LINK_SECTION)
                if section is not None:
                    collect(section)

        collect(model)
        return found

    def test_a_filed_row_offers_all_four(self):
        self.assertEqual(
            self.actions(SEND),
            ["todo.edit", "todo.open", "todo.move", "todo.remove"],
        )

    def test_a_row_of_theirs_offers_only_what_stays_here(self):
        self.assertEqual(self.actions(THEIRS), ["todo.edit", "todo.remove"])

    def test_remove_is_last_and_says_it_asks_first(self):
        popover = self.right_click(SEND)
        self.assertEqual(labels(popover.get_menu_model())[-1], "Remove…")

    def test_the_menu_key_opens_it_on_the_row_with_the_focus(self):
        row = self.row(SEND)
        (keys,) = controllers(row, Gtk.ShortcutController)
        triggers = [s.get_trigger().to_string() for s in keys]
        self.assertEqual(triggers, ["Menu", "<Shift>F10"])
        flags = Gtk.ShortcutActionFlags.EXCLUSIVE
        for shortcut in keys:
            self.page._row_menus.drop()
            self.assertTrue(shortcut.get_action().activate(flags, row, None))
            self.assertIs(self.page._row_menus.popover.get_parent(), row)

    def test_a_meeting_put_away_is_read_not_edited(self):
        set_archived(self.dir, True)
        self.window.sidebar.refresh()
        self.page.reload()
        settle()
        self.assertFalse(self.page.current is None)
        self.assertTrue(self.page.current.archived)
        for row in self.page.todo_box:
            if isinstance(row, Gtk.Box):
                self.assertEqual(controllers(row, Gtk.ShortcutController), [])
        self.assertFalse(
            any(
                isinstance(w, Gtk.Button) and w.has_css_class("add-todo")
                for w in walk(self.page.todo_box)
            )
        )

    def test_rebuilding_the_list_takes_the_menu_down(self):
        popover = self.right_click(SEND)
        self.page._load_todos(self.page.current, load_todos(self.dir))
        self.assertIsNone(popover.get_parent())


class TestEditing(TodoEditCase):
    def test_the_prompt_starts_on_the_words_and_the_row_takes_the_answer(self):
        asked = self.answers("ask_text", "Send the Q3 usage figures")
        popover = self.right_click(SEND)
        uid = self.local(SEND).uid

        self.assertTrue(popover.activate_action("todo.edit", None))

        (prompt,) = asked
        self.assertEqual(prompt["initial"], SEND)
        self.assertIn("Send the Q3 usage figures", self.rows())
        self.assertNotIn(SEND, self.rows())
        self.assertEqual(self.local("Send the Q3 usage figures").uid, uid)
        self.assertTrue(
            wait_for(
                lambda: self.task(uid).get("title")
                == "Send the Q3 usage figures"
            ),
            "asst never heard the new wording",
        )
        self.assertEqual(self.toasts, [])

    def test_the_same_words_change_nothing_and_call_nothing(self):
        self.answers("ask_text", SEND)
        self.right_click(SEND).activate_action("todo.edit", None)
        settle()
        self.assertEqual(self.asst.calls("edit"), [])
        self.assertIn(SEND, self.rows())

    def test_words_already_on_the_list_are_refused_and_said(self):
        self.answers("ask_text", DRAFT)
        self.right_click(SEND).activate_action("todo.edit", None)
        self.assertEqual(self.toasts, ["That is empty, or already on the list"])
        self.assertIn(SEND, self.rows())

    def test_a_row_of_theirs_is_reworded_here_only(self):
        self.answers("ask_text", "Revised quote by Monday")
        self.right_click(THEIRS).activate_action("todo.edit", None)
        self.assertIn("Revised quote by Monday", self.rows())
        settle()
        self.assertEqual(self.asst.calls("edit"), [])

    def test_when_asst_is_down_the_row_is_still_reworded_and_it_says_so(self):
        self.answers("ask_text", "Send the Q3 usage figures")
        popover = self.right_click(SEND)
        self.asst.down = True
        popover.activate_action("todo.edit", None)
        self.assertIn("Send the Q3 usage figures", self.rows())
        self.assertTrue(
            wait_for(lambda: bool(self.toasts)), "nothing was said"
        )
        self.assertIn("asst did not take the new wording", self.toasts[0])


class TestRemoving(TodoEditCase):
    def confirm(self):
        patch = mock.patch.object(
            self.page, "_dialog",
            side_effect=lambda _h, _b, _c, go, **_kw: go(),
        )
        self.dialog = patch.start()
        self.addCleanup(patch.stop)

    def test_it_asks_first_and_says_the_task_goes_too(self):
        with mock.patch.object(self.page, "_dialog") as dialog:
            self.right_click(SEND).activate_action("todo.remove", None)
        heading, body, confirm, _go = dialog.call_args.args
        self.assertEqual(heading, "Remove this to-do?")
        self.assertIn(SEND, body)
        self.assertIn("asst", body)
        self.assertEqual(confirm, "Remove")
        self.assertIn(SEND, self.rows())  # not before the answer
        self.assertEqual(len(self.asst.calls("rm")), 0)

    def test_a_row_of_theirs_says_nothing_of_asst(self):
        with mock.patch.object(self.page, "_dialog") as dialog:
            self.right_click(THEIRS).activate_action("todo.remove", None)
        self.assertNotIn("asst", dialog.call_args.args[1])

    def test_yes_removes_the_row_and_its_task(self):
        self.confirm()
        uid = self.local(SEND).uid
        self.right_click(SEND).activate_action("todo.remove", None)
        self.assertNotIn(SEND, self.rows())
        self.assertNotIn(SEND, [t.text for t in load_todos(self.dir)])
        self.assertTrue(
            wait_for(lambda: uid not in [t["id"] for t in self.asst.tasks()]),
            "the task is still in asst",
        )
        self.assertEqual(self.toasts, [])

    def test_the_count_follows(self):
        self.confirm()
        self.assertEqual(self.window.sidebar.sessions[0].open_todos, 2)
        self.right_click(SEND).activate_action("todo.remove", None)
        self.assertEqual(self.window.sidebar.sessions[0].open_todos, 1)

    def test_a_task_asst_would_not_delete_is_said(self):
        self.confirm()
        self.asst.refuse_rm(self.local(SEND).uid)
        self.right_click(SEND).activate_action("todo.remove", None)
        self.assertTrue(wait_for(lambda: bool(self.toasts)))
        self.assertIn("would not delete", self.toasts[0])

    def test_the_last_row_leaves_the_empty_page_and_the_way_to_add(self):
        self.confirm()
        for text in (SEND, DRAFT, THEIRS):
            self.right_click(text).activate_action("todo.remove", None)
        self.assertEqual(self.rows(), {})
        self.assertTrue(self.page.add_todo_button.get_visible())


class TestAdding(TodoEditCase):
    def test_it_is_at_the_end_of_the_section(self):
        last = self.page.todo_box.get_last_child()
        self.assertIs(last, self.page.add_todo_button)
        labels_ = [
            w.get_label() for w in walk(last) if isinstance(w, Gtk.Label)
        ]
        self.assertEqual(labels_, ["Add a to-do"])

    def test_the_answer_becomes_your_row_and_is_filed(self):
        asked = self.answers("ask_text", "Email the committee")
        self.page.add_todo_button.emit("clicked")

        self.assertEqual(asked[0]["heading"], "Add a to-do")
        self.assertIn("Email the committee", self.rows())
        self.assertEqual(self.local("Email the committee").owner, "you")
        self.assertTrue(
            wait_for(lambda: self.local("Email the committee").uid != ""),
            "it was never filed in asst",
        )
        self.assertEqual(self.window.sidebar.sessions[0].open_todos, 3)
        # Back on the list with the way to its task.
        self.assertTrue(
            wait_for(lambda: any(
                isinstance(w, Gtk.Button) and w.has_css_class("todo-list")
                for w in walk(self.row("Email the committee"))
            ))
        )
        self.assertIs(
            self.page.todo_box.get_last_child(), self.page.add_todo_button
        )

    def test_nothing_else_waiting_is_filed_with_it(self):
        self.asst.down = True
        sync_todos(self.dir, SUMMARY)  # nothing new; asst is down
        self.asst.down = False
        before = len(self.asst.calls("add"))
        self.answers("ask_text", "Email the committee")
        self.page.add_todo_button.emit("clicked")
        self.assertTrue(
            wait_for(lambda: self.local("Email the committee").uid != "")
        )
        self.assertEqual(len(self.asst.calls("add")), before + 1)

    def test_empty_or_repeated_words_are_refused_and_said(self):
        self.answers("ask_text", DRAFT)
        self.page.add_todo_button.emit("clicked")
        self.assertEqual(self.toasts, ["That is empty, or already on the list"])
        self.assertEqual(len(load_todos(self.dir)), 3)

    def test_without_asst_it_stays_here(self):
        self.asst.down = True
        self.answers("ask_text", "Email the committee")
        self.page.add_todo_button.emit("clicked")
        settle()
        self.assertIn("Email the committee", self.rows())
        self.assertEqual(self.local("Email the committee").uid, "")


class TestTheListName(TodoEditCase):
    def list_button(self, text: str) -> Gtk.Button | None:
        found = [
            w for w in walk(self.row(text))
            if isinstance(w, Gtk.Button) and w.has_css_class("todo-list")
        ]
        return found[0] if found else None

    def test_a_filed_row_names_its_list_on_a_button_that_opens_the_task(self):
        uid = self.local(SEND).uid
        button = self.list_button(SEND)
        self.assertEqual(button.get_label(), "Inbox")
        button.emit("clicked")
        self.assertTrue(
            wait_for(lambda: self.asst.calls("open") == [["open", uid]]),
            f"asst was asked {self.asst.calls('open')}",
        )

    def test_a_row_that_is_not_filed_has_no_list_to_name(self):
        self.assertIsNone(self.list_button(THEIRS))

    def test_open_in_the_menu_does_the_same(self):
        uid = self.local(SEND).uid
        self.right_click(SEND).activate_action("todo.open", None)
        self.assertTrue(
            wait_for(lambda: self.asst.calls("open") == [["open", uid]])
        )


class TestMoving(TodoEditCase):
    def pick(self, name: str):
        asked: list[tuple] = []

        def ask(_parent, heading, names, current, on_pick):
            asked.append((heading, list(names), current))
            on_pick(name)

        patch = mock.patch("steno.gui.meeting.ask_list", ask)
        patch.start()
        self.addCleanup(patch.stop)
        return asked

    def test_it_offers_the_inbox_and_asts_lists_and_moves_the_task(self):
        asked = self.pick("thesis")
        uid = self.local(SEND).uid
        self.right_click(SEND).activate_action("todo.move", None)
        self.assertTrue(wait_for(lambda: bool(asked)), "no list was offered")
        self.assertEqual(asked[0][1], ["Inbox", "thesis", "lab"])
        self.assertEqual(asked[0][2], "Inbox")
        self.assertTrue(
            wait_for(lambda: self.task(uid)["list_name"] == "thesis")
        )
        self.assertTrue(
            wait_for(lambda: self.local(SEND).list_name == "thesis")
        )
        self.assertEqual(self.local(DRAFT).list_name, "Inbox")

    def test_without_lists_it_says_so(self):
        self.asst.down = True
        popover = self.right_click(SEND)
        popover.activate_action("todo.move", None)
        self.assertTrue(wait_for(lambda: bool(self.toasts)))
        self.assertEqual(self.toasts, ["asst has no lists to move it to"])


if __name__ == "__main__":
    unittest.main()
