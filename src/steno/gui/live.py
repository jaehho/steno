"""A meeting in progress: what they just said, what you're writing, what you asked.

Weighted to the transcript on purpose. This page is read in a second by someone
who is mid-sentence, so the far side's words get the space and the type size.
On a wide window notes and asking sit in a narrow column beside them; on a tall
one — a window parked beside the call — they drop below, and the transcript
keeps the full width.
"""
from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk, Pango

from .style import TRANSCRIPT_PT_DEFAULT, TRANSCRIPT_PT_MAX, TRANSCRIPT_PT_MIN
from .widgets import AskBox, NotesBox, section

MAX_LINES = 600  # keep the pane cheap to render across a long meeting


class TranscriptLine(Gtk.Box):
    """One utterance: a speaker label and the words.

    The label is text, not just colour — the split has to survive a grayscale
    screenshot and a colourblind reader.
    """

    def __init__(self, speaker: str, text: str) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.add_css_class("transcript-line")
        self.add_css_class("from-them" if speaker == "them" else "from-you")

        tag = Gtk.Label(label="Them" if speaker == "them" else "You", xalign=0.0)
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


class LivePage(Gtk.Box):
    def __init__(self, on_ask: Callable[[str], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.session_dir = None
        self.transcript_pt = TRANSCRIPT_PT_DEFAULT

        self.layouts = Adw.MultiLayoutView()
        self.layouts.set_vexpand(True)
        self.layouts.add_layout(self._wide_layout())
        self.layouts.add_layout(self._stacked_layout())

        self.notes = NotesBox()
        self.ask = AskBox(on_ask=on_ask)
        notes = section("Your notes", self.notes, hint="Saved with the meeting")
        notes.set_vexpand(True)
        self.layouts.set_child("transcript", self._build_transcript())
        self.layouts.set_child("notes", notes)
        self.layouts.set_child("ask", section("Ask", self.ask))
        self.layouts.set_layout_name("wide")
        self.append(self.layouts)

    # ------------------------------------------------------------------ layouts

    def _wide_layout(self) -> Adw.Layout:
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        side.add_css_class("side-column")
        side.add_css_class("beside")
        side.set_size_request(320, -1)
        side.set_hexpand(False)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for setter in (inner.set_margin_top, inner.set_margin_start, inner.set_margin_end):
            setter(16)
        inner.set_margin_bottom(14)
        inner.set_vexpand(True)
        notes = Adw.LayoutSlot.new("notes")
        notes.set_vexpand(True)
        inner.append(notes)
        inner.append(Adw.LayoutSlot.new("ask"))
        side.append(inner)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        transcript = Adw.LayoutSlot.new("transcript")
        transcript.set_hexpand(True)
        row.append(transcript)
        row.append(side)
        layout = Adw.Layout.new(row)
        layout.set_name("wide")
        return layout

    def _stacked_layout(self) -> Adw.Layout:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        panel.add_css_class("side-column")
        panel.add_css_class("below")
        # The notes view expands; below the transcript that must not win it space.
        panel.set_vexpand(False)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for setter in (inner.set_margin_top, inner.set_margin_start, inner.set_margin_end):
            setter(14)
        inner.set_margin_bottom(12)
        notes = Adw.LayoutSlot.new("notes")
        notes.set_size_request(-1, 150)
        inner.append(notes)
        inner.append(Adw.LayoutSlot.new("ask"))
        panel.append(inner)

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        transcript = Adw.LayoutSlot.new("transcript")
        transcript.set_vexpand(True)
        column.append(transcript)
        column.append(panel)
        layout = Adw.Layout.new(column)
        layout.set_name("stacked")
        return layout

    def set_stacked(self, stacked: bool) -> None:
        self.layouts.set_layout_name("stacked" if stacked else "wide")
        # A new shape moves the bottom; land on the latest line again.
        self._scroll_to_end(force=True)

    def _build_transcript(self) -> Gtk.Widget:
        self.lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.lines.add_css_class("transcript-live")
        self.lines.set_margin_start(24)
        self.lines.set_margin_end(24)
        self.lines.set_margin_top(14)
        self.lines.set_margin_bottom(4)

        self.empty_hint = Gtk.Label(
            label="Nothing heard yet.\nTheir words appear here as they speak.",
            justify=Gtk.Justification.CENTER,
        )
        self.empty_hint.add_css_class("empty-hint")
        self.empty_hint.set_vexpand(True)
        self.empty_hint.set_valign(Gtk.Align.CENTER)

        # Interim text sits below the finals and is replaced, never appended.
        self.interim = Gtk.Label(label="", xalign=0.0)
        self.interim.set_wrap(True)
        self.interim.add_css_class("transcript-live")
        self.interim.add_css_class("interim")
        self.interim.set_margin_start(24 + 52)
        self.interim.set_margin_end(24)
        self.interim.set_margin_bottom(14)
        self.interim.set_visible(False)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        inner.set_valign(Gtk.Align.END)
        inner.append(self.empty_hint)
        inner.append(self.lines)
        inner.append(self.interim)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(inner)
        self.scroller.set_vexpand(True)
        self.scroller.set_hexpand(True)
        # Follow the conversation unless the user has scrolled up to read. The
        # range grows after layout, not when a line is appended, so the snap
        # to the bottom happens when it changes rather than when text arrives.
        self._following = True
        self._snap: int | None = None
        adj = self.scroller.get_vadjustment()
        adj.connect("changed", self._on_range_changed)
        adj.connect("value-changed", self._on_scrolled)
        return self.scroller

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
        self.ask.clear()
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

    def _scroll_to_end(self, force: bool = False) -> None:
        if force:
            self._following = True
        if self._following:
            adj = self.scroller.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())

    def _on_range_changed(self, adj) -> None:
        # Emitted mid-allocation, where moving the view would not be laid out
        # until something else asked for it; do it once the frame is done.
        if self._following and self._snap is None:
            self._snap = GLib.idle_add(self._snap_to_end, adj)

    def _snap_to_end(self, adj) -> bool:
        self._snap = None
        if self._following:
            adj.set_value(adj.get_upper() - adj.get_page_size())
        return GLib.SOURCE_REMOVE

    def _on_scrolled(self, adj) -> None:
        self._following = adj.get_value() + adj.get_page_size() >= adj.get_upper() - 120

    # -------------------------------------------------------------------- notes

    def bind_session(self, session_dir) -> None:
        self.session_dir = session_dir
        self.notes.bind(session_dir)
        self._scroll_to_end(force=True)

    def flush_notes(self) -> None:
        self.notes.flush()

    # --------------------------------------------------------------- type scale

    def zoom(self, delta: int) -> int:
        self.transcript_pt = max(
            TRANSCRIPT_PT_MIN, min(TRANSCRIPT_PT_MAX, self.transcript_pt + delta)
        )
        return self.transcript_pt
