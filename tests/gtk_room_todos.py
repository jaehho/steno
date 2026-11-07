"""GTK checks for a meeting held in a room: its items wait under "Whose
are these?" with Mine and Not mine, and a recording kept on the laptop
asks whether everyone agreed before anything is sent.

Run by test_room_window.py, in a child process on a Broadway display and
never on a real one. The name keeps test discovery from importing this
file in the main process, where `import gi` would connect to the desktop.

asst is tests/fakeasst.py, a script on a PATH of its own.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import json
import sys
import tempfile
import unittest
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk, Gio, Gtk

from steno.gui import words
from steno.gui.meeting import MeetingPage
from steno.gui.window import Window
from steno.notes import load_todos, sync_todos
from tests.fakeasst import FakeAsst
from tests.gtk_meeting_scroll import wait_for
from tests.gtk_meeting_todo_edit import walk
from tests.gtk_sidebar_menu import StubApp, controllers, labels_in, settle

SUMMARY = """# Group meeting

## Action items
**You**
- Send the slides by Friday

**Them**
- voice 2: book the room
- voice 3: redo the chart
"""
ROOM = "20300116-140200"
KEPT = "20300117-140200"
SEND = "Send the slides by Friday"
BOOK = "voice 2: book the room"
REDO = "voice 3: redo the chart"


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


def make_room(root: Path) -> Path:
    """A room meeting, summarized: three voices on the microphone."""
    folder = root / ROOM
    folder.mkdir(parents=True)
    lines = [
        {"ts": 1.0 + i, "speaker": "you", "who": f"mic_{i}", "text": text}
        for i, text in enumerate(("Can you send the slides?", "Yes.", "Ok."))
    ]
    (folder / "transcript.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in lines)
    )
    (folder / "meta.json").write_text(json.dumps({
        "title": "Group meeting", "manual": True, "room": True,
    }))
    (folder / "summary.md").write_text(SUMMARY)
    return folder


def make_kept(root: Path) -> Path:
    """A room recording that ended and was kept: audio, nothing sent."""
    folder = root / KEPT
    folder.mkdir(parents=True)
    (folder / "transcript.jsonl").write_text("")
    (folder / "meta.json").write_text(json.dumps({
        "started_at": 1.0, "app": "manual", "manual": True,
        "mic_pending": True,
    }))
    (folder / "you.wav").write_bytes(b"RIFF....WAVE")
    return folder


class RoomCase(unittest.TestCase):
    def setUp(self):
        self.raised: list[BaseException] = []
        self._hook = sys.excepthook
        sys.excepthook = lambda _kind, value, _tb: self.raised.append(value)

        self.asst = FakeAsst().start()
        self.addCleanup(self.asst.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.dir = make_room(self.root)
        self.kept = make_kept(self.root)
        sync_todos(self.dir, SUMMARY)
        self.app = StubApp()
        self.app.register(None)
        self.window = Window(self.app, self.root)
        self.window.present()
        settle()
        self.page: MeetingPage = self.window.meeting
        self.window.sidebar.select(self.dir)
        settle()
        self.assertEqual(self.page.current.dir, self.dir)

    def tearDown(self):
        sys.excepthook = self._hook
        self.window.destroy()
        settle()
        self._tmp.cleanup()
        self.assertEqual(self.raised, [])

    def texts(self) -> list[str]:
        """The to-do column, top to bottom, as words."""
        return [t for t in labels_in(self.page.todo_box) if t]

    def row(self, text: str) -> Gtk.Box:
        key = " ".join(text.lower().split())
        return self.page._todo_rows[key][0]

    def button(self, within: Gtk.Widget, label: str) -> Gtk.Button:
        (found,) = [
            w for w in walk(within)
            if isinstance(w, Gtk.Button) and w.get_label() == label
        ]
        return found

    def local(self, text: str):
        return next(t for t in load_todos(self.dir) if t.text == text)


class TestWhose(RoomCase):
    def test_every_item_waits_under_whose_with_nothing_filed(self):
        texts = self.texts()
        self.assertEqual(texts[0], words.HELD_HEAD)
        self.assertIn(words.HELD_HINT, texts)
        for text in (SEND, BOOK, REDO):
            self.assertLess(texts.index(text), texts.index("For you"))
            self.button(self.row(text), "Mine")
            self.button(self.row(text), "Not mine")
        self.assertEqual(self.asst.tasks(), [])
        # None is yours yet, so the page counts none as to do.
        self.assertEqual(self.page.todo_count.get_text(), "")

    def test_the_list_of_meetings_says_how_many_to_sort(self):
        (row,) = [
            r for r in walk(self.window.sidebar.list)
            if isinstance(r, Gtk.ListBoxRow) and "Group meeting" in labels_in(r)
        ]
        self.assertIn("3 to sort", labels_in(row))

    def test_mine_files_it_and_it_joins_your_items(self):
        self.button(self.row(SEND), "Mine").emit("clicked")
        self.assertTrue(
            wait_for(lambda: len(self.asst.tasks()) == 1), "nothing was filed"
        )
        self.assertEqual(self.asst.tasks()[0]["title"], SEND)
        row = self.local(SEND)
        self.assertEqual((row.owner, row.held, row.owner_by_user),
                         ("you", False, True))
        self.assertTrue(wait_for(lambda: bool(self.local(SEND).uid)))
        texts = self.texts()
        self.assertGreater(texts.index(SEND), texts.index("For you"))
        self.assertEqual(self.page.todo_count.get_text(), "1 to do")

    def test_not_mine_files_nothing_and_can_be_taken_back(self):
        self.button(self.row(BOOK), "Not mine").emit("clicked")
        settle()
        row = self.local(BOOK)
        self.assertEqual((row.owner, row.held), ("them", False))
        texts = self.texts()
        self.assertGreater(texts.index(BOOK), texts.index("Waiting on them"))
        self.assertEqual(self.menu(BOOK), ["todo.mine", "todo.edit",
                                           "todo.remove"])
        self.assertEqual(self.asst.tasks(), [])

    def test_the_rest_are_theirs_sorts_all_that_wait(self):
        self.button(self.row(SEND), "Mine").emit("clicked")
        settle()
        self.button(self.page.todo_box, words.HELD_REST).emit("clicked")
        settle()
        self.assertNotIn(words.HELD_HEAD, self.texts())
        self.assertEqual(
            [(t.text, t.owner, t.held) for t in load_todos(self.dir)],
            [(SEND, "you", False), (BOOK, "them", False),
             (REDO, "them", False)],
        )
        self.assertTrue(wait_for(lambda: len(self.asst.tasks()) == 1))

    def menu(self, text: str) -> list[str]:
        secondary = [
            g for g in controllers(self.row(text), Gtk.GestureClick)
            if g.get_button() == Gdk.BUTTON_SECONDARY
        ]
        secondary[0].emit("pressed", 1, 8.0, 8.0)
        model = self.page._row_menus.popover.get_menu_model()
        found: list[str] = []

        def collect(menu: Gio.MenuModel) -> None:
            for i in range(menu.get_n_items()):
                action = menu.get_item_attribute_value(i, "action", None)
                if action is not None:
                    found.append(action.get_string())
                section = menu.get_item_link(i, Gio.MENU_LINK_SECTION)
                if section is not None:
                    collect(section)

        collect(model)
        self.page._row_menus.drop()
        return found

    def test_the_menu_and_its_keys_say_whose_too(self):
        self.assertEqual(
            self.menu(REDO),
            ["todo.mine", "todo.theirs", "todo.edit", "todo.remove"],
        )
        (keys,) = controllers(self.row(REDO), Gtk.ShortcutController)
        shortcuts = {s.get_trigger().to_string(): s for s in keys}
        self.assertLessEqual({"y", "t"}, set(shortcuts))
        shortcuts["t"].get_action().activate(
            Gtk.ShortcutActionFlags.EXCLUSIVE, self.row(REDO), None
        )
        settle()
        self.assertEqual(self.local(REDO).owner, "them")
        self.assertFalse(self.local(REDO).held)


class TestKeptRoom(RoomCase):
    def setUp(self):
        super().setUp()
        self.window.sidebar.select(self.kept)
        settle()
        self.assertEqual(self.page.current.dir, self.kept)

    def test_it_says_nothing_left_and_offers_to_send_it(self):
        self.assertIn("nothing has gone to Deepgram",
                      self.page.summary_label.get_text())
        self.assertTrue(self.page.summarize_button.get_visible())
        self.assertTrue(
            self.page.actions.lookup_action("summarize").get_enabled()
        )

    def test_sending_it_asks_whether_everyone_agreed(self):
        """By its button, which on any other meeting writes at once."""
        asked: list[tuple] = []
        sent: list[Path] = []
        self.page._dialog = lambda *args, **kw: asked.append((args, kw))
        self.page._on_summarize = sent.append
        self.assertEqual(self.page.summarize_button.get_label(),
                         words.ROOM_SEND)
        self.page.summarize_button.emit("clicked")
        ((args, kw),) = asked
        heading, body, confirm, on_confirm = args
        self.assertEqual(heading, words.ROOM_AGREE_HEADING)
        self.assertIn("Deepgram", body)
        self.assertEqual(confirm, words.ROOM_AGREE_CONFIRM)
        self.assertFalse(kw["destructive"])
        self.assertEqual(sent, [])
        on_confirm()
        self.assertEqual(sent, [self.kept])


if __name__ == "__main__":
    unittest.main()
