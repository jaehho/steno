"""Which model answers a question, and the control to change it.

The pick is a window preference (`prefs.ask_model`) and `STENO_ASK_MODEL`
overrides it; with neither, asking uses the summary's model. The control
names the one in force and opens a popover with the choices: the default,
the provider's presets, its own model list when that can be fetched, and
a model typed by hand.

The list is a plain GET to the provider, made once per run, on a thread,
when the popover is first opened. Until it arrives (or if it never does)
the presets are all there is, and nothing says it failed.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import asyncio
import os
import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk, Pango

from .. import ASK_MODEL_ENV, ask_model, claude, model_default
from . import prefs

# Rows past which a filter box is worth its space.
FILTER_AFTER = 10


def choice_rows(
    pick: str, default: str, presets: list[str], listed: list[str]
) -> list[tuple[str, str]]:
    """(value, label) for each choice. The value saved for the default is "".

    The pick is always among them, even when nothing else lists it, so the
    row that is in force can be seen.
    """
    rows = [("", f"Default · {default}")]
    seen = {""}
    for model in [pick, *presets, *listed]:
        if model not in seen:
            seen.add(model)
            rows.append((model, model))
    return rows


def fetch_models() -> list[str]:
    """The provider's model list. Blocks, so never on the GTK thread."""
    return asyncio.run(claude.list_models())


class ModelList:
    """The provider's models: fetched once, off the GTK thread, silently."""

    def __init__(self) -> None:
        self.models: list[str] = []
        self._loaded = False
        # Who to tell when the list lands; None while no fetch is running.
        self._waiting: list[Callable[[], None]] | None = None

    def load(self, on_loaded: Callable[[], None]) -> None:
        """Fetch the list unless there is one. `on_loaded()` runs on the GTK
        thread once it lands; if the fetch fails it never does, and the next
        `load` tries again."""
        if self._loaded:
            return
        if self._waiting is not None:
            self._waiting.append(on_loaded)
            return
        self._waiting = [on_loaded]
        threading.Thread(target=self._fetch, name="steno-models", daemon=True).start()

    def _fetch(self) -> None:
        try:
            models = fetch_models()
        except Exception:   # a menu entry is not worth an error
            models = []
        GLib.idle_add(self._arrive, models)

    def _arrive(self, models: list[str]) -> bool:
        waiting, self._waiting = self._waiting or [], None
        if models:
            self.models, self._loaded = models, True
            for on_loaded in waiting:
                on_loaded()
        return GLib.SOURCE_REMOVE


MODELS = ModelList()


class _ChoiceRow(Gtk.ListBoxRow):
    """One model in the list. A value of None is the "Custom…" row."""

    def __init__(self, value: str | None, label: str, current: bool = False) -> None:
        super().__init__()
        self.value = value
        self.label = label
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        text = Gtk.Label(label=label, xalign=0.0)
        text.set_hexpand(True)
        text.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        text.set_max_width_chars(36)
        box.append(text)
        if current:
            box.append(Gtk.Image.new_from_icon_name("object-select-symbolic"))
        self.set_child(box)
        self.set_tooltip_text(label)


class AskModelButton(Gtk.MenuButton):
    """The model in force, as a small flat button that opens the choices."""

    def __init__(self) -> None:
        super().__init__()
        self.add_css_class("flat")
        self.add_css_class("ask-model")
        self.set_halign(Gtk.Align.START)

        self.model_label = Gtk.Label(xalign=0.0)
        self.model_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.model_label.set_max_width_chars(24)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.append(self.model_label)
        row.append(Gtk.Image.new_from_icon_name("pan-down-symbolic"))
        self.set_child(row)

        self.popover = Gtk.Popover()
        self.popover.set_child(self._build_choices())
        self.popover.connect("show", lambda *_a: self._on_show())
        self.set_popover(self.popover)
        # Two boxes share the pick; the one not on screen catches up on show.
        self.connect("map", lambda *_a: self.refresh())
        self.refresh()

    @property
    def model(self) -> str:
        """The model a question asked now would go to."""
        try:
            return ask_model(prefs.ask_model())
        except claude.ClaudeError:
            # A provider that does not exist names no default; the engine
            # says so when it is asked, and the window still opens.
            return prefs.ask_model()

    def refresh(self) -> None:
        self.model_label.set_text(self.model)
        by = f", set by {ASK_MODEL_ENV}" if os.environ.get(ASK_MODEL_ENV, "").strip() else ""
        self.set_tooltip_text(f"Model for questions{by}: {self.model}")

    # -------------------------------------------------------------- the choices

    def _build_choices(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(8)
        # A popover otherwise takes the width of its narrowest line.
        box.set_size_request(280, -1)

        title = Gtk.Label(label="Model for questions", xalign=0.0)
        title.add_css_class("sec")
        box.append(title)

        self.note = Gtk.Label(xalign=0.0)
        self.note.set_wrap(True)
        self.note.set_max_width_chars(34)
        self.note.add_css_class("hint")
        box.append(self.note)

        self.filter_entry = Gtk.SearchEntry()
        self.filter_entry.set_placeholder_text("Filter models")
        self.filter_entry.connect("search-changed", lambda *_a: self.choices.invalidate_filter())
        box.append(self.filter_entry)

        self.choices = Gtk.ListBox()
        self.choices.set_selection_mode(Gtk.SelectionMode.NONE)
        self.choices.add_css_class("navigation-sidebar")
        self.choices.set_filter_func(self._shown)
        self.choices.connect("row-activated", lambda _l, row: self._activated(row))
        self.choices_scroll = Gtk.ScrolledWindow()
        self.choices_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.choices_scroll.set_propagate_natural_height(True)
        self.choices_scroll.set_max_content_height(260)
        self.choices_scroll.set_child(self.choices)
        box.append(self.choices_scroll)

        self.custom_entry = Gtk.Entry()
        self.custom_entry.set_placeholder_text("Model id, then Enter")
        self.custom_entry.connect("activate", lambda _e: self._custom_entered())
        box.append(self.custom_entry)
        return box

    def _on_show(self) -> None:
        """Rebuilt on each open: the pick may have moved, or been set elsewhere."""
        forced = os.environ.get(ASK_MODEL_ENV, "").strip()
        self.note.set_visible(bool(forced))
        self.note.set_text(
            f"{ASK_MODEL_ENV} is set to {forced}, so it answers every "
            "question. Unset it to choose here." if forced else "")
        for part in (self.filter_entry, self.choices_scroll):
            part.set_visible(not forced)
        self.custom_entry.set_visible(False)
        self.custom_entry.set_text("")
        self.filter_entry.set_text("")
        if not forced:
            self._fill()
            MODELS.load(self._on_models)

    def _on_models(self) -> None:
        if self.popover.get_visible():
            self._fill()

    def _fill(self) -> None:
        while (row := self.choices.get_row_at_index(0)) is not None:
            self.choices.remove(row)
        try:
            presets, default = claude.presets(), model_default()
        except claude.ClaudeError:
            presets, default = [], ""
        pick = prefs.ask_model()
        rows = choice_rows(pick, default, presets, MODELS.models)
        for value, label in rows:
            self.choices.append(_ChoiceRow(value, label, current=value == pick))
        self.choices.append(_ChoiceRow(None, "Custom…"))
        self.filter_entry.set_visible(len(rows) > FILTER_AFTER)

    def _shown(self, row: _ChoiceRow) -> bool:
        needle = self.filter_entry.get_text().strip().lower()
        return row.value is None or needle in row.label.lower()

    def _activated(self, row: _ChoiceRow) -> None:
        if row.value is None:
            self.custom_entry.set_visible(True)
            self.custom_entry.grab_focus()
        else:
            self._choose(row.value)

    def _custom_entered(self) -> None:
        # Enter on an empty box is not a choice; "Default" is how to go back.
        if self.custom_entry.get_text().strip():
            self._choose(self.custom_entry.get_text())

    def _choose(self, model: str) -> None:
        """Remember the pick. It is the next question that goes to it."""
        prefs.save_ask_model(model)
        self.refresh()
        self.popover.popdown()
