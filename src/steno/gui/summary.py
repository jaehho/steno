"""A summary on the page: one row to an item, and a way to ask an item
where in the meeting it was said.

The rows are built from `words.summary_blocks`. An item that carries
cites is a button of sorts: it says how many places it points at, shows a
pointer, takes the keyboard, and when it is chosen the page lights the
transcript lines it came from. Everything else is selectable text as
before, so a bullet can still be copied out of the summary.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
from collections.abc import Callable
from dataclasses import dataclass, field

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk, Pango

from .. import citations
from . import words
from .widgets import wrapping_label

ACTIVATE_KEYS = (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space)
# About three places; a longer list scrolls inside the bar, so that the
# transcript above it keeps most of the window.
MAX_PLACES_HEIGHT = 120


@dataclass(eq=False)
class CiteSource:
    """Something on the page that can say where it was said: a summary
    item, or a to-do."""

    mark: Gtk.Widget  # gets `item-active` while its places are shown
    cites: list[citations.Cite]
    markup: str  # its words, for the bar that lists the places
    # What it is, to find it again after the page is rebuilt.
    ref: tuple[str, object] = field(default=("", None))


class CitedBox(Gtk.Box):
    """A box that is itself what takes the focus.

    GtkBox hands focus to its children, and a cited item's children are
    labels that cannot have it, so Tab would walk past the item. This one
    takes it, once, and the next Tab moves on.
    """

    def do_focus(self, _direction: Gtk.DirectionType) -> bool:
        if self.is_focus() or self.get_focus_child() is not None:
            return False
        return self.grab_focus()


def places_word(count: int) -> str:
    return f"{count} place" + ("" if count == 1 else "s")


def make_citable(
    target: Gtk.Widget, source: CiteSource,
    on_choose: Callable[[CiteSource], None],
) -> None:
    """Make `target` the thing to click, or press Enter on, to see where
    `source` was said."""
    target.add_css_class("cited-item")
    target.set_tooltip_text(
        f"Show where this was said ({places_word(len(source.cites))})"
    )
    target.set_cursor_from_name("pointer")
    target.set_focusable(True)

    click = Gtk.GestureClick()
    click.set_button(Gdk.BUTTON_PRIMARY)

    def released(_gesture, presses: int, _x: float, _y: float) -> None:
        # A double click is two presses; only the first one chooses.
        if presses == 1:
            # Focus comes with the click, so Esc and Enter reach the page from
            # here and not from wherever the focus was (the meeting list).
            target.grab_focus()
            on_choose(source)

    click.connect("released", released)
    target.add_controller(click)

    def pressed(_keys, key: int, _code: int, _state: Gdk.ModifierType) -> bool:
        if key not in ACTIVATE_KEYS:
            return False
        on_choose(source)
        return True

    keys = Gtk.EventControllerKey()
    keys.connect("key-pressed", pressed)
    target.add_controller(keys)


class SummaryView(Gtk.Box):
    """The summary, a row to an item. `sources` are the items that cite."""

    def __init__(self, on_choose: Callable[[CiteSource], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._on_choose = on_choose
        self.sources: list[CiteSource] = []

    def clear(self) -> None:
        while (child := self.get_first_child()) is not None:
            self.remove(child)
        self.sources = []

    def show(self, blocks: list[words.Block]) -> None:
        self.clear()
        for block in blocks:
            self.append(self._row(block))

    def _row(self, block: words.Block) -> Gtk.Widget:
        if block.kind == "item":
            return self._item(block)
        label = wrapping_label()
        label.add_css_class("summary")
        label.set_markup(block.markup)
        if block.kind == "heading":
            # Air above a section, none above the first thing on the page.
            label.set_margin_top(10 if self.get_first_child() else 0)
        return label

    def _item(self, block: words.Block) -> Gtk.Widget:
        cited = bool(block.cites)
        box = CitedBox if cited else Gtk.Box
        row = box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.add_css_class("summary-item")
        row.set_margin_start(12 * (block.indent // 2))

        bullet = Gtk.Label(label=block.bullet, xalign=1.0, yalign=0.0)
        bullet.add_css_class("summary")
        bullet.set_width_chars(1 if block.bullet == "•" else 2)
        row.append(bullet)

        # A cited item is clicked, so its words are not selectable: a
        # selectable label takes the click and shows a text cursor.
        text = wrapping_label(selectable=not cited)
        text.add_css_class("summary")
        text.set_hexpand(True)
        count = len(block.cites)
        text.set_markup(
            block.markup + (
                f' <span size="smaller" alpha="60%" underline="single">'
                f"{count}</span>" if cited else ""
            )
        )
        row.append(text)

        if cited:
            source = CiteSource(row, block.cites, block.markup)
            make_citable(row, source, self._on_choose)
            self.sources.append(source)
        return row


class CiteBar(Gtk.Box):
    """The places a lit item points at, docked above the player.

    Not under the item: the summary and the transcript scroll together,
    so a list placed under the item would be scrolled out of sight by the
    very jump it is for. Here it stays put while the transcript moves, and
    its steps and rows are the way along the places.
    """

    def __init__(
        self,
        on_step: Callable[[int], None],
        on_pick: Callable[[int], None],
        on_close: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("cite-bar")
        self.set_visible(False)
        self._on_pick = on_pick
        self._buttons: list[Gtk.Button] = []

        self.count = Gtk.Label(xalign=0.0)
        self.count.add_css_class("sec")
        self.up = self._tool("go-up-symbolic", "Previous place")
        self.up.connect("clicked", lambda _b: on_step(-1))
        self.down = self._tool("go-down-symbolic", "Next place")
        self.down.connect("clicked", lambda _b: on_step(1))
        self.title = Gtk.Label(xalign=0.0)
        self.title.add_css_class("hint")
        self.title.set_hexpand(True)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        close = self._tool("window-close-symbolic", "Clear  (Esc)")
        close.connect("clicked", lambda _b: on_close())

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for widget in (self.count, self.up, self.down, self.title, close):
            head.append(widget)
        self.append(head)

        self.places = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_propagate_natural_height(True)
        self.scroller.set_max_content_height(MAX_PLACES_HEIGHT)
        self.scroller.set_child(self.places)
        self.append(self.scroller)

    @staticmethod
    def _tool(icon: str, tip: str) -> Gtk.Button:
        button = Gtk.Button.new_from_icon_name(icon)
        button.add_css_class("flat")
        button.set_tooltip_text(tip)
        return button

    def show_places(
        self, title: str, places: list[words.Place], playable: bool
    ) -> None:
        """List `places` for the item whose words are `title` (markup).
        `playable` is whether the meeting still has audio to play from."""
        self.clear()
        self.count.set_text(places_word(len(places)))
        self.title.set_markup(title)
        for index, place in enumerate(places):
            button = self._place_row(place, playable)
            button.connect("clicked", lambda _b, i=index: self._on_pick(i))
            self.places.append(button)
            self._buttons.append(button)
        # One place has nothing to step to.
        self.up.set_visible(len(places) > 1)
        self.down.set_visible(len(places) > 1)
        self.set_visible(True)

    @staticmethod
    def _place_row(place: words.Place, playable: bool) -> Gtk.Button:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        stamp = Gtk.Label(label=place.stamp, xalign=1.0)
        stamp.add_css_class("place-stamp")
        stamp.set_width_chars(5)
        who = Gtk.Label(label=place.speaker, xalign=0.0)
        who.add_css_class("place-who")
        who.set_width_chars(4)
        quote = Gtk.Label(label=f"“{place.quote}”", xalign=0.0)
        quote.set_ellipsize(Pango.EllipsizeMode.END)
        quote.set_hexpand(True)
        for widget in (stamp, who, quote):
            row.append(widget)
        if place.more:
            more = Gtk.Label(
                label=f"+{place.more} more line{'' if place.more == 1 else 's'}"
            )
            more.add_css_class("hint")
            row.append(more)
        if playable:
            row.append(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
        button = Gtk.Button()
        button.set_child(row)
        button.add_css_class("flat")
        button.add_css_class("place")
        button.set_tooltip_text("Play from here" if playable else "Show this place")
        return button

    def set_current(self, index: int) -> None:
        """Mark one place as the one the transcript was last taken to."""
        for i, button in enumerate(self._buttons):
            if i == index:
                button.add_css_class("place-current")
            else:
                button.remove_css_class("place-current")
        self.up.set_sensitive(index > 0)
        self.down.set_sensitive(index < len(self._buttons) - 1)
        if 0 <= index < len(self._buttons):
            self._reveal(self._buttons[index])

    def _reveal(self, button: Gtk.Button) -> None:
        """Scroll the list, if it is long enough to scroll, to a row."""
        # The rows are its direct children, so the allocation is where they
        # sit in it, padding included.
        box = button.get_allocation()
        adjustment = self.scroller.get_vadjustment()
        if box.y < adjustment.get_value():
            adjustment.set_value(box.y)
        elif box.y + box.height > adjustment.get_value() + adjustment.get_page_size():
            adjustment.set_value(box.y + box.height - adjustment.get_page_size())

    def clear(self) -> None:
        while (child := self.places.get_first_child()) is not None:
            self.places.remove(child)
        self._buttons = []

    def hide(self) -> None:
        self.set_visible(False)
        self.clear()
