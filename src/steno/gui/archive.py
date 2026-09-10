"""The archive: meetings you've had, and what you owe because of them.

This is where the tool earns its keep. A recording nobody replays is worse than
no recording, so the summary is the default surface and the raw transcript is
one click further in — the opposite of what a capture tool usually does.

The transcript is also the tape deck. ASR gets words wrong, and the only honest
fix for "did they really say that?" is to hear it, so every line is a seek
point.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, Gtk, Pango

from ..notes import (
    Todo,
    ensure_todos,
    load_notes,
    open_count,
    save_notes,
    set_done,
)
from ..playback import (
    display_offset,
    format_offset,
    index_at,
    repair_started_at,
    seek_offset,
    session_tracks,
)
from ..realign import plan
from ..retention import audio_bytes, delete_audio, delete_session, human_bytes
from ..summarize import (
    SUMMARY_FILENAME,
    duration_minutes,
    iter_sessions,
    load_records,
    load_title,
    session_stamp,
)
from .player import Player

NUDGE_S = 10.0

# What "listen to" means for each side. Both sides at once is the default,
# because that is what the meeting sounded like; the solo settings are for the
# times when one side is the only one you can make out. Neither side is at full
# scale when both are playing, so two loud moments landing together stay inside
# the headroom.
MIXES: tuple[tuple[str, dict[str, float]], ...] = (
    ("Both sides", {"you": 0.8, "them": 0.8}),
    ("Only you", {"you": 1.0, "them": 0.0}),
    ("Only them", {"you": 0.0, "them": 1.0}),
)


class Session:
    """A row in the sidebar, read cheaply from disk."""

    def __init__(self, path: Path) -> None:
        self.dir = path
        self.title = load_title(path)
        try:
            self.start = datetime.strptime(path.name, "%Y%m%d-%H%M%S")  # noqa: DTZ007
        except ValueError:
            self.start = datetime.fromtimestamp(path.stat().st_mtime)  # noqa: DTZ006
        self.open_todos = open_count(path)
        self._haystack: str | None = None
        self.stamp = session_stamp(path)

    @property
    def summary_path(self) -> Path:
        return self.dir / SUMMARY_FILENAME

    def matches(self, query: str) -> bool:
        """Substring search over everything the meeting produced.

        Reading the transcripts is fine at this scale and keeps the archive a
        directory of files rather than a database. If this ever gets slow, that
        is the moment to add an index — not before.
        """
        if not query:
            return True
        if self._haystack is None:
            parts = [self.title or "", self.dir.name]
            for name in (SUMMARY_FILENAME, "notes.md", "transcript.txt"):
                try:
                    parts.append((self.dir / name).read_text())
                except OSError:
                    pass
            self._haystack = "\n".join(parts).lower()
        return all(word in self._haystack for word in query.lower().split())


class ArchiveView(Gtk.Box):
    def __init__(self, root: Path, on_realign=None) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.root = root
        # Realigning is network work on the engine's loop; the view asks for it
        # and the window decides how to run it.
        self._on_realign = on_realign
        self._realigning = False
        self.sessions: list[Session] = []
        self.shown: list[Session] = []
        self.current: Session | None = None
        self.query = ""
        self._notes_dirty = False
        self._records: list[dict] = []
        self._offsets: list[float] = []
        self._rows: list[Gtk.Widget] = []
        self._playing_index = -1
        self._started_at = 0.0
        self.player = Player(on_tick=self._on_tick)

        self.append(self._build_sidebar())
        self.append(_vhairline())
        self.append(self._build_content())
        self.refresh()

    # ----------------------------------------------------------------- building

    def _build_sidebar(self) -> Gtk.Widget:
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search meetings")
        self.search.set_margin_start(8)
        self.search.set_margin_end(8)
        self.search.set_margin_top(8)
        self.search.set_margin_bottom(8)
        self.search.connect("search-changed", self._on_search)

        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.connect("row-selected", self._on_row_selected)

        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self.list)
        scroll.set_vexpand(True)

        self.sidebar_footer = Gtk.Label(label="", xalign=0.0)
        self.sidebar_footer.add_css_class("chrome")
        self.sidebar_footer.set_margin_start(12)
        self.sidebar_footer.set_margin_bottom(8)
        self.sidebar_footer.set_margin_top(4)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.search)
        box.append(_hairline())
        box.append(scroll)
        box.append(_hairline())
        box.append(self.sidebar_footer)
        box.set_size_request(300, -1)
        return box

    def _build_content(self) -> Gtk.Widget:
        self.stack = Adw.ViewStack()
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)

        self.summary_label = _reading_label()
        self.summary_label.add_css_class("summary")
        self.stack.add_titled_with_icon(
            _scrolled(self.summary_label), "summary", "Summary", "view-list-symbolic"
        )

        self.todo_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.todo_box.set_margin_start(18)
        self.todo_box.set_margin_end(18)
        self.todo_box.set_margin_top(14)
        self.stack.add_titled_with_icon(
            _scrolled(self.todo_box), "todos", "Todos", "checkbox-checked-symbolic"
        )

        self.notes_view = Gtk.TextView()
        self.notes_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.notes_view.add_css_class("notes")
        self.notes_view.get_buffer().connect("changed", self._on_notes_changed)
        self.stack.add_titled_with_icon(
            _scrolled(self.notes_view), "notes", "Notes", "document-edit-symbolic"
        )

        self.stack.add_titled_with_icon(
            self._build_transcript(),
            "transcript", "Transcript", "audio-input-microphone-symbolic",
        )

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_margin_top(8)
        switcher.set_margin_bottom(8)
        switcher.set_hexpand(True)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.append(switcher)
        header.append(self._build_session_menu())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(header)
        box.append(_hairline())
        box.append(self.stack)
        box.set_hexpand(True)
        return box

    def _build_session_menu(self) -> Gtk.Widget:
        """Everything that acts on the meeting you are looking at.

        Destructive items live behind a menu and a dialog rather than a button
        in reach of the transcript, which is a surface people click at speed.
        """
        menu = Gio.Menu()
        menu.append("Re-time against the audio…", "archive.realign")
        section = Gio.Menu()
        section.append("Delete the audio, keep the text…", "archive.delete-audio")
        section.append("Delete this meeting…", "archive.delete")
        menu.append_section(None, section)

        actions = Gio.SimpleActionGroup()
        for name, handler in (
            ("realign", self._confirm_realign),
            ("delete-audio", self._confirm_delete_audio),
            ("delete", self._confirm_delete),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, fn=handler: fn())
            actions.add_action(action)
        self.insert_action_group("archive", actions)

        button = Gtk.MenuButton()
        button.set_icon_name("view-more-symbolic")
        button.set_tooltip_text("This meeting")
        button.set_menu_model(menu)
        button.set_margin_end(10)
        button.set_valign(Gtk.Align.CENTER)
        self.session_menu = button
        return button

    def _build_transcript(self) -> Gtk.Widget:
        self.transcript_list = Gtk.ListBox()
        self.transcript_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.transcript_list.add_css_class("transcript")
        self.transcript_list.connect("row-activated", self._on_line_activated)
        self.transcript_list.set_margin_start(10)
        self.transcript_list.set_margin_end(10)
        self.transcript_list.set_margin_top(10)

        self.transcript_scroll = _scrolled(self.transcript_list)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.transcript_scroll)
        box.append(_hairline())
        box.append(self._build_transport())
        return box

    def _build_transport(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_start(14)
        bar.set_margin_end(14)
        bar.set_margin_top(8)
        bar.set_margin_bottom(8)

        self.play_button = Gtk.Button.new_from_icon_name("media-playback-start-symbolic")
        self.play_button.set_tooltip_text("Play / pause")
        self.play_button.connect("clicked", lambda _b: self._toggle_play())

        back = Gtk.Button.new_from_icon_name("media-seek-backward-symbolic")
        back.set_tooltip_text(f"Back {NUDGE_S:.0f} seconds")
        back.connect("clicked", lambda _b: self.player.nudge(-NUDGE_S))

        self.position_label = Gtk.Label(label="—", xalign=0.0)
        self.position_label.add_css_class("chrome")

        self.playback_hint = Gtk.Label(label="Click a line to hear it", xalign=1.0)
        self.playback_hint.add_css_class("chrome")
        self.playback_hint.set_hexpand(True)

        self.mix_choice = Gtk.DropDown.new_from_strings(
            [label for label, _levels in MIXES]
        )
        self.mix_choice.set_tooltip_text("Which side to listen to")
        self.mix_choice.set_valign(Gtk.Align.CENTER)
        self.mix_choice.connect("notify::selected", lambda *_a: self._apply_mix())

        bar.append(self.play_button)
        bar.append(back)
        bar.append(self.position_label)
        bar.append(self.playback_hint)
        bar.append(self.mix_choice)
        return bar

    def _apply_mix(self) -> None:
        _label, levels = MIXES[min(self.mix_choice.get_selected(), len(MIXES) - 1)]
        self.player.set_levels(levels)

    # ------------------------------------------------------------------ loading

    def refresh(self, select: Path | None = None) -> None:
        """Re-read the sessions directory, keeping the selection where it can."""
        keep = select or (self.current.dir if self.current else None)
        self.sessions = [Session(p) for p in iter_sessions(self.root)]
        self._repopulate(keep)

    def refresh_if_stale(self) -> bool:
        """Reload only when the files behind the view have actually changed.

        `steno realign` and `steno summarize` rewrite a meeting from a terminal
        while the window sits there showing what it read on the way in, and a
        transcript that has been replaced on disk but not on screen is the kind
        of wrong that looks like the tool not having worked. Unconditionally
        reloading would throw away the scroll position every time the window is
        raised, so this checks first.
        """
        current = [session_stamp(p) for p in iter_sessions(self.root)]
        if current == [s.stamp for s in self.sessions]:
            return False
        self.refresh()
        return True

    def _repopulate(self, keep: Path | None) -> None:
        while (row := self.list.get_first_child()) is not None:
            self.list.remove(row)
        self.shown = [s for s in self.sessions if s.matches(self.query)]
        for s in self.shown:
            self.list.append(_session_row(s))
        self._update_footer()
        if not self.shown:
            self.current = None
            self._show_empty()
            return
        index = next((i for i, s in enumerate(self.shown) if s.dir == keep), 0)
        row = self.list.get_row_at_index(index)
        if row is not None:
            self.list.select_row(row)

    def _update_footer(self) -> None:
        total = len(self.sessions)
        open_todos = sum(s.open_todos for s in self.sessions)
        shown = f"{len(self.shown)}/{total}" if self.query else str(total)
        parts = [f"{shown} meeting{'' if total == 1 and not self.query else 's'}"]
        if open_todos:
            parts.append(f"{open_todos} open")
        self.sidebar_footer.set_text(" · ".join(parts))

    def _on_search(self, entry) -> None:
        self.query = entry.get_text().strip()
        self._repopulate(self.current.dir if self.current else None)

    def focus_search(self) -> None:
        self.search.grab_focus()

    def _on_row_selected(self, _list, row) -> None:
        if row is None:
            return
        self.flush_notes()
        index = row.get_index()
        if 0 <= index < len(self.shown):
            current = self.shown[index]
            if self.current is not None and current.dir != self.current.dir:
                self.stop_playback()
            self.current = current
            self._load(current)

    def _load(self, s: Session) -> None:
        if s.summary_path.is_file():
            self.summary_label.set_markup(_markup(s.summary_path.read_text()))
        else:
            self.summary_label.set_markup(
                "<i>No summary yet.</i>\n\n"
                "It is written when a meeting ends. For an older session, run\n"
                f"<tt>steno summarize {s.dir.name}</tt>"
            )
        self._load_todos(s)

        buf = self.notes_view.get_buffer()
        buf.handler_block_by_func(self._on_notes_changed)
        buf.set_text(load_notes(s.dir))
        buf.handler_unblock_by_func(self._on_notes_changed)
        self._notes_dirty = False

        self._load_transcript(s)

    def _load_transcript(self, s: Session) -> None:
        while (row := self.transcript_list.get_first_child()) is not None:
            self.transcript_list.remove(row)
        self._records = load_records(s.dir)
        # Repairs and caches `started_at` for sessions recorded before the tool
        # wrote one, so the gutter and playback agree from the first click.
        self._started_at = repair_started_at(s.dir, self._records)
        self._offsets = [seek_offset(r, self._started_at) for r in self._records]
        self._rows = []
        self._playing_index = -1

        for record in self._records:
            row = _transcript_row(record, self._started_at)
            self.transcript_list.append(row)
            self._rows.append(row)
        if not self._records:
            empty = Gtk.Label(label="No transcript.", xalign=0.0)
            empty.add_css_class("empty-hint")
            self.transcript_list.append(empty)
        self._update_transport_hint(s)

    def _update_transport_hint(self, s: Session) -> None:
        has_audio = audio_bytes(s.dir) > 0
        self.play_button.set_sensitive(has_audio)
        if not has_audio:
            self.playback_hint.set_text("No audio for this meeting")
        elif not self.player.available():
            self.playback_hint.set_text("GStreamer not available")
            self.play_button.set_sensitive(False)
        else:
            self.playback_hint.set_text(
                f"Click a line to hear it · {human_bytes(audio_bytes(s.dir))}"
            )
        self.position_label.set_text("—")

    def _load_todos(self, s: Session) -> None:
        while (child := self.todo_box.get_first_child()) is not None:
            self.todo_box.remove(child)
        todos = ensure_todos(s.dir)
        if not todos:
            hint = Gtk.Label(
                label="Nothing to do out of this one.", xalign=0.0
            )
            hint.add_css_class("empty-hint")
            self.todo_box.append(hint)
            return
        for todo in todos:
            self.todo_box.append(self._todo_row(s, todo))

    def _todo_row(self, s: Session, todo: Todo) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        check = Gtk.CheckButton()
        check.set_active(todo.done)
        check.set_valign(Gtk.Align.START)

        label = Gtk.Label(label=todo.text, xalign=0.0)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_hexpand(True)
        label.set_selectable(True)
        if todo.done:
            label.add_css_class("todo-done")

        owner = Gtk.Label(label=todo.owner.upper(), xalign=1.0)
        owner.add_css_class("chrome")
        owner.add_css_class(f"owner-{todo.owner}")
        owner.set_valign(Gtk.Align.START)

        def toggled(button):
            set_done(s.dir, todo.key, button.get_active())
            label.remove_css_class("todo-done")
            if button.get_active():
                label.add_css_class("todo-done")
            s.open_todos = open_count(s.dir)
            self._update_footer()

        check.connect("toggled", toggled)
        row.append(check)
        row.append(label)
        row.append(owner)
        row.set_margin_bottom(6)
        return row

    def _show_empty(self) -> None:
        message = (
            "<i>No meeting matches that.</i>" if self.query
            else "<i>No meetings recorded yet.</i>\n\n"
                 "Start a call in a supported app and this fills itself in."
        )
        self.summary_label.set_markup(message)
        while (row := self.transcript_list.get_first_child()) is not None:
            self.transcript_list.remove(row)
        while (child := self.todo_box.get_first_child()) is not None:
            self.todo_box.remove(child)

    # ----------------------------------------------------------------- playback

    def _toggle_play(self) -> None:
        """Play, pause, or start from the top.

        A play button that does nothing until you have already clicked a line
        reads as broken, so with no track loaded this starts the meeting from
        its first line rather than shrugging.
        """
        if not self.player.tracks:
            self._play_line(0)
            return
        self.player.toggle()

    def _play_line(self, index: int) -> None:
        """Play the meeting from where this line starts.

        Both sides, on one timeline. The offsets are measured from the same
        instant in both files, so a line's position in its own track is its
        position in the mix, and the reply that follows it arrives on time
        instead of needing the other track opened by hand.
        """
        if self.current is None or not (0 <= index < len(self._records)):
            return
        tracks = session_tracks(self.current.dir)
        if not tracks:
            self.playback_hint.set_text("The audio is gone")
            return
        self._apply_mix()
        self.player.play(tracks, self._offsets[index])
        self._highlight(index)

    def _on_line_activated(self, _list, row) -> None:
        self._play_line(row.get_index())

    def _on_tick(self, position: float, duration: float, playing: bool) -> None:
        icon = "media-playback-pause-symbolic" if playing \
            else "media-playback-start-symbolic"
        self.play_button.set_icon_name(icon)
        if duration > 0:
            self.position_label.set_text(
                f"{format_offset(position)} / {format_offset(duration)}"
            )
        # Both sides share one timeline now, so following along is just asking
        # which line the meeting has reached.
        index = index_at(self._offsets, position)
        if index >= 0:
            self._highlight(index)

    def _highlight(self, index: int) -> None:
        if index == self._playing_index or not (0 <= index < len(self._rows)):
            return
        if 0 <= self._playing_index < len(self._rows):
            self._rows[self._playing_index].remove_css_class("line-playing")
        self._rows[index].add_css_class("line-playing")
        self._playing_index = index

    def stop_playback(self) -> None:
        """Let go of the audio device — on window close, and between sessions."""
        self.player.stop()
        if 0 <= self._playing_index < len(self._rows):
            self._rows[self._playing_index].remove_css_class("line-playing")
        self._playing_index = -1
        self.play_button.set_icon_name("media-playback-start-symbolic")

    # ------------------------------------------------------------ this meeting

    def _dialog(self, heading: str, body: str, confirm: str, on_confirm,
                destructive: bool = True) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("go", confirm)
        if destructive:
            dialog.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
        else:
            dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.connect(
            "response", lambda _d, response: on_confirm() if response == "go" else None
        )
        dialog.present(self)

    def _describe(self, s: Session) -> str:
        parts = [s.start.strftime("%A %d %B, %H:%M")]
        size = audio_bytes(s.dir)
        if size:
            parts.append(human_bytes(size) + " of audio")
        open_todos = s.open_todos
        if open_todos:
            parts.append(f"{open_todos} open todo{'s' if open_todos != 1 else ''}")
        return " · ".join(parts)

    def _confirm_delete(self) -> None:
        s = self.current
        if s is None:
            return
        self._dialog(
            f"Delete “{s.title or s.dir.name}”?",
            f"{self._describe(s)}\n\n"
            "The recording, transcript, summary, todos and your notes all go to "
            "the trash together. You can put them back from there.",
            "Move to trash",
            lambda: self._delete(s),
        )

    def _delete(self, s: Session) -> None:
        self.stop_playback()
        outcome, where = delete_session(s.dir)
        if outcome == "failed":
            self._dialog(
                "Could not move it to the trash",
                f"{s.dir} is still there. This usually means the trash is on a "
                "different filesystem.\n\nDeleting it permanently cannot be "
                "undone.",
                "Delete permanently",
                lambda: self._delete_permanently(s),
            )
            return
        self.sessions = [x for x in self.sessions if x.dir != s.dir]
        self.current = None
        self._repopulate(None)
        self.playback_hint.set_text(f"Moved to trash: {where.name if where else ''}")

    def _delete_permanently(self, s: Session) -> None:
        outcome, _ = delete_session(s.dir, permanent=True)
        if outcome == "deleted":
            self.sessions = [x for x in self.sessions if x.dir != s.dir]
            self.current = None
            self._repopulate(None)

    def _confirm_delete_audio(self) -> None:
        s = self.current
        if s is None:
            return
        size = audio_bytes(s.dir)
        if not size:
            self.playback_hint.set_text("No audio to delete")
            return
        self._dialog(
            "Delete the audio for this meeting?",
            f"Frees {human_bytes(size)}. The transcript, summary, todos and your "
            "notes stay.\n\nPlayback will no longer work for this meeting, and "
            "the recording is not recoverable.",
            "Delete audio",
            lambda: self._delete_audio(s),
        )

    def _delete_audio(self, s: Session) -> None:
        self.stop_playback()
        freed = delete_audio(s.dir)
        self._load_transcript(s)
        self.playback_hint.set_text(f"Freed {human_bytes(freed)}")

    def _confirm_realign(self) -> None:
        s = self.current
        if s is None or self._on_realign is None or self._realigning:
            return
        job = plan(s.dir)
        if job.minutes <= 0:
            self.playback_hint.set_text("No audio to re-time against")
            return
        note = (
            "Every line already has a measured position, so this would only "
            "re-transcribe it.\n\n" if job.already_aligned else
            "Right now this meeting's lines are placed by inference, which is "
            "accurate to a second or two.\n\n"
        )
        self._dialog(
            "Re-time this meeting against its audio?",
            f"{note}Transcribing {job.minutes:.0f} minutes of audio costs about "
            f"${job.cost:.2f}. The far side is re-transcribed in one pass, which "
            "is usually more accurate than the live stream was.\n\n"
            "The current transcript is kept beside the new one.",
            "Re-time it",
            lambda: self._realign(s),
            destructive=False,
        )

    def _realign(self, s: Session) -> None:
        if self._on_realign is None:
            return
        self._realigning = True
        self.session_menu.set_sensitive(False)
        self.playback_hint.set_text("Re-timing against the audio…")
        self._on_realign(s.dir, self._realigned)

    def _realigned(self, session_dir: Path, lines: int, error: str) -> None:
        """Called back on the GTK thread when the pass finishes."""
        self._realigning = False
        self.session_menu.set_sensitive(True)
        if error:
            self.playback_hint.set_text(f"Re-timing failed: {error}")
            return
        self.refresh(select=session_dir)
        self.playback_hint.set_text(f"Re-timed · {lines} lines")

    # -------------------------------------------------------------------- notes

    def _on_notes_changed(self, _buf) -> None:
        self._notes_dirty = True

    def flush_notes(self) -> None:
        """Persist edits made in the archive's notes tab."""
        if not self._notes_dirty or self.current is None:
            return
        buf = self.notes_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        save_notes(self.current.dir, text)
        self._notes_dirty = False


# ------------------------------------------------------------------- rendering


def _transcript_row(record: dict, started_at: float) -> Gtk.ListBoxRow:
    """One line of transcript, and one seek point."""
    row = Gtk.ListBoxRow()
    row.set_activatable(True)
    speaker = str(record.get("speaker", "them"))

    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    box.add_css_class("transcript-line")
    box.add_css_class("from-them" if speaker == "them" else "from-you")

    stamp = Gtk.Label(
        label=format_offset(display_offset(record, started_at)), xalign=1.0
    )
    stamp.add_css_class("line-stamp")
    stamp.set_valign(Gtk.Align.START)
    stamp.set_width_chars(6)

    tag = Gtk.Label(label="THEM" if speaker == "them" else "YOU", xalign=1.0)
    tag.add_css_class("speaker-tag")
    tag.set_valign(Gtk.Align.START)
    tag.set_width_chars(5)

    text = Gtk.Label(label=str(record.get("text", "")), xalign=0.0)
    text.set_wrap(True)
    text.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    text.set_hexpand(True)

    box.append(stamp)
    box.append(tag)
    box.append(text)
    row.set_child(box)
    return row


def _session_row(s: Session) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.set_margin_start(12)
    box.set_margin_end(12)
    box.set_margin_top(8)
    box.set_margin_bottom(8)

    title = Gtk.Label(label=s.title or "Untitled", xalign=0.0)
    title.add_css_class("session-title")
    if not s.title:
        title.add_css_class("session-untitled")
    title.set_ellipsize(Pango.EllipsizeMode.END)
    box.append(title)

    records = load_records(s.dir)
    mins = duration_minutes(records)
    meta = s.start.strftime("%b %d  %H:%M")
    if mins:
        meta += f"  ·  {mins:.0f}m"
    line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    sub = Gtk.Label(label=meta, xalign=0.0)
    sub.add_css_class("session-meta")
    sub.set_hexpand(True)
    line.append(sub)
    if s.open_todos:
        badge = Gtk.Label(label=f"{s.open_todos} open", xalign=1.0)
        badge.add_css_class("todo-badge")
        line.append(badge)
    box.append(line)
    return box


def _markup(md: str) -> str:
    """Render the summary's small markdown subset as Pango markup.

    Deliberately not a markdown engine: the summary is written by a prompt we
    control, so headings, bullets and bold cover it, and anything unexpected
    should show as its own literal text rather than disappear.
    """
    out = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("# "):
            out.append(f"<span size='x-large' weight='bold'>{_escape(line[2:])}</span>")
        elif line.startswith("## "):
            out.append(f"\n<span weight='bold' size='large'>{_escape(line[3:])}</span>")
        elif line.startswith("### "):
            out.append(f"\n<span weight='bold'>{_escape(line[4:])}</span>")
        elif line.lstrip().startswith(("- ", "* ")):
            indent = " " * (len(line) - len(line.lstrip()))
            out.append(f"{indent}  •  {_inline(line.lstrip()[2:])}")
        else:
            out.append(_inline(line))
    return "\n".join(out)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _inline(text: str) -> str:
    """Bold and code spans, after escaping — order matters or the markup breaks."""
    out = _escape(text)
    for token, tag in (("**", "b"), ("`", "tt")):
        parts = out.split(token)
        if len(parts) >= 3:
            rebuilt = parts[0]
            for i, part in enumerate(parts[1:], start=1):
                rebuilt += (f"<{tag}>{part}</{tag}>" if i % 2 else part)
            out = rebuilt
    return out


def _reading_label() -> Gtk.Label:
    label = Gtk.Label(label="", xalign=0.0, yalign=0.0)
    label.set_wrap(True)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_selectable(True)
    label.set_margin_start(18)
    label.set_margin_end(18)
    label.set_margin_top(14)
    label.set_margin_bottom(18)
    return label


def _scrolled(child: Gtk.Widget) -> Gtk.Widget:
    scroll = Gtk.ScrolledWindow()
    scroll.set_child(child)
    scroll.set_vexpand(True)
    scroll.set_hexpand(True)
    return scroll


def _hairline() -> Gtk.Widget:
    line = Gtk.Box()
    line.add_css_class("hairline")
    return line


def _vhairline() -> Gtk.Widget:
    line = Gtk.Box()
    line.add_css_class("hairline")
    line.set_size_request(1, -1)
    return line


__all__ = ["ArchiveView", "Session", "time"]
