"""The menu on a to-do row, and the questions its items ask (issue #22).

A row offers Edit, Remove and, when asst holds the task, Open in asst and
Move to list. The page owns what each does; this is the widget side: the
menu model, how it is opened (right-click, long press, Menu and
Shift+F10 on the row that has the focus), and the text and list prompts.
"""
from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

GROUP = "todo"
MENU_KEYS = ("Menu", "<Shift>F10")


def todo_menu_model(filed: bool) -> Gio.Menu:
    """What a row offers. Filed means asst holds the task, which is what
    opening it there, moving it and removing it there need."""
    menu = Gio.Menu()
    own = Gio.Menu()
    own.append("Edit…", f"{GROUP}.edit")
    if filed:
        own.append("Open in asst", f"{GROUP}.open")
        own.append("Move to list…", f"{GROUP}.move")
    menu.append_section(None, own)
    gone = Gio.Menu()
    gone.append("Remove…", f"{GROUP}.remove")
    menu.append_section(None, gone)
    return menu


class RowMenus:
    """The menu of whichever to-do row asked: one popover at a time, and
    none left parented to a row that is about to be rebuilt."""

    def __init__(self) -> None:
        self.popover: Gtk.PopoverMenu | None = None

    def attach(
        self,
        row: Gtk.Widget,
        filed: bool,
        handlers: dict[str, Callable[[], None]],
    ) -> None:
        group = Gio.SimpleActionGroup()
        for name, handler in handlers.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, fn=handler: fn())
            group.add_action(action)
        row.insert_action_group(GROUP, group)

        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect(
            "pressed", lambda g, _n, x, y: self._on_gesture(g, row, filed, x, y)
        )
        row.add_controller(click)
        hold = Gtk.GestureLongPress(touch_only=True)
        hold.connect(
            "pressed", lambda g, x, y: self._on_gesture(g, row, filed, x, y)
        )
        row.add_controller(hold)
        keys = Gtk.ShortcutController()
        for key in MENU_KEYS:
            keys.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(key),
                Gtk.CallbackAction.new(
                    lambda *_a: self.popup(row, filed)
                ),
            ))
        row.add_controller(keys)

    def _on_gesture(self, gesture, row, filed: bool, x: float, y: float) -> None:
        # Claimed, so a touch long press is not also taken for a click.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.popup(row, filed, x, y)

    def popup(
        self, row: Gtk.Widget, filed: bool, x: float | None = None, y: float = 0
    ) -> bool:
        """Open the row's menu at the pointer, or under the row without one."""
        self.drop()
        popover = Gtk.PopoverMenu.new_from_model(todo_menu_model(filed))
        popover.set_parent(row)
        popover.set_has_arrow(False)
        popover.set_halign(Gtk.Align.START)
        if x is not None:
            area = Gdk.Rectangle()
            area.x, area.y, area.width, area.height = int(x), int(y), 1, 1
            popover.set_pointing_to(area)
        popover.connect("closed", self._on_closed)
        self.popover = popover
        popover.popup()
        return True

    def _on_closed(self, popover: Gtk.PopoverMenu) -> None:
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
