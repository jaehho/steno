"""GTK checks for putting a meeting away from the window (issue #29).

Run by test_archive.py, in a child process on a Broadway display and
never on a real one. The name keeps test discovery from importing this
file in the main process, where `import gi` would connect to the desktop.

A click on a row, a menu item or a toast's button is the signal it
emits, sent without a pointer; what the rest of the window does with it
is the real thing.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk

from steno import notes
from steno.gui.menu import menu_actions
from steno.gui.sidebar import ARCHIVE, LIVE, Session, Sidebar, row_menu
from steno.gui.window import Window
from steno.notes import ensure_todos, load_todos, sync_todos
from steno.summarize import is_archived, load_meta, set_archived
from tests.fakeasst import FakeAsst
from tests.gtk_meeting_scroll import wait_for
from tests.gtk_sidebar_menu import StubApp, right_click, settle

# Newest last. Delta and Alpha are put away.
MEETINGS = {
    "20260110-090000": "Alpha",
    "20260111-090000": "Bravo",
    "20260112-090000": "Charlie",
    "20260113-090000": "Delta",
    "20260114-090000": "Echo",
}
AWAY = ("20260110-090000", "20260113-090000")
PROMISE = """# Alpha

## Action items
**You**
- Send the usage figures by Friday
"""


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


def make_meetings(root: Path, extra: int = 0) -> None:
    """The five meetings, two of them archived, and `extra` older ones."""
    stamps = dict(MEETINGS)
    for i in range(extra):
        stamps[f"20250101-{i:06d}"] = f"Old {i}"
    for stamp, title in stamps.items():
        folder = root / stamp
        folder.mkdir(parents=True)
        line = {"speaker": "them", "text": f"hello from {title}", "ts": 1.0}
        (folder / "transcript.jsonl").write_text(json.dumps(line) + "\n")
        (folder / "meta.json").write_text(
            json.dumps({"title": title, "project": "thesis"})
        )
    for stamp in AWAY:
        set_archived(root / stamp, True)
    (root / AWAY[0] / "summary.md").write_text(PROMISE)


def titles(sidebar: Sidebar) -> list[str]:
    """The list as it reads: titles, with the archive's row as ARCHIVE."""
    return [
        item.title if isinstance(item, Session) else item
        for item in sidebar.items
    ]


def toggle_label(sidebar: Sidebar) -> str:
    row = sidebar._archive_row_widget()
    return row.get_child().get_first_child().get_label()


def row_of(sidebar: Sidebar, title: str) -> Gtk.ListBoxRow:
    index = titles(sidebar).index(title)
    return sidebar.list.get_row_at_index(index)


def session_of(sidebar: Sidebar, title: str) -> Session:
    return next(s for s in sidebar.sessions if s.title == title)


def open_the_archive(sidebar: Sidebar) -> None:
    sidebar.list.emit("row-activated", sidebar._archive_row_widget())
    settle()


class TestTheList(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make_meetings(self.root)
        self.chosen: list = []
        self.activated = 0
        self.sidebar = Sidebar(
            self.root,
            on_select=self.chosen.append,
            on_status_button=lambda _label: None,
            on_activate=self.count_activation,
        )
        self.sidebar.refresh()
        self.host = Gtk.Window(child=self.sidebar)
        self.host.set_default_size(300, 520)
        self.host.present()
        settle()

    def tearDown(self):
        self.sidebar._drop_menu()
        self.host.destroy()
        settle()
        self._tmp.cleanup()

    def count_activation(self) -> None:
        self.activated += 1

    def archive(self, title: str, archived: bool = True) -> Session:
        """What the meeting page does: write the flag, tell the list."""
        s = session_of(self.sidebar, title)
        set_archived(s.dir, archived)
        self.sidebar.update_session(s)
        return s

    def test_the_archived_are_not_in_the_list_until_asked_for(self):
        self.assertEqual(titles(self.sidebar), ["Echo", "Charlie", "Bravo", ARCHIVE])
        self.assertEqual(toggle_label(self.sidebar), "Archived (2)")
        self.assertIs(self.sidebar.items[0], self.chosen[0])

    def test_that_row_is_not_a_meeting(self):
        row = self.sidebar._archive_row_widget()
        self.assertFalse(row.get_selectable())
        self.assertIsNone(row_menu(ARCHIVE))
        self.assertEqual(self.sidebar.items[-1], ARCHIVE)
        self.assertFalse(self.sidebar.popup_menu(row))
        self.sidebar.list.select_row(row)
        self.assertEqual(len(self.chosen), 1)
        self.assertEqual(self.sidebar.selected.title, "Echo")

    def test_activating_it_opens_the_archive_and_again_closes_it(self):
        open_the_archive(self.sidebar)
        self.assertEqual(
            titles(self.sidebar),
            ["Echo", "Charlie", "Bravo", ARCHIVE, "Delta", "Alpha"],
        )
        for title in ("Delta", "Alpha"):
            self.assertTrue(
                row_of(self.sidebar, title).has_css_class("session-archived")
            )
        for title in ("Echo", "Charlie", "Bravo"):
            self.assertFalse(
                row_of(self.sidebar, title).has_css_class("session-archived")
            )
        # The meeting that was open stays open: no second selection, and
        # the click did not take a narrow window to the page.
        self.assertEqual(len(self.chosen), 1)
        self.assertEqual(self.activated, 0)

        open_the_archive(self.sidebar)
        self.assertEqual(titles(self.sidebar), ["Echo", "Charlie", "Bravo", ARCHIVE])
        self.assertEqual(len(self.chosen), 1)

    def test_a_meeting_activated_still_takes_a_narrow_window_to_its_page(self):
        self.sidebar.list.emit("row-activated", row_of(self.sidebar, "Bravo"))
        self.assertEqual(self.activated, 1)

    def test_the_row_is_the_heading_of_what_it_opens(self):
        open_the_archive(self.sidebar)
        headers = {
            t: row_of(self.sidebar, t).get_header() for t in titles(self.sidebar)
        }
        self.assertIsNone(headers[ARCHIVE])
        self.assertIsNone(headers["Delta"])
        self.assertIsNone(headers["Alpha"])
        self.assertEqual(headers["Echo"].get_label(), "Earlier")
        self.assertIsNone(headers["Charlie"])

    def test_the_row_keeps_the_keyboard_focus_where_it_was_pressed(self):
        open_the_archive(self.sidebar)
        row = self.sidebar._archive_row_widget()
        self.assertIs(self.sidebar.list.get_focus_child(), row)

    def test_a_search_counts_what_it_found_in_the_archive_and_hides_it(self):
        self.sidebar.search.set_text("alpha")
        self.sidebar.search.emit("search-changed")
        self.assertEqual(titles(self.sidebar), [ARCHIVE])
        self.assertEqual(toggle_label(self.sidebar), "Archived (1)")
        open_the_archive(self.sidebar)
        self.assertEqual(titles(self.sidebar), [ARCHIVE, "Alpha"])

        self.sidebar.search.set_text("echo")
        self.sidebar.search.emit("search-changed")
        self.assertEqual(titles(self.sidebar), ["Echo"])

    def test_asking_for_a_meeting_in_the_archive_opens_the_archive(self):
        alpha = session_of(self.sidebar, "Alpha")
        self.sidebar.select(alpha.dir)
        self.assertTrue(self.sidebar.show_archived)
        self.assertIs(self.sidebar.selected, alpha)
        self.assertIs(self.chosen[-1], alpha)

    def test_so_does_the_list_when_it_is_told_which_to_show(self):
        delta = session_of(self.sidebar, "Delta")
        self.sidebar.refresh(select=delta.dir)
        self.assertTrue(self.sidebar.show_archived)
        self.assertEqual(self.sidebar.selected.title, "Delta")
        self.assertEqual(self.chosen[-1].title, "Delta")

    def test_a_refresh_does_not_open_it_for_the_meeting_that_is_open(self):
        self.sidebar.select(session_of(self.sidebar, "Alpha").dir)
        open_the_archive(self.sidebar)
        self.assertFalse(self.sidebar.show_archived)
        self.sidebar.refresh()
        self.assertFalse(self.sidebar.show_archived)
        self.assertEqual(self.sidebar.selected.title, "Alpha")

    def test_closing_the_archive_leaves_its_open_meeting_on_the_page(self):
        self.sidebar.select(session_of(self.sidebar, "Alpha").dir)
        count = len(self.chosen)
        open_the_archive(self.sidebar)
        self.assertEqual(titles(self.sidebar), ["Echo", "Charlie", "Bravo", ARCHIVE])
        self.assertEqual(self.sidebar.selected.title, "Alpha")
        self.assertEqual(len(self.chosen), count)
        self.assertIsNone(self.sidebar.list.get_selected_row())

    def test_closing_it_with_nothing_else_in_the_list_keeps_the_page_too(self):
        for title in ("Echo", "Charlie", "Bravo"):
            self.archive(title)
        self.sidebar.select(session_of(self.sidebar, "Alpha").dir)
        count = len(self.chosen)
        open_the_archive(self.sidebar)
        self.assertEqual(titles(self.sidebar), [ARCHIVE])
        self.assertEqual(self.sidebar.selected.title, "Alpha")
        self.assertEqual(len(self.chosen), count)

    def test_the_only_match_of_a_search_put_away_leaves_the_empty_page(self):
        self.sidebar.search.set_text("bravo")
        self.sidebar.search.emit("search-changed")
        self.sidebar.select(session_of(self.sidebar, "Bravo").dir)
        self.assertEqual(titles(self.sidebar), ["Bravo"])
        self.archive("Bravo")
        self.assertIsNone(self.chosen[-1])
        self.assertIsNone(self.sidebar.selected)

    def test_putting_the_open_meeting_away_opens_the_one_below_it(self):
        self.sidebar.select(session_of(self.sidebar, "Charlie").dir)
        self.archive("Charlie")
        self.assertEqual(titles(self.sidebar), ["Echo", "Bravo", ARCHIVE])
        self.assertEqual(self.chosen[-1].title, "Bravo")
        self.assertEqual(self.sidebar.selected.title, "Bravo")
        self.assertEqual(toggle_label(self.sidebar), "Archived (3)")

    def test_the_newest_one_gives_way_to_the_next(self):
        self.archive("Echo")
        self.assertEqual(self.chosen[-1].title, "Charlie")

    def test_the_last_one_leaves_the_empty_page(self):
        for title in ("Echo", "Charlie", "Bravo"):
            self.archive(title)
        self.assertEqual(titles(self.sidebar), [ARCHIVE])
        self.assertIsNone(self.chosen[-1])
        self.assertIsNone(self.sidebar.selected)
        self.assertEqual(toggle_label(self.sidebar), "Archived (5)")
        self.assertEqual(self.sidebar.archived_count(), 5)

    def test_an_archive_that_is_open_keeps_the_page_where_it_is(self):
        open_the_archive(self.sidebar)
        count = len(self.chosen)
        self.archive("Echo")
        self.assertEqual(
            titles(self.sidebar),
            ["Charlie", "Bravo", ARCHIVE, "Echo", "Delta", "Alpha"],
        )
        self.assertEqual(self.sidebar.selected.title, "Echo")
        self.assertEqual(len(self.chosen), count)
        selected = self.sidebar.list.get_selected_row()
        self.assertIs(selected, row_of(self.sidebar, "Echo"))

    def test_bringing_one_back_keeps_it_open_in_the_list(self):
        self.sidebar.select(session_of(self.sidebar, "Delta").dir)
        count = len(self.chosen)
        self.archive("Delta", archived=False)
        # The archive stays open, with what is left in it.
        self.assertEqual(
            titles(self.sidebar),
            ["Echo", "Delta", "Charlie", "Bravo", ARCHIVE, "Alpha"],
        )
        self.assertEqual(toggle_label(self.sidebar), "Archived (1)")
        self.assertEqual(self.sidebar.selected.title, "Delta")
        self.assertEqual(len(self.chosen), count)
        self.assertIs(
            self.sidebar.list.get_selected_row(), row_of(self.sidebar, "Delta")
        )

    def test_the_row_goes_when_nothing_is_left_in_the_archive(self):
        self.sidebar.select(session_of(self.sidebar, "Delta").dir)
        self.archive("Delta", archived=False)
        self.archive("Alpha", archived=False)
        self.assertNotIn(ARCHIVE, self.sidebar.items)
        self.assertEqual(
            titles(self.sidebar),
            ["Echo", "Delta", "Charlie", "Bravo", "Alpha"],
        )

    def test_deleting_a_meeting_does_not_open_the_row_that_opens_the_archive(self):
        # Only the archive is left below the last meeting.
        for title in ("Echo", "Charlie"):
            self.archive(title)
        self.sidebar.select(session_of(self.sidebar, "Bravo").dir)
        self.sidebar.forget(session_of(self.sidebar, "Bravo").dir)
        self.assertIsNone(self.chosen[-1])
        self.assertEqual(titles(self.sidebar), [ARCHIVE])

    def test_a_terminal_archiving_shows_when_the_window_is_raised(self):
        self.assertFalse(self.sidebar.refresh_if_stale())
        set_archived(self.root / "20260111-090000", True)
        self.assertTrue(self.sidebar.refresh_if_stale())
        self.assertEqual(titles(self.sidebar), ["Echo", "Charlie", ARCHIVE])
        self.assertEqual(toggle_label(self.sidebar), "Archived (3)")
        self.assertFalse(self.sidebar.refresh_if_stale())

    def test_a_terminal_undoing_the_windows_own_change_shows_too(self):
        for title, archived in (("Bravo", True), ("Delta", False)):
            with self.subTest(title=title):
                s = self.archive(title, archived)
                # The window's own change is not news to it.
                self.assertFalse(self.sidebar.refresh_if_stale())
                set_archived(s.dir, not archived)
                self.assertTrue(self.sidebar.refresh_if_stale())
                self.assertEqual(
                    session_of(self.sidebar, title).archived, not archived
                )
                self.assertFalse(self.sidebar.refresh_if_stale())

    def test_a_summary_rewritten_meanwhile_is_still_news_after_it(self):
        bravo = session_of(self.sidebar, "Bravo")
        (bravo.dir / "summary.md").write_text("# Bravo\n\nRewritten.\n")
        self.archive("Bravo")
        self.assertTrue(self.sidebar.refresh_if_stale())

    def test_the_open_to_dos_of_a_meeting_put_away_are_not_counted(self):
        alpha = session_of(self.sidebar, "Alpha")
        self.assertEqual(len(ensure_todos(alpha.dir)), 1)
        self.sidebar.refresh()
        self.assertEqual(session_of(self.sidebar, "Alpha").open_todos, 0)
        self.assertFalse(self.sidebar.owed.get_visible())
        self.assertIsNone(self.sidebar.owed.get_tooltip_text())

    def test_bringing_it_back_counts_them_again(self):
        ensure_todos(session_of(self.sidebar, "Alpha").dir)
        self.archive("Alpha", archived=False)
        self.assertEqual(self.sidebar.owed.get_label(), "1 to-do open")
        self.assertTrue(self.sidebar.owed.get_visible())

    def test_the_list_of_what_is_owed_leaves_the_archive_out(self):
        ensure_todos(session_of(self.sidebar, "Alpha").dir)
        self.sidebar.refresh()
        self.assertEqual(self.owed_heads(), [])

        self.archive("Alpha", archived=False)
        (head,) = self.owed_heads()
        self.assertIn("Alpha", head)
        self.assertNotIn("archived", head)
        self.sidebar._open_owed(session_of(self.sidebar, "Alpha").dir)
        self.assertEqual(self.sidebar.selected.title, "Alpha")

    def owed_heads(self) -> list[str]:
        self.sidebar._fill_owed()
        heads = []
        child = self.sidebar._owed_box.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Label):
                heads.append(child.get_label())
            child = child.get_next_sibling()
        return heads

    def test_the_footer_counts_every_meeting_it_has(self):
        self.assertEqual(
            self.sidebar.footer.get_text().split(" · ")[0], "5 meetings"
        )


class TestTheListScrolls(unittest.TestCase):
    """Opening the archive at the bottom of a long list shows what opened."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make_meetings(self.root, extra=40)
        self.sidebar = Sidebar(
            self.root,
            on_select=lambda _item: None,
            on_status_button=lambda _label: None,
            on_activate=lambda: None,
        )
        self.sidebar.refresh()
        self.host = Gtk.Window(child=self.sidebar)
        self.host.set_default_size(300, 520)
        self.host.present()
        settle()

    def tearDown(self):
        self.host.destroy()
        settle()
        self._tmp.cleanup()

    def test_the_rows_that_opened_are_in_view(self):
        adj = self.sidebar.scroll.get_vadjustment()
        self.assertGreater(adj.get_upper(), adj.get_page_size())
        self.assertEqual(adj.get_value(), 0)

        open_the_archive(self.sidebar)
        delta = row_of(self.sidebar, "Delta")
        self.assertTrue(wait_for(lambda: adj.get_value() > 0))

        top = delta.translate_coordinates(self.sidebar.list, 0, 0)[1]
        self.assertGreaterEqual(top, adj.get_value())
        self.assertLessEqual(
            top + delta.get_height(), adj.get_value() + adj.get_page_size()
        )

    def test_closing_it_does_not_scroll_anywhere_it_was_not_sent(self):
        open_the_archive(self.sidebar)
        adj = self.sidebar.scroll.get_vadjustment()
        wait_for(lambda: adj.get_value() > 0)
        open_the_archive(self.sidebar)
        # The list is shorter; the adjustment keeps within it.
        self.assertLessEqual(
            adj.get_value(), adj.get_upper() - adj.get_page_size()
        )


class TestInTheWindow(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make_meetings(self.root)
        self.app = StubApp()
        self.app.register(None)
        self.window = Window(self.app, self.root)
        self.window.present()
        settle()
        self.sidebar = self.window.sidebar
        self.page = self.window.meeting
        self.toasts: list[tuple[str, dict]] = []
        self.page._toast = lambda text, **kw: self.toasts.append((text, kw))

    def tearDown(self):
        self.sidebar._drop_menu()
        self.window.destroy()
        settle()
        self._tmp.cleanup()

    def folder(self, title: str) -> Path:
        stamp = next(k for k, v in MEETINGS.items() if v == title)
        return self.root / stamp

    def run_action(self, name: str) -> None:
        self.page.actions.activate_action(name, None)
        settle()

    def open(self, title: str) -> None:
        self.sidebar.select(self.folder(title))
        settle()
        self.assertEqual(self.page.current.title, title)

    def model_actions(self) -> list[str]:
        return menu_actions(self.window.meeting_menu.get_menu_model())

    def test_a_meeting_is_offered_archive_and_an_archived_one_unarchive(self):
        self.open("Charlie")
        self.assertIn("meeting.archive", self.model_actions())
        self.assertNotIn("meeting.unarchive", self.model_actions())
        self.assertNotIn("Archived", self.window.page_subtitle.get_text())

        self.open("Alpha")
        self.assertIn("meeting.unarchive", self.model_actions())
        self.assertNotIn("meeting.archive", self.model_actions())
        self.assertIn("Archived", self.window.page_subtitle.get_text())

        self.open("Echo")
        self.assertIn("meeting.archive", self.model_actions())

    def test_the_menu_of_a_row_does_the_same(self):
        self.assertIn(
            "meeting.archive",
            menu_actions(row_menu(session_of(self.sidebar, "Echo"))),
        )
        self.assertIn(
            "meeting.unarchive",
            menu_actions(row_menu(session_of(self.sidebar, "Delta"))),
        )

    def test_the_header_keeps_its_menu_while_it_is_open(self):
        """The window refreshes the header every second; a new model
        every time would close a menu that someone is reading."""
        self.open("Charlie")
        model = self.window.meeting_menu.get_menu_model()
        for _ in range(3):
            self.window._update_header()
        self.assertIs(self.window.meeting_menu.get_menu_model(), model)

    def test_archive_in_a_rows_menu_puts_that_meeting_away_without_asking(self):
        before = load_meta(self.folder("Bravo"))
        right_click(self.sidebar.list.get_row_at_index(2))
        self.assertEqual(self.page.current.title, "Bravo")
        with mock.patch.object(self.page, "_dialog") as dialog:
            popover = self.sidebar._menu
            self.assertTrue(popover.activate_action("meeting.archive", None))
            settle()
        dialog.assert_not_called()

        meta = load_meta(self.folder("Bravo"))
        self.assertTrue(is_archived(self.folder("Bravo")))
        self.assertIsInstance(meta["archived_at"], float)
        for key, value in before.items():
            self.assertEqual(meta[key], value)
        self.assertEqual(titles(self.sidebar), ["Echo", "Charlie", ARCHIVE])
        self.assertEqual(self.page.current.title, "Charlie")
        self.assertEqual(self.sidebar.selected.title, "Charlie")
        self.assertEqual(self.window.stack.get_visible_child_name(), "meeting")

    def test_the_toast_says_so_and_takes_it_back(self):
        self.open("Bravo")
        self.run_action("archive")
        (text, kw), = self.toasts
        self.assertIn("Bravo", text)
        self.assertEqual(titles(self.sidebar), ["Echo", "Charlie", ARCHIVE])

        kw["undo"]()
        settle()
        self.assertFalse(is_archived(self.folder("Bravo")))
        self.assertNotIn("archived_at", load_meta(self.folder("Bravo")))
        self.assertEqual(
            titles(self.sidebar), ["Echo", "Charlie", "Bravo", ARCHIVE]
        )
        self.assertEqual(self.page.current.title, "Bravo")
        self.assertEqual(self.sidebar.selected.title, "Bravo")
        self.assertNotIn("meeting.unarchive", self.model_actions())

    def test_an_undo_for_a_meeting_deleted_since_leaves_the_list_alone(self):
        self.open("Bravo")
        self.run_action("archive")
        undo = self.toasts[-1][1]["undo"]
        self.open("Bravo")
        with mock.patch("steno.gui.meeting.forget_asst"):
            self.page._delete(self.page.current)
        settle()
        before = (titles(self.sidebar), self.sidebar.selected)

        undo()
        settle()
        self.assertFalse(self.folder("Bravo").exists())
        self.assertEqual((titles(self.sidebar), self.sidebar.selected), before)
        self.assertIs(
            self.sidebar.list.get_selected_row(),
            row_of(self.sidebar, self.sidebar.selected.title),
        )

    def test_the_toast_the_window_makes_has_a_working_undo_button(self):
        made: list[Adw.Toast] = []
        real = Adw.Toast.new

        def new(text):
            made.append(real(text))
            return made[-1]

        undone: list[int] = []
        with mock.patch.object(Adw.Toast, "new", side_effect=new):
            self.window._toast("Archived", undo=lambda: undone.append(1))
            self.window._toast("Summary ready", open_session=self.root)
            self.window._toast("Plain")
        archived, opened, plain = made
        self.assertEqual(archived.get_button_label(), "Undo")
        archived.emit("button-clicked")
        self.assertEqual(undone, [1])
        self.assertEqual(opened.get_button_label(), "Open")
        self.assertIsNone(plain.get_button_label())

    def fake_asst(self) -> FakeAsst:
        asst = FakeAsst(lists=("thesis",)).start()
        self.addCleanup(asst.stop)
        return asst

    def open_with_filed_to_dos(self, asst: FakeAsst) -> None:
        """Alpha, brought back and its one promise filed in `asst`."""
        self.open("Alpha")
        self.run_action("unarchive")
        sync_todos(self.folder("Alpha"), PROMISE)
        self.assertEqual(len(asst.tasks()), 1)
        self.page.load(self.page.current)
        settle()

    def todo_rows(self) -> list[Gtk.Widget]:
        rows = []
        child = self.page.todo_box.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Box):
                rows.append(child)
            child = child.get_next_sibling()
        return rows

    def test_the_toast_says_what_it_took_out_of_asst(self):
        asst = self.fake_asst()
        self.open_with_filed_to_dos(asst)
        self.run_action("archive")
        self.assertTrue(wait_for(lambda: self.toasts))
        self.assertEqual(
            self.toasts[-1][0],
            "Archived “Alpha”. Removed 1 open task from asst.",
        )
        self.assertEqual(asst.tasks(), [])
        rows = load_todos(self.folder("Alpha"))
        self.assertTrue(all(t.dropped for t in rows))

    def test_the_page_does_not_wait_for_asst(self):
        asst = self.fake_asst()
        self.open_with_filed_to_dos(asst)
        release = threading.Event()
        real = notes.drop_open_todos

        def slow(session_dir):
            release.wait(5)
            return real(session_dir)

        with mock.patch("steno.gui.meeting.drop_open_todos", slow):
            self.run_action("archive")
            # Put away, and the list shows it, with asst still thinking.
            self.assertTrue(is_archived(self.folder("Alpha")))
            self.assertEqual(self.toasts, [])
            release.set()
            self.assertTrue(wait_for(lambda: self.toasts))
        self.assertEqual(asst.tasks(), [])

    def test_the_page_of_a_meeting_put_away_has_nothing_open_on_it(self):
        asst = self.fake_asst()
        self.open_with_filed_to_dos(asst)
        (row,) = self.todo_rows()
        self.assertTrue(row.get_first_child().get_sensitive())
        self.assertEqual(self.page.todo_count.get_text(), "1 to do")

        self.run_action("archive")
        self.assertTrue(wait_for(lambda: self.toasts))
        self.sidebar.select(self.folder("Alpha"))
        settle()
        (row,) = self.todo_rows()
        self.assertFalse(row.get_first_child().get_sensitive())
        self.assertEqual(self.page.todo_count.get_text(), "")
        self.assertFalse(self.sidebar.owed.get_visible())

    def test_a_task_asst_kept_is_said_in_the_toast(self):
        asst = self.fake_asst()
        self.open_with_filed_to_dos(asst)
        asst.down = True
        self.run_action("archive")
        self.assertTrue(wait_for(lambda: self.toasts))
        self.assertTrue(is_archived(self.folder("Alpha")))
        self.assertEqual(
            self.toasts[-1][0],
            "Archived “Alpha”. Could not remove 1 open task from asst; "
            "it stays open there.",
        )
        self.assertFalse(self.sidebar.owed.get_visible())

    def test_undo_opens_the_to_dos_and_files_them_again(self):
        asst = self.fake_asst()
        self.open_with_filed_to_dos(asst)
        self.run_action("archive")
        self.assertTrue(wait_for(lambda: self.toasts))
        undo = self.toasts[-1][1]["undo"]
        self.toasts.clear()

        undo()
        self.assertTrue(wait_for(lambda: self.toasts))
        self.assertEqual(
            self.toasts[-1][0],
            "Unarchived “Alpha”. Filed 1 open task in asst again.",
        )
        self.assertFalse(is_archived(self.folder("Alpha")))
        self.assertEqual(len(asst.tasks()), 1)
        (todo,) = load_todos(self.folder("Alpha"))
        self.assertTrue(todo.open)
        self.assertEqual(todo.uid, asst.tasks()[0]["id"])
        self.assertEqual(self.sidebar.owed.get_label(), "1 to-do open")

    def test_unarchive_in_the_menu_does_the_same_and_twice_does_no_harm(self):
        asst = self.fake_asst()
        self.open_with_filed_to_dos(asst)
        self.run_action("archive")
        self.assertTrue(wait_for(lambda: self.toasts))
        self.toasts.clear()
        self.open("Alpha")
        self.run_action("unarchive")
        self.assertTrue(wait_for(lambda: self.toasts))
        self.assertIn("Filed 1 open task", self.toasts[-1][0])

        self.toasts.clear()
        self.page._bring_back(self.page.current)
        settle()
        wait_for(lambda: False, 0.3)
        self.assertEqual(self.toasts, [])
        self.assertEqual(len(asst.tasks()), 1)

    def test_unarchive_keeps_the_open_meeting_open(self):
        self.open("Delta")
        self.assertTrue(self.sidebar.show_archived)
        self.run_action("unarchive")
        self.assertFalse(is_archived(self.folder("Delta")))
        self.assertEqual(self.page.current.title, "Delta")
        self.assertEqual(
            titles(self.sidebar),
            ["Echo", "Delta", "Charlie", "Bravo", ARCHIVE, "Alpha"],
        )
        self.assertIn("meeting.archive", self.model_actions())
        self.assertTrue(self.page.actions.lookup_action("archive").get_enabled())
        self.assertFalse(
            self.page.actions.lookup_action("unarchive").get_enabled()
        )

    def test_each_action_is_on_only_for_the_state_it_changes(self):
        self.open("Charlie")
        archive = self.page.actions.lookup_action("archive")
        unarchive = self.page.actions.lookup_action("unarchive")
        self.assertEqual((archive.get_enabled(), unarchive.get_enabled()), (True, False))
        self.open("Alpha")
        self.assertEqual((archive.get_enabled(), unarchive.get_enabled()), (False, True))

    def test_putting_away_every_meeting_ends_on_a_page_that_says_where_they_went(self):
        for title in ("Echo", "Charlie", "Bravo"):
            self.open(title)
            self.run_action("archive")
        self.assertEqual(self.window.stack.get_visible_child_name(), "empty")
        self.assertIsNone(self.sidebar.selected)
        self.assertEqual(self.window.empty.get_title(), "Nothing open")
        self.assertIn("5 archived", self.window.empty.get_description())
        self.assertFalse(self.window.meeting_menu.get_visible())

    def test_the_last_one_put_away_takes_its_audio_with_it(self):
        """Its player buttons are on the page that has just gone."""
        for title in ("Echo", "Charlie"):
            self.open(title)
            self.run_action("archive")
        self.open("Bravo")
        stopped: list[int] = []
        player = SimpleNamespace(stop=lambda: stopped.append(1))
        with mock.patch.object(self.page, "player", player):
            self.run_action("archive")
        self.assertEqual(self.window.stack.get_visible_child_name(), "empty")
        self.assertTrue(stopped)
        self.assertIsNone(self.page.current)

    def test_a_list_with_nothing_put_away_still_says_no_meetings_yet(self):
        for title in ("Delta", "Alpha"):
            self.open(title)
            self.run_action("unarchive")
        for title in ("Echo", "Delta", "Charlie", "Bravo", "Alpha"):
            self.sidebar.forget(self.folder(title))
        self.assertEqual(self.window.empty.get_title(), "No meetings yet")

    def test_the_meeting_being_recorded_cannot_be_put_away(self):
        folder = self.folder("Bravo")
        self.app.engine = SimpleNamespace(
            current=SimpleNamespace(session_dir=folder)
        )
        self.addCleanup(setattr, self.app, "engine", None)
        self.open("Bravo")

        archive = self.page.actions.lookup_action("archive")
        self.assertFalse(archive.get_enabled())
        self.run_action("archive")
        self.page._archive()
        self.assertFalse(is_archived(folder))
        self.assertEqual(self.toasts, [])
        self.assertEqual(self.page.current.title, "Bravo")

        # The item is there but cannot be pressed.
        right_click(self.sidebar.list.get_row_at_index(2))
        button = model_button(self.sidebar._menu, "Archive")
        self.assertFalse(button.is_sensitive())
        self.assertTrue(model_button(self.sidebar._menu, "Summarize again...").is_sensitive())

        # Any other meeting can still be.
        self.sidebar._drop_menu()
        self.open("Charlie")
        self.assertTrue(archive.get_enabled())

    def test_the_item_of_a_meeting_that_can_be_put_away_can_be_pressed(self):
        right_click(self.sidebar.list.get_row_at_index(1))
        self.assertTrue(model_button(self.sidebar._menu, "Archive").is_sensitive())

    def test_a_meeting_put_away_by_a_terminal_is_still_on_the_page_it_was_on(self):
        self.open("Charlie")
        set_archived(self.folder("Charlie"), True)
        self.window.refresh_if_stale()
        settle()
        self.assertEqual(self.page.current.title, "Charlie")
        self.assertIn("meeting.unarchive", self.model_actions())
        self.assertIn("Archived", self.window.page_subtitle.get_text())
        self.assertNotIn("Charlie", titles(self.sidebar))

    def test_a_notification_for_an_archived_meeting_opens_it(self):
        self.window.show_session(self.folder("Alpha"))
        settle()
        self.assertEqual(self.page.current.title, "Alpha")
        self.assertTrue(self.sidebar.show_archived)

    def test_the_live_row_is_not_confused_with_the_archive(self):
        self.sidebar.set_live(self.root / "20260115-120000", "Zoom")
        self.assertEqual(self.sidebar.items[0], LIVE)
        self.assertEqual(self.sidebar.selected, LIVE)
        self.assertEqual(
            titles(self.sidebar)[1:], ["Echo", "Charlie", "Bravo", ARCHIVE]
        )


def model_button(popover: Gtk.PopoverMenu, label: str) -> Gtk.Widget:
    """The button a menu shows for an item, by what it says."""
    found = []

    def walk(widget: Gtk.Widget) -> None:
        child = widget.get_first_child()
        while child is not None:
            if type(child).__name__ == "GtkModelButton":
                texts = [
                    w.get_label() for w in descendants(child)
                    if isinstance(w, Gtk.Label)
                ]
                if label in texts:
                    found.append(child)
            walk(child)
            child = child.get_next_sibling()

    walk(popover)
    assert len(found) == 1, (label, found)
    return found[0]


def descendants(widget: Gtk.Widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from descendants(child)
        child = child.get_next_sibling()


if __name__ == "__main__":
    unittest.main()
