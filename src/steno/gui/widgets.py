"""Pieces both meeting pages use: notes, asking, and the words between them."""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk, Pango

from ..notes import load_notes, save_notes

NOTES_AUTOSAVE_S = 3


def section_label(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0.0)
    label.add_css_class("sec")
    return label


def hint_label(text: str = "") -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0.0)
    label.add_css_class("hint")
    return label


def section(
    title: str, *children: Gtk.Widget, hint: str = "", beside: Gtk.Widget | None = None,
    spacing: int = 8,
) -> Gtk.Box:
    """A labelled block: the label (and a hint or count beside it), then content."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    head.append(section_label(title))
    if hint:
        head.append(hint_label(hint))
    if beside is not None:
        head.append(beside)
    box.append(head)
    for child in children:
        box.append(child)
    return box


def wrapping_label(text: str = "", selectable: bool = True) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0.0, yalign=0.0)
    label.set_wrap(True)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_selectable(selectable)
    return label


class NotesBox(Gtk.Overlay):
    """The user's notes for one meeting: a plain sheet, saved as they type.

    `notes.md` is theirs and is never rewritten by anything else, so this only
    writes when they have actually changed something. `on_saved(dir)` hears
    of each write, for whatever else is built from the notes.
    """

    def __init__(
        self,
        placeholder: str = "Your notes. Saved with the meeting.",
        on_saved: Callable[[Path], None] | None = None,
    ) -> None:
        super().__init__()
        self._on_saved = on_saved
        self.session_dir: Path | None = None
        self._dirty = False
        self._timer: int | None = None

        self.view = Gtk.TextView()
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.add_css_class("notes")
        self.view.get_buffer().connect("changed", self._on_changed)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.view)
        scroll.add_css_class("notes-frame")
        scroll.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_child(scroll)

        # TextView has no placeholder, so float one and hide it on first keystroke.
        self.hint = Gtk.Label(label=placeholder, xalign=0.0)
        self.hint.set_wrap(True)
        self.hint.add_css_class("empty-hint")
        self.hint.set_halign(Gtk.Align.FILL)
        self.hint.set_margin_end(14)
        self.hint.set_valign(Gtk.Align.START)
        self.hint.set_margin_start(14)
        self.hint.set_margin_top(9)
        self.hint.set_can_target(False)
        self.add_overlay(self.hint)
        self.set_size_request(-1, 110)
        self.set_vexpand(True)

    def bind(self, session_dir: Path | None) -> None:
        """Point at a meeting, saving whatever was open before."""
        self.flush()
        self.session_dir = session_dir
        buf = self.view.get_buffer()
        buf.handler_block_by_func(self._on_changed)
        buf.set_text(load_notes(session_dir) if session_dir else "")
        buf.handler_unblock_by_func(self._on_changed)
        self.hint.set_visible(buf.get_char_count() == 0)
        self._dirty = False

    def _on_changed(self, buf) -> None:
        self._dirty = True
        self.hint.set_visible(buf.get_char_count() == 0)
        if self._timer is None:
            self._timer = GLib.timeout_add_seconds(NOTES_AUTOSAVE_S, self._autosave)

    def _autosave(self) -> bool:
        self._timer = None
        self.flush()
        return GLib.SOURCE_REMOVE

    def flush(self) -> None:
        """Persist now. Called on autosave, on switching meetings, and on close."""
        if not self._dirty or self.session_dir is None:
            return
        buf = self.view.get_buffer()
        save_notes(self.session_dir, buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
        self._dirty = False
        if self._on_saved is not None:
            self._on_saved(self.session_dir)


class AskBox(Gtk.Box):
    """A question about this meeting, and the answer as it streams in.

    The answer area stays collapsed until something has been asked; an empty
    pane reserving space is the opposite of glanceable.
    """

    IDLE_PLACEHOLDER = "Ask about this meeting  (Ctrl+G)"
    BUSY_PLACEHOLDER = "Still answering the last question…"

    def __init__(self, on_ask: Callable[[str], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._on_ask = on_ask
        self._buf = ""
        # One question at a time, because the engine answers one at a time.
        # `_ignore` is set when the box is cleared (another meeting was opened)
        # under an answer still in flight: it is for the old page, not this one.
        self._busy = False
        self._ignore = False

        self.question = hint_label()
        self.question.set_wrap(True)
        self.question.set_max_width_chars(30)
        self.question.set_visible(False)
        self.append(self.question)

        self.answer = wrapping_label()
        self.answer.set_max_width_chars(30)
        self.answer.add_css_class("advice")
        self.answer_scroll = Gtk.ScrolledWindow()
        self.answer_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.answer_scroll.set_propagate_natural_height(True)
        self.answer_scroll.set_max_content_height(220)
        self.answer_scroll.set_child(self.answer)
        self.answer_scroll.set_visible(False)
        self.append(self.answer_scroll)

        self.entry = Gtk.Entry()
        self.entry.add_css_class("ask")
        self.entry.set_placeholder_text(self.IDLE_PLACEHOLDER)
        self.entry.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, "system-search-symbolic")
        self.entry.connect("activate", lambda _e: self._submit())
        self.append(self.entry)

    def _submit(self) -> None:
        question = self.entry.get_text().strip()
        if not question or self._busy:
            return
        self._busy = True
        self._ignore = False
        self.entry.set_placeholder_text(self.BUSY_PLACEHOLDER)
        self.entry.set_text("")
        self.question.set_text(question)
        self.question.set_visible(True)
        self.answer.set_text("Thinking…")
        self.answer.add_css_class("advice-thinking")
        self.answer_scroll.set_visible(True)
        self._buf = ""
        self._on_ask(question)

    def _finish(self) -> None:
        self._busy = False
        self._ignore = False
        self.entry.set_placeholder_text(self.IDLE_PLACEHOLDER)

    def delta(self, text: str) -> None:
        if self._ignore:
            return
        if not self._buf:
            self.answer.remove_css_class("advice-thinking")
        self._buf += text
        self.answer.set_text(self._buf)

    def done(self, text: str) -> None:
        was_ignored = self._ignore
        self._finish()
        if was_ignored:
            return
        self.answer.remove_css_class("advice-thinking")
        if text:
            self._buf = text
            self.answer.set_text(text)
        elif not self._buf:
            self.answer_scroll.set_visible(False)
            self.question.set_visible(False)

    def clear(self) -> None:
        self._ignore = self._busy
        self._buf = ""
        self.answer.set_text("")
        self.answer_scroll.set_visible(False)
        self.question.set_visible(False)

    def focus(self) -> None:
        self.entry.grab_focus()
