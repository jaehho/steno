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

from gi.repository import Gdk, Gtk

from .. import citations
from . import words
from .widgets import wrapping_label

ACTIVATE_KEYS = (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space)


@dataclass(eq=False)
class CiteSource:
    """Something on the page that can say where it was said: a summary
    item, or a to-do."""

    mark: Gtk.Widget  # gets `item-active` while its places are shown
    cites: list[citations.Cite]
    markup: str  # its words, for the bar that lists the places
    # What it is, to find it again after the page is rebuilt.
    ref: tuple[str, object] = field(default=("", None))


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
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.add_css_class("summary-item")
        row.set_margin_start(12 * (block.indent // 2))

        bullet = Gtk.Label(label=block.bullet, xalign=1.0, yalign=0.0)
        bullet.add_css_class("summary")
        bullet.set_width_chars(1 if block.bullet == "•" else 2)
        row.append(bullet)

        cited = bool(block.cites)
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
