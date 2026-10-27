"""GTK checks for the menu on a sidebar row (issue #17).

Run by test_sidebar_menu.py, in a child process on a Broadway display and
never on a real one. The name keeps test discovery from importing this
file in the main process, where `import gi` would connect to the desktop.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from steno.gui.menu import meeting_menu_model, menu_actions
from steno.gui.sidebar import LIVE, Session, Sidebar, row_menu
from steno.gui.window import Window

MEETINGS = {
    "20260110-090000": "Alpha",
    "20260111-090000": "Bravo",
    "20260112-090000": "Charlie",
}
# Newest first, as the sidebar lists them.
ORDER = ["Charlie", "Bravo", "Alpha"]
MENU_KEYS = ("Menu", "<Shift>F10")
EXPECTED = [
    "meeting.copy",
    "meeting.save",
    "meeting.project",
    "meeting.summarize",
    "meeting.realign",
    "meeting.merge",
    "meeting.archive",
    "meeting.delete-audio",
    "meeting.delete",
]
# The oldest meeting has nothing before it to join with.
OLDEST = [name for name in EXPECTED if name != "meeting.merge"]


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


def make_meetings(root: Path) -> None:
    for stamp, title in MEETINGS.items():
        folder = root / stamp
        folder.mkdir(parents=True)
        line = {"speaker": "them", "text": f"hello from {title}", "ts": 1.0}
        (folder / "transcript.jsonl").write_text(json.dumps(line) + "\n")
        (folder / "meta.json").write_text(json.dumps({"title": title}))


def settle() -> None:
    context = GLib.MainContext.default()
    while context.iteration(False):
        pass


def controllers(widget: Gtk.Widget, kind: type) -> list:
    return [c for c in widget.observe_controllers() if isinstance(c, kind)]


def right_click(row: Gtk.ListBoxRow) -> None:
    """The handler the secondary button runs, without a pointer."""
    (click,) = controllers(row, Gtk.GestureClick)
    assert click.get_button() == Gdk.BUTTON_SECONDARY
    click.emit("pressed", 1, 12.0, 8.0)


def menu_shortcuts(sidebar: Sidebar) -> list[Gtk.Shortcut]:
    """The list's shortcuts on the keys that ask for a context menu."""
    found = []
    for keys in controllers(sidebar.list, Gtk.ShortcutController):
        found += [s for s in keys if s.get_trigger().to_string() in MENU_KEYS]
    return found


def press_menu_key(sidebar: Sidebar) -> bool:
    """Run what Menu and Shift+F10 are bound to, without a key event."""
    flags = Gtk.ShortcutActionFlags.EXCLUSIVE
    ran = [
        shortcut.get_action().activate(flags, sidebar.list, None)
        for shortcut in menu_shortcuts(sidebar)
    ]
    return bool(ran) and all(ran)


def labels(menu: Gio.MenuModel) -> list[str]:
    found = []
    for i in range(menu.get_n_items()):
        label = menu.get_item_attribute_value(i, "label", None)
        if label is not None:
            found.append(label.get_string())
        section = menu.get_item_link(i, Gio.MENU_LINK_SECTION)
        if section is not None:
            found.extend(labels(section))
    return found


class TestRowMenu(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make_meetings(self.root)
        self.chosen: list = []
        self.sidebar = Sidebar(
            self.root,
            on_select=self.chosen.append,
            on_status_button=lambda _label: None,
            on_activate=lambda: None,
        )
        self.sidebar.refresh()
        self.rows = [self.sidebar.list.get_row_at_index(i) for i in range(3)]
        # A popover needs a toplevel to open on.
        self.host = Gtk.Window(child=self.sidebar)
        self.host.present()
        settle()

    def tearDown(self):
        self.sidebar._drop_menu()
        self.host.destroy()
        settle()
        self._tmp.cleanup()

    def title(self, index: int) -> str:
        return self.sidebar.items[index].title

    def test_a_past_meeting_offers_the_header_menu(self):
        session = self.sidebar.items[0]
        self.assertIsInstance(session, Session)
        self.assertEqual(menu_actions(row_menu(session)), EXPECTED)
        self.assertEqual(
            labels(row_menu(session)),
            labels(meeting_menu_model(has_previous=True)),
        )

    def test_the_meeting_in_progress_offers_nothing(self):
        self.assertIsNone(row_menu(LIVE))
        self.assertIsNone(row_menu(None))

    def test_right_click_opens_the_clicked_meeting_before_its_menu(self):
        self.assertEqual(self.title(0), "Charlie")
        self.assertIs(self.sidebar.selected, self.sidebar.items[0])

        right_click(self.rows[2])

        self.assertEqual(self.sidebar.selected.title, "Alpha")
        self.assertIs(self.chosen[-1], self.sidebar.items[2])
        menu = self.sidebar._menu
        self.assertIsInstance(menu, Gtk.PopoverMenu)
        self.assertIs(menu.get_parent(), self.rows[2])
        self.assertEqual(menu_actions(menu.get_menu_model()), OLDEST)

    def test_delete_stays_last_in_its_own_section(self):
        right_click(self.rows[1])
        model = self.sidebar._menu.get_menu_model()
        last = model.get_item_link(
            model.get_n_items() - 1, Gio.MENU_LINK_SECTION
        )
        self.assertEqual(
            menu_actions(last), ["meeting.delete-audio", "meeting.delete"]
        )

    def test_right_click_on_the_open_meeting_does_not_reload_it(self):
        right_click(self.rows[0])
        self.assertEqual(self.chosen, [self.sidebar.items[0]])
        self.assertIs(self.sidebar._menu.get_parent(), self.rows[0])

    def test_a_second_menu_replaces_the_first(self):
        right_click(self.rows[1])
        first = self.sidebar._menu
        right_click(self.rows[2])
        self.assertIsNot(self.sidebar._menu, first)
        settle()
        self.assertIsNone(first.get_parent())
        self.assertIs(self.sidebar._menu.get_parent(), self.rows[2])

    def test_rebuilding_the_list_takes_the_menu_down(self):
        right_click(self.rows[1])
        menu = self.sidebar._menu
        self.sidebar.refresh()
        self.assertIsNone(self.sidebar._menu)
        self.assertIsNone(menu.get_parent())

    def test_the_menu_key_and_shift_f10_open_the_selected_meeting(self):
        triggers = [
            s.get_trigger().to_string() for s in menu_shortcuts(self.sidebar)
        ]
        self.assertEqual(triggers, list(MENU_KEYS))

        self.sidebar.list.select_row(self.rows[1])
        self.assertTrue(press_menu_key(self.sidebar))
        self.assertIs(self.sidebar._menu.get_parent(), self.rows[1])
        self.assertEqual(self.sidebar.selected.title, "Bravo")

    def test_the_meeting_in_progress_has_no_menu_to_open(self):
        self.sidebar.set_live(self.root / "20260113-120000", "Zoom")
        self.assertEqual(self.sidebar.items[0], LIVE)
        live = self.sidebar.list.get_row_at_index(0)
        self.assertEqual(controllers(live, Gtk.GestureClick), [])
        self.assertEqual(controllers(live, Gtk.GestureLongPress), [])
        self.assertEqual(self.sidebar.selected, LIVE)

        self.assertFalse(self.sidebar.popup_menu(live))
        self.assertFalse(press_menu_key(self.sidebar))
        self.assertIsNone(self.sidebar._menu)
        self.assertEqual(self.sidebar.selected, LIVE)


class TestSearchKeys(unittest.TestCase):
    """Esc, Enter and the arrows in the search box (issue #20)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        make_meetings(self.root)
        self.chosen: list = []
        self.opened = 0
        self.sidebar = Sidebar(
            self.root,
            on_select=self.chosen.append,
            on_status_button=lambda _label: None,
            on_activate=self._opened,
        )
        self.sidebar.refresh()
        self.host = Gtk.Window(child=self.sidebar)
        self.host.present()
        settle()

    def _opened(self) -> None:
        self.opened += 1

    def tearDown(self):
        self.sidebar._drop_menu()
        self.host.destroy()
        settle()
        self._tmp.cleanup()

    def search(self, text: str) -> None:
        self.sidebar.search.set_text(text)
        # search-changed is delayed by the entry; run what it runs.
        self.sidebar._on_search(self.sidebar.search)
        settle()

    def test_the_first_hit_is_selected_and_the_page_follows(self):
        self.assertEqual(self.sidebar.selected.title, "Charlie")
        self.search("Alpha")
        self.assertEqual(self.sidebar.selected.title, "Alpha")
        self.assertEqual(self.chosen[-1].title, "Alpha")
        row = self.sidebar.list.get_selected_row()
        self.assertIs(row, self.sidebar.list.get_row_at_index(0))

    def test_a_query_with_no_hit_leaves_the_page_alone(self):
        self.search("nothing-says-this")
        self.assertEqual(self.sidebar.items, [])
        self.assertEqual(self.sidebar.selected.title, "Charlie")

    def test_esc_clears_the_text_and_keeps_the_meeting_being_read(self):
        self.search("Alpha")
        self.sidebar.search.emit("stop-search")
        settle()
        self.assertEqual(self.sidebar.search.get_text(), "")
        self.assertEqual(self.sidebar.query, "")
        self.assertEqual(len(self.sidebar.items), 3)
        self.assertEqual(self.sidebar.selected.title, "Alpha")

    def test_esc_on_an_empty_box_focuses_the_selected_row(self):
        self.sidebar.search.grab_focus()
        settle()
        self.sidebar.search.emit("stop-search")
        settle()
        selected = self.sidebar.list.get_selected_row()
        self.assertIs(self.sidebar.list.get_focus_child(), selected)

    def test_enter_selects_the_first_hit_and_opens_the_page(self):
        self.search("hello")
        first = self.sidebar.items[0].title
        self.sidebar.search.emit("activate")
        self.assertEqual(self.sidebar.selected.title, first)
        self.assertEqual(self.opened, 1)

    def test_enter_with_no_hit_does_nothing(self):
        self.search("nothing-says-this")
        self.sidebar.search.emit("activate")
        self.assertEqual(self.opened, 0)

    def test_the_arrows_walk_the_hits_and_stop_at_the_ends(self):
        self.search("hello")  # every meeting says it
        titles = [s.title for s in self.sidebar.items]
        self.assertEqual(titles, ORDER)
        self.assertEqual(self.sidebar.selected.title, "Charlie")
        self.sidebar.search.emit("next-match")
        self.assertEqual(self.sidebar.selected.title, "Bravo")
        self.sidebar.search.emit("next-match")
        self.sidebar.search.emit("next-match")
        self.assertEqual(self.sidebar.selected.title, "Alpha")
        self.sidebar.search.emit("previous-match")
        self.assertEqual(self.sidebar.selected.title, "Bravo")
        self.assertEqual(self.chosen[-1].title, "Bravo")

    def test_down_and_up_keys_in_the_box_walk_the_hits(self):
        self.search("hello")
        self.assertTrue(self.sidebar._on_search_key(None, Gdk.KEY_Down, 0, 0))
        self.assertEqual(self.sidebar.selected.title, "Bravo")
        self.assertTrue(self.sidebar._on_search_key(None, Gdk.KEY_Up, 0, 0))
        self.assertEqual(self.sidebar.selected.title, "Charlie")
        self.assertFalse(self.sidebar._on_search_key(None, Gdk.KEY_a, 0, 0))


class RecordingStartedError(AssertionError):
    """A menu test must never start a recording."""


class StubApp(Adw.Application):
    """Enough of steno's App for the window to be built and looked at."""

    def __init__(self) -> None:
        super().__init__(
            application_id="dev.jaeho.StenoMenuTest",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.status = "idle"
        self.detail = ""
        self.since = 0.0
        self.engine = None
        self.bridge = None
        self.rewriter = None

    def toggle_record(self) -> None:
        raise RecordingStartedError

    stop_record = pause = toggle_record


class TestMenuInTheWindow(unittest.TestCase):
    """The row menu's actions reach the meeting page, on the clicked row."""

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

    def tearDown(self):
        self.sidebar._drop_menu()
        self.window.destroy()
        settle()
        self._tmp.cleanup()

    def row(self, index: int) -> Gtk.ListBoxRow:
        return self.sidebar.list.get_row_at_index(index)

    def test_it_opens_on_the_search_box_not_on_record(self):
        focus = self.window.get_focus()
        self.assertNotIsInstance(focus, Gtk.Button)
        self.assertTrue(
            focus is self.sidebar.search
            or focus.is_ancestor(self.sidebar.search)
        )

    def test_it_is_the_menu_in_the_page_header(self):
        right_click(self.row(1))
        header = self.window.meeting_menu.get_menu_model()
        popup = self.sidebar._menu.get_menu_model()
        self.assertEqual(menu_actions(popup), menu_actions(header))
        self.assertEqual(labels(popup), labels(header))

    def test_every_item_is_an_action_the_page_already_has(self):
        right_click(self.row(1))
        for name in menu_actions(self.sidebar._menu.get_menu_model()):
            group, _, action = name.partition(".")
            self.assertEqual(group, "meeting")
            self.assertIsNotNone(self.page.actions.lookup_action(action))

    def test_an_action_runs_on_the_clicked_meeting_not_the_open_one(self):
        self.assertEqual(self.page.current.title, "Charlie")
        seen: list[str] = []
        with mock.patch(
            "steno.gui.meeting.meeting_markdown",
            side_effect=lambda folder: seen.append(folder.name) or "md",
        ):
            right_click(self.row(2))
            self.assertEqual(self.page.current.title, "Alpha")
            popover = self.sidebar._menu
            self.assertTrue(popover.activate_action("meeting.copy", None))
        self.assertEqual(seen, ["20260110-090000"])

    def test_delete_still_asks_and_then_trashes_the_clicked_meeting(self):
        with (
            mock.patch.object(self.page, "_dialog") as dialog,
            mock.patch("steno.gui.meeting.forget_asst"),
            mock.patch(
                "steno.gui.meeting.delete_session",
                return_value=("trashed", Path("/trash/Bravo")),
            ) as trash,
        ):
            right_click(self.row(1))
            popover = self.sidebar._menu
            self.assertTrue(popover.activate_action("meeting.delete", None))

            dialog.assert_called_once()
            heading, _body, _confirm, confirmed = dialog.call_args.args
            self.assertIn("Bravo", heading)
            trash.assert_not_called()

            confirmed()
            trash.assert_called_once_with(self.root / "20260111-090000")
        left = [s.title for s in self.sidebar.sessions]
        self.assertEqual(left, ["Charlie", "Alpha"])

    def test_closing_the_menu_lets_go_of_the_row(self):
        right_click(self.row(1))
        popover = self.sidebar._menu
        settle()
        popover.popdown()
        settle()
        self.assertIsNone(popover.get_parent())
        self.assertIsNone(self.sidebar._menu)

    def test_menu_key_opens_the_row_that_has_the_focus(self):
        row = self.row(2)
        self.assertTrue(row.grab_focus())
        settle()
        self.assertIs(self.sidebar.list.get_focus_child(), row)

        self.assertTrue(press_menu_key(self.sidebar))
        self.assertIs(self.sidebar._menu.get_parent(), row)
        self.assertEqual(self.page.current.title, "Alpha")


if __name__ == "__main__":
    unittest.main()
