"""The menu on a to-do row, and the questions its items ask (issue #22).

A row offers Edit, Remove and, when asst holds the task, Open in asst and
Move to list; a row said in a room also offers Mine and Not mine, to say
whose it is, until asst holds it. The page owns what each does; this is the widget side: the
menu model, how it is opened (right-click, long press, the Menu key on
the row that has the focus), a letter for each item, and the text and
list prompts.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

if TYPE_CHECKING:
    from ..notes import Todo

GROUP = "todo"
MENU_KEYS = ("Menu", "<Shift>F10")
# A key for each item on the row that has the focus, as LazyVim's file
# explorer has them (snacks.nvim: a add, r rename, d delete, m move,
# o open), so nothing needs the menu, or a function key, to be reached.
ITEM_KEYS = {"edit": "r", "remove": "d", "move": "m", "open": "o", "add": "a",
             "mine": "y", "theirs": "t"}
# Whose a row is, in menu order: y for yours, t for theirs.
WHOSE = ("mine", "theirs")


def whose_offered(todo: Todo) -> tuple[str, ...]:
    """Which of Mine and Not mine a row offers.

    Both on a row said in a room and not sorted yet; the other one on a
    row the user sorted, to undo it. None on any other row (a call's are
    the summary's to give), none once asst holds the task, nor on a
    closed row: taking a task back out of asst is Remove.
    """
    if todo.uid or not todo.open:
        return ()
    if todo.held:
        return WHOSE
    if todo.owner_by_user:
        return ("mine",) if todo.owner == "them" else ("theirs",)
    return ()


def todo_menu_model(filed: bool, whose: tuple[str, ...] = ()) -> Gio.Menu:
    """What a row offers. Filed means asst holds the task, which is what
    opening it there, moving it and removing it there need. `whose` is
    the Mine and Not mine items it offers (`whose_offered`), first."""
    menu = Gio.Menu()
    if whose:
        sort = Gio.Menu()
        for name in whose:
            sort.append_item(_item("Mine" if name == "mine" else "Not mine", name))
        menu.append_section(None, sort)
    own = Gio.Menu()
    own.append_item(_item("Edit…", "edit"))
    if filed:
        own.append_item(_item("Open in asst", "open"))
        own.append_item(_item("Move to list…", "move"))
    menu.append_section(None, own)
    gone = Gio.Menu()
    gone.append_item(_item("Remove…", "remove"))
    menu.append_section(None, gone)
    return menu


def offered(filed: bool, whose: tuple[str, ...] = ()) -> set[str]:
    """The items a row has: the menu's, and Add, which has no menu item."""
    return (
        {"edit", "remove", "add", *whose}
        | ({"open", "move"} if filed else set())
    )


def _item(label: str, name: str) -> Gio.MenuItem:
    """A menu item that shows the key that does the same."""
    item = Gio.MenuItem.new(label, f"{GROUP}.{name}")
    item.set_attribute_value("accel", GLib.Variant.new_string(ITEM_KEYS[name]))
    return item


def _done(run: Callable[[Callable[[], None]], None], fn: Callable[[], None]) -> bool:
    """Run an item for its key, and say the key was taken."""
    run(fn)
    return True


class RowMenus:
    """The menu of whichever to-do row asked: one popover at a time, and
    none left parented to a row that is about to be rebuilt."""

    def __init__(self) -> None:
        self.popover: Gtk.PopoverMenu | None = None

    def attach(
        self,
        row: Gtk.Widget,
        home: Gtk.Widget,
        filed: bool,
        handlers: dict[str, Callable[[], None]],
    ) -> None:
        """Give `row` its menu. Focus goes to `home`, in the row, when the
        menu closes: a prompt an item asks gives focus back to whatever
        had it when it opened, and the menu's own item will be gone."""
        def run(handler: Callable[[], None]) -> None:
            home.grab_focus()
            handler()

        whose = tuple(name for name in WHOSE if name in handlers)

        group = Gio.SimpleActionGroup()
        for name, handler in handlers.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, fn=handler: run(fn))
            group.add_action(action)
        row.insert_action_group(GROUP, group)

        def at_pointer(gesture, x: float, y: float) -> None:
            # Claimed, so a touch long press is not also taken for a click.
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self.popup(row, home, filed, x, y, whose=whose)

        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect("pressed", lambda g, _n, x, y: at_pointer(g, x, y))
        row.add_controller(click)
        hold = Gtk.GestureLongPress(touch_only=True)
        hold.connect("pressed", at_pointer)
        row.add_controller(hold)
        keys = Gtk.ShortcutController()
        for key in MENU_KEYS:
            keys.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(key),
                Gtk.CallbackAction.new(
                    lambda *_a: self.popup(row, home, filed, whose=whose)
                ),
            ))
        for name, key in ITEM_KEYS.items():
            if name in handlers and name in offered(filed, whose):
                keys.add_shortcut(Gtk.Shortcut.new(
                    Gtk.ShortcutTrigger.parse_string(key),
                    Gtk.CallbackAction.new(
                        lambda *_a, fn=handlers[name]: _done(run, fn)
                    ),
                ))
        row.add_controller(keys)

    def popup(  # noqa: PLR0913 - the row, where, and what it offers
        self,
        row: Gtk.Widget,
        home: Gtk.Widget,
        filed: bool,
        x: float | None = None,
        y: float = 0,
        *,
        whose: tuple[str, ...] = (),
    ) -> bool:
        """Open the row's menu at the pointer, or under the row without one."""
        self.drop()
        popover = Gtk.PopoverMenu.new_from_model(todo_menu_model(filed, whose))
        popover.set_parent(row)
        popover.set_has_arrow(False)
        popover.set_halign(Gtk.Align.START)
        if x is not None:
            area = Gdk.Rectangle()
            area.x, area.y, area.width, area.height = int(x), int(y), 1, 1
            popover.set_pointing_to(area)
        popover.connect("closed", self._on_closed, home)
        self.popover = popover
        popover.popup()
        return True

    def _on_closed(self, popover: Gtk.PopoverMenu, home: Gtk.Widget) -> None:
        # Esc: left inside, focus would move on to the next row or further
        # when the menu is unparented. An item already has it out of here.
        focus = popover.get_root().get_focus()
        if focus is not None and focus.is_ancestor(popover):
            home.grab_focus()
        # Not here: the click that closed it is still delivering its action.
        GLib.idle_add(self.drop, popover)

    def drop(self, popover: Gtk.PopoverMenu | None = None) -> bool:
        popover = popover or self.popover
        if popover is self.popover:
            self.popover = None
        if popover is not None and popover.get_parent() is not None:
            popover.unparent()
        return GLib.SOURCE_REMOVE


def ask_text(  # noqa: PLR0913 - a prompt: where, what it says, what it answers
    parent: Gtk.Widget,
    heading: str,
    body: str,
    on_text: Callable[[str], None],
    *,
    confirm: str,
    initial: str = "",
) -> None:
    """A one-line question; Enter answers it."""
    entry = Gtk.Entry()
    entry.set_text(initial)
    entry.set_width_chars(38)
    entry.set_activates_default(True)
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("go", confirm)
    dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("go")
    dialog.set_close_response("cancel")
    dialog.connect(
        "response",
        lambda _d, response: on_text(entry.get_text()) if response == "go" else None,
    )
    dialog.present(parent)
    entry.grab_focus()


def ask_list(
    parent: Gtk.Widget,
    heading: str,
    names: list[str],
    current: str,
    on_pick: Callable[[str], None],
) -> None:
    """Pick one of `names`, starting on `current`."""
    choice = Gtk.DropDown.new_from_strings(names)
    lowered = [n.lower() for n in names]
    if current.lower() in lowered:
        choice.set_selected(lowered.index(current.lower()))
    dialog = Adw.AlertDialog(heading=heading)
    dialog.set_extra_child(choice)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("go", "Move")
    dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("go")
    dialog.set_close_response("cancel")
    dialog.connect(
        "response",
        lambda _d, response: on_pick(names[choice.get_selected()])
        if response == "go" else None,
    )
    dialog.present(parent)
