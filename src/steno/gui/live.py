"""The live view: what they just said, what you're writing, what you asked.

Weighted to the transcript on purpose. This screen is read in a second by
someone who is mid-sentence, so the far side's words get the space and the type
size, and everything else is narrow and quiet beside them.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango

from ..notes import load_notes, save_notes
from .style import (
    TRANSCRIPT_PT_DEFAULT,
    TRANSCRIPT_PT_MAX,
    TRANSCRIPT_PT_MIN,
)

NOTES_AUTOSAVE_S = 3
MAX_LINES = 600  # keep the pane cheap to render across a long meeting


class TranscriptLine(Gtk.Box):
    """One utterance: a speaker tag and the words.

    The tag is text, not just colour — the split has to survive a grayscale
    screenshot and a colourblind reader.
    """

    def __init__(self, speaker: str, text: str) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.add_css_class("transcript-line")
        self.add_css_class("from-them" if speaker == "them" else "from-you")

        tag = Gtk.Label(label="THEM" if speaker == "them" else "YOU", xalign=1.0)
        tag.add_css_class("speaker-tag")
        tag.set_valign(Gtk.Align.START)
        tag.set_width_chars(5)
        self.append(tag)

        self.label = Gtk.Label(label=text, xalign=0.0)
        self.label.set_wrap(True)
        self.label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.label.set_selectable(True)
        self.label.set_hexpand(True)
        self.append(self.label)


class LiveView(Gtk.Box):
    def __init__(self, on_ask, on_toggle_record) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_ask = on_ask
        self._on_toggle_record = on_toggle_record
        self.session_dir = None
        self._notes_dirty = False
        self._notes_timer: int | None = None
        self.transcript_pt = TRANSCRIPT_PT_DEFAULT

        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        split.set_position(640)
        split.set_resize_start_child(True)
        split.set_shrink_start_child(False)
        split.set_shrink_end_child(False)
        split.set_start_child(self._build_transcript())
        split.set_end_child(self._build_side())
        split.set_vexpand(True)
        self.append(split)

    # ----------------------------------------------------------------- building

    def _build_transcript(self) -> Gtk.Widget:
        self.lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.lines.set_margin_start(18)
        self.lines.set_margin_end(18)
        self.lines.set_margin_top(14)
        self.lines.set_margin_bottom(6)
        self.lines.add_css_class("transcript")

        self.empty_hint = Gtk.Label(
            label="Nothing heard yet.\nThis fills in once a meeting starts.",
            justify=Gtk.Justification.CENTER,
        )
        self.empty_hint.add_css_class("empty-hint")
        self.empty_hint.set_vexpand(True)
        self.empty_hint.set_valign(Gtk.Align.CENTER)

        # Interim text sits below the finals and is replaced, never appended.
        self.interim = Gtk.Label(label="", xalign=0.0)
        self.interim.set_wrap(True)
        self.interim.add_css_class("interim")
        self.interim.set_margin_start(18)
        self.interim.set_margin_end(18)
        self.interim.set_margin_bottom(12)
        self.interim.set_visible(False)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        inner.append(self.empty_hint)
        inner.append(self.lines)
        inner.append(self.interim)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(inner)
        self.scroller.set_vexpand(True)
        self.scroller.add_css_class("transcript")
        return self.scroller

    def _build_side(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_size_request(320, -1)

        box.append(_section_label("Notes"))
        self.notes = Gtk.TextView()
        self.notes.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.notes.add_css_class("notes")
        self.notes.get_buffer().connect("changed", self._on_notes_changed)
        notes_scroll = Gtk.ScrolledWindow()
        notes_scroll.set_child(self.notes)
        notes_scroll.set_vexpand(True)

        # TextView has no placeholder, so float one and hide it on first keystroke.
        self.notes_hint = Gtk.Label(label="Your notes. Saved with the meeting.")
        self.notes_hint.add_css_class("empty-hint")
        self.notes_hint.set_halign(Gtk.Align.START)
        self.notes_hint.set_valign(Gtk.Align.START)
        self.notes_hint.set_margin_start(14)
        self.notes_hint.set_margin_top(10)
        self.notes_hint.set_can_target(False)
        overlay = Gtk.Overlay()
        overlay.set_child(notes_scroll)
        overlay.add_overlay(self.notes_hint)
        overlay.set_vexpand(True)
        box.append(overlay)

        box.append(_hairline())
        box.append(_section_label("Ask"))

        self.advice = Gtk.Label(label="", xalign=0.0)
        self.advice.set_wrap(True)
        self.advice.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.advice.set_selectable(True)
        self.advice.add_css_class("advice")
        self.advice.set_visible(False)
        # Collapsed until something has been asked; an empty pane reserving a
        # third of the column is the opposite of glanceable.
        self.advice_scroll = Gtk.ScrolledWindow()
        self.advice_scroll.set_child(self.advice)
        self.advice_scroll.set_size_request(-1, 150)
        self.advice_scroll.set_visible(False)
        box.append(self.advice_scroll)

        self.ask_entry = Gtk.Entry()
        self.ask_entry.set_placeholder_text("Ask about this meeting…  (Ctrl+G)")
        self.ask_entry.connect("activate", lambda _e: self.ask())
        self.ask_entry.set_margin_start(8)
        self.ask_entry.set_margin_end(8)
        self.ask_entry.set_margin_top(6)
        self.ask_entry.set_margin_bottom(8)
        box.append(self.ask_entry)
        return box

    # ------------------------------------------------------------------- events

    def add_line(self, speaker: str, text: str) -> None:
        self.empty_hint.set_visible(False)
        self.lines.append(TranscriptLine(speaker, text))
        self._trim()
        self.set_interim("")
        self._scroll_to_end()

    def set_interim(self, text: str) -> None:
        self.interim.set_text(text)
        self.interim.set_visible(bool(text))
        if text:
            self.empty_hint.set_visible(False)
            self._scroll_to_end()

    def clear(self) -> None:
        while (child := self.lines.get_first_child()) is not None:
            self.lines.remove(child)
        self.set_interim("")
        self.advice.set_text("")
        self.advice.set_visible(False)
        self.advice_scroll.set_visible(False)
        self.empty_hint.set_visible(True)

    def _trim(self) -> None:
        count = 0
        child = self.lines.get_last_child()
        while child is not None:
            count += 1
            child = child.get_prev_sibling()
        while count > MAX_LINES and (first := self.lines.get_first_child()):
            self.lines.remove(first)
            count -= 1

    def _scroll_to_end(self) -> None:
        """Follow the conversation, unless the user has scrolled up to read."""
        adj = self.scroller.get_vadjustment()
        at_bottom = (
            adj.get_value() + adj.get_page_size() >= adj.get_upper() - 120
        )
        if at_bottom:
            GLib.idle_add(
                lambda: (adj.set_value(adj.get_upper() - adj.get_page_size()), False)[1]
            )

    # ------------------------------------------------------------------- advice

    def ask(self) -> None:
        question = self.ask_entry.get_text().strip()
        self.advice.set_text("thinking…")
        self.advice.add_css_class("advice-thinking")
        self.advice.set_visible(True)
        self.advice_scroll.set_visible(True)
        self._advice_buf = ""
        self.ask_entry.set_text("")
        self._on_ask(question)

    def advice_delta(self, text: str) -> None:
        if not hasattr(self, "_advice_buf"):
            self._advice_buf = ""
        if not self._advice_buf:
            self.advice.remove_css_class("advice-thinking")
            self.advice.set_text("")
        self._advice_buf += text
        self.advice.set_text(self._advice_buf)

    def advice_done(self, text: str) -> None:
        self.advice.remove_css_class("advice-thinking")
        if text:
            self.advice.set_text(text)
            self._advice_buf = text

    # -------------------------------------------------------------------- notes

    def bind_session(self, session_dir) -> None:
        """Point the notes pane at a session, flushing whatever came before."""
        self.flush_notes()
        self.session_dir = session_dir
        buf = self.notes.get_buffer()
        buf.handler_block_by_func(self._on_notes_changed)
        buf.set_text(load_notes(session_dir) if session_dir else "")
        buf.handler_unblock_by_func(self._on_notes_changed)
        self.notes_hint.set_visible(buf.get_char_count() == 0)
        self._notes_dirty = False

    def _on_notes_changed(self, buf) -> None:
        self._notes_dirty = True
        self.notes_hint.set_visible(buf.get_char_count() == 0)
        if self._notes_timer is None:
            self._notes_timer = GLib.timeout_add_seconds(
                NOTES_AUTOSAVE_S, self._autosave
            )

    def _autosave(self) -> bool:
        self._notes_timer = None
        self.flush_notes()
        return GLib.SOURCE_REMOVE

    def flush_notes(self) -> None:
        """Persist notes now. Called on autosave, session end, and shutdown."""
        if not self._notes_dirty or self.session_dir is None:
            return
        buf = self.notes.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        save_notes(self.session_dir, text)
        self._notes_dirty = False

    # --------------------------------------------------------------- type scale

    def zoom(self, delta: int) -> int:
        self.transcript_pt = max(
            TRANSCRIPT_PT_MIN, min(TRANSCRIPT_PT_MAX, self.transcript_pt + delta)
        )
        return self.transcript_pt


def _section_label(text: str) -> Gtk.Widget:
    label = Gtk.Label(label=text, xalign=0.0)
    label.add_css_class("chrome")
    label.set_margin_start(10)
    label.set_margin_top(10)
    label.set_margin_bottom(4)
    return label


def _hairline() -> Gtk.Widget:
    line = Gtk.Box()
    line.add_css_class("hairline")
    return line


__all__ = ["Adw", "LiveView", "TranscriptLine"]
