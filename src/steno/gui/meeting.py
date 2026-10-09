"""A past meeting, on one page: what was decided, what you owe, what was said.

Nothing is behind a tab. The summary leads, because a recording nobody replays
is worse than none; the to-dos, your notes and a question box sit beside it on a
wide window and below it on a narrow one; the transcript follows. Every
transcript line is a seek point, and the player stays pinned to the bottom,
because the only honest fix for "did they really say that?" is to hear it.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import threading
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from .. import asstlink
from ..export import default_filename, meeting_markdown
from ..notes import (
    Todo,
    ensure_todos,
    forget_asst,
    move_with_asst,
    pull_todos,
    set_done,
)
from ..playback import (
    audio_duration,
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
    list_projects,
    load_records,
    projects_root,
    save_project,
)
from . import words
from .player import Player
from .sidebar import Session
from .widgets import AskBox, NotesBox, hint_label, section, wrapping_label

NUDGE_S = 10.0
# asstd emits a D-Bus `Changed` on every task write, which is how a phone
# check-off arrives within a second. The timer is only a fallback for when
# that signal is missing (daemon restarted under us, no session bus).
PULL_S = 60

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

SLOTS = ("summary", "todos", "notes", "ask", "transcript")


def slot(name: str) -> Adw.LayoutSlot:
    return Adw.LayoutSlot.new(name)


class MeetingPage(Gtk.Box):
    def __init__(
        self,
        on_ask: Callable[[Path, str], None],
        on_realign: Callable[..., None] | None,
        on_deleted: Callable[[Path], None],
        on_session_changed: Callable[[Session], None],
        on_toast: Callable[[str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._on_ask = on_ask
        self._on_realign = on_realign
        self._on_deleted = on_deleted
        self._on_session_changed = on_session_changed
        self._toast = on_toast
        self._realigning = False
        self._pull_timer = 0
        self._pull_in_flight = False
        self._asst_watch: int | None = None
        self._asst_bus = None
        self.current: Session | None = None
        self._records: list[dict] = []
        self._offsets: list[float] = []
        self._rows: list[Gtk.ListBoxRow] = []
        self._playing_index = -1
        self._started_at = 0.0
        self.player = Player(on_tick=self._on_tick)

        self.layouts = Adw.MultiLayoutView()
        self.layouts.set_vexpand(True)
        self.layouts.add_layout(self._wide_layout())
        self.layouts.add_layout(self._stacked_layout())
        for name, widget in zip(SLOTS, self._build_slots(), strict=True):
            self.layouts.set_child(name, widget)
        self.layouts.set_layout_name("wide")
        self.append(self.layouts)
        self.append(self._build_player())

        self.actions = Gio.SimpleActionGroup()
        for name, handler in (
            ("copy", self._copy_meeting),
            ("save", self._save_meeting),
            ("project", self._set_project),
            ("realign", self._confirm_realign),
            ("delete-audio", self._confirm_delete_audio),
            ("delete", self._confirm_delete),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, fn=handler: fn())
            self.actions.add_action(action)

    # ------------------------------------------------------------------ layouts

    def _wide_layout(self) -> Adw.Layout:
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=26)
        main.set_margin_top(18)
        main.set_margin_bottom(24)
        main.set_margin_start(28)
        main.set_margin_end(28)
        main.append(slot("summary"))
        main.append(slot("transcript"))
        clamp = Adw.Clamp(maximum_size=760, tightening_threshold=600)
        clamp.set_halign(Gtk.Align.START)
        clamp.set_child(main)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(clamp)
        scroll.set_hexpand(True)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        side.add_css_class("side-column")
        side.add_css_class("beside")
        side.set_size_request(300, -1)
        # The notes view would otherwise pass its horizontal expansion up and
        # take half the page from the summary.
        side.set_hexpand(False)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        inner.set_margin_top(18)
        inner.set_margin_bottom(16)
        inner.set_margin_start(16)
        inner.set_margin_end(16)
        inner.set_vexpand(True)
        inner.append(slot("todos"))
        notes = slot("notes")
        notes.set_vexpand(True)
        inner.append(notes)
        inner.append(slot("ask"))
        side.append(inner)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row.append(scroll)
        row.append(side)
        layout = Adw.Layout.new(row)
        layout.set_name("wide")
        return layout

    def _stacked_layout(self) -> Adw.Layout:
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=26)
        column.add_css_class("stacked")
        column.set_margin_top(16)
        column.set_margin_bottom(24)
        column.set_margin_start(18)
        column.set_margin_end(18)
        for name in SLOTS:
            column.append(slot(name))
        clamp = Adw.Clamp(maximum_size=760, tightening_threshold=600)
        clamp.set_child(column)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(clamp)
        layout = Adw.Layout.new(scroll)
        layout.set_name("stacked")
        return layout

    def set_stacked(self, stacked: bool) -> None:
        self.layouts.set_layout_name("stacked" if stacked else "wide")

    # ----------------------------------------------------------------- building

    def _build_slots(self) -> list[Gtk.Widget]:
        self.summary_label = wrapping_label()
        self.summary_label.add_css_class("summary")
        summary = section("Summary", self.summary_label)

        self.todo_count = Gtk.Label(label="")
        self.todo_count.add_css_class("count")
        self.todo_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        todos = section("To do", self.todo_box, beside=self.todo_count)

        self.notes = NotesBox()
        notes = section("Your notes", self.notes)
        notes.set_vexpand(True)

        self.ask = AskBox(on_ask=self._ask)

        self.transcript_list = Gtk.ListBox()
        self.transcript_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.transcript_list.add_css_class("transcript-past")
        self.transcript_list.connect("row-activated", self._on_line_activated)
        self.transcript_hint = "Click a line to play from there"
        self.transcript = section("Transcript", self.transcript_list, hint=self.transcript_hint)
        return [summary, todos, notes, self.ask, self.transcript]

    def _build_player(self) -> Gtk.Widget:
        self.player_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.player_bar.add_css_class("player-bar")

        self.play_button = Gtk.Button.new_from_icon_name("media-playback-start-symbolic")
        self.play_button.set_tooltip_text("Play or pause  (Ctrl+Space)")
        self.play_button.connect("clicked", lambda _b: self.toggle_play())

        back = Gtk.Button.new_from_icon_name("media-seek-backward-symbolic")
        back.add_css_class("flat")
        back.set_tooltip_text(f"Back {NUDGE_S:.0f} seconds")
        back.connect("clicked", lambda _b: self.player.nudge(-NUDGE_S))

        self.position_label = Gtk.Label(label="00:00")
        self.position_label.add_css_class("mono")
        self.position_label.add_css_class("hint")

        self.scrubber = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.scrubber.set_draw_value(False)
        self.scrubber.set_hexpand(True)
        self.scrubber.set_valign(Gtk.Align.CENTER)
        self.scrubber.connect("change-value", self._on_scrub)

        self.duration_label = Gtk.Label(label="00:00")
        self.duration_label.add_css_class("mono")
        self.duration_label.add_css_class("hint")

        self.mix_choice = Gtk.DropDown.new_from_strings([label for label, _levels in MIXES])
        self.mix_choice.set_tooltip_text("Which side to listen to")
        self.mix_choice.set_valign(Gtk.Align.CENTER)
        self.mix_choice.connect("notify::selected", lambda *_a: self._apply_mix())

        for widget in (self.play_button, back, self.position_label, self.scrubber,
                       self.duration_label, self.mix_choice):
            self.player_bar.append(widget)
        return self.player_bar

    # ------------------------------------------------------------------ loading

    def _clear_hits(self) -> None:
        for row in self._rows:
            row.remove_css_class("line-hit")

    def jump_to(self, query: str) -> None:
        """Scroll the transcript to the first line a search matched.

        Called when the meeting is opened *from* a search. Without this the
        search finds the meeting and then abandons the reader at the top of
        it, which is the difference between finding a meeting and finding a
        moment.
        """
        if not query or not self._rows:
            return
        words = query.lower().split()
        first = -1
        for i, record in enumerate(self._records):
            if all(w in str(record.get("text", "")).lower() for w in words):
                if first < 0:
                    first = i
                self._rows[i].add_css_class("line-hit")
        if first < 0:
            return
        row = self._rows[first]
        # One idle pass: the rows have only just been added to the list box,
        # and scrolling before they are allocated does nothing.
        GLib.idle_add(self._scroll_to_row, row)

    def _scroll_to_row(self, row: Gtk.ListBoxRow) -> bool:
        self.transcript_list.select_row(row)
        mark = self.transcript_list.get_adjustment()
        if mark is None:
            mark = self.transcript_list.get_parent().get_vadjustment()
        if mark is not None:
            y = row.get_allocation().y
            mark.set_value(max(0, y - 48))
        row.grab_focus()
        return GLib.SOURCE_REMOVE

    def load(self, s: Session) -> None:
        if self.current is not None and self.current.dir != s.dir:
            self.stop_playback()
        self.current = s

        todos = ensure_todos(s.dir)
        if s.summary_path.is_file():
            body = words.summary_body(s.summary_path.read_text(), has_todos=bool(todos))
            self.summary_label.set_markup(words.markup(body))
        else:
            self.summary_label.set_markup(
                "<i>No summary yet.</i> It is written when a meeting ends; for an older "
                f"meeting, run <tt>steno summarize {words.escape(s.dir.name)}</tt>."
            )
        self._load_todos(s, todos)
        self.notes.bind(s.dir)
        self.ask.clear()
        self._load_transcript(s)
        self._arm_pull(s)

    def stop_pull(self) -> None:
        """Leave the meeting page: stop asking asst about this one."""
        if self._pull_timer:
            GLib.source_remove(self._pull_timer)
            self._pull_timer = 0

    def _arm_pull(self, s: Session) -> None:
        self.stop_pull()
        self._watch_asst()
        self._pull_timer = GLib.timeout_add_seconds(PULL_S, self._on_pull_tick)
        self._start_pull(s)

    def _watch_asst(self) -> None:
        """Follow asstd's Changed signal so a phone tick lands in a second.

        asst's CLI is only a client; the daemon owns the store and announces
        writes. Without the bus we fall back to the timer alone.
        """
        if self._asst_watch is not None:
            return
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return
        self._asst_bus = bus
        self._asst_watch = bus.signal_subscribe(
            "dev.jaeho.Asst.Daemon",
            "dev.jaeho.Asst1",
            "Changed",
            "/dev/jaeho/Asst",
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_asst_changed,
        )

    def _on_asst_changed(self, *_args) -> None:
        # A burst of signals (sync, a multi-edit) is one pull, already
        # coalesced by `_pull_in_flight`.
        if self.current is not None:
            self._start_pull(self.current)

    def _on_pull_tick(self) -> bool:
        if self.current is not None:
            self._start_pull(self.current)
        return GLib.SOURCE_CONTINUE

    def _start_pull(self, s: Session) -> None:
        """Read asst off-thread so a slow daemon cannot freeze the page."""
        if self._pull_in_flight:
            return
        self._pull_in_flight = True

        def work() -> None:
            todos = pull_todos(s.dir)
            GLib.idle_add(self._apply_pull, s, todos)

        threading.Thread(target=work, daemon=True).start()

    def _apply_pull(self, s: Session, todos: list[Todo]) -> bool:
        self._pull_in_flight = False
        # A slow pull must not paint the meeting the user has already left.
        if self.current is None or self.current.dir != s.dir:
            return GLib.SOURCE_REMOVE
        self._load_todos(s, todos)
        self._on_session_changed(s)
        return GLib.SOURCE_REMOVE

    def reload(self) -> None:
        if self.current is not None:
            self.load(self.current)

    def _load_todos(self, s: Session, todos: list[Todo]) -> None:
        while (child := self.todo_box.get_first_child()) is not None:
            self.todo_box.remove(child)
        yours = [t for t in todos if t.owner != "them"]
        theirs = [t for t in todos if t.owner == "them"]
        self.todo_count.set_text(words.todo_count(sum(not t.done for t in yours)))
        if not todos:
            self.todo_box.append(hint_label("Nothing to do out of this one."))
            return
        # Two groups, not one list with a tiny owner tag. Your work is the
        # to-do; what they owe is a reminder, so it sits under its own head.
        if yours:
            self.todo_box.append(self._group_label("Yours"))
            for todo in yours:
                self.todo_box.append(self._todo_row(s, todo))
        if theirs:
            self.todo_box.append(self._group_label("They owe"))
            for todo in theirs:
                self.todo_box.append(self._todo_row(s, todo))

    def _group_label(self, text: str) -> Gtk.Widget:
        label = Gtk.Label(label=text, xalign=0.0)
        label.add_css_class("sec")
        label.set_margin_top(4 if self.todo_box.get_first_child() is None else 10)
        return label

    def _todo_row(self, s: Session, todo: Todo) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        check = Gtk.CheckButton()
        check.set_active(todo.done)
        check.set_valign(Gtk.Align.START)
        check.set_hexpand(True)

        label = wrapping_label(todo.text, selectable=False)
        # Wrap at a column's width instead of asking for the whole sentence.
        label.set_max_width_chars(30)
        label.set_hexpand(True)
        if todo.done:
            label.add_css_class("todo-done")
        # Clicking the words ticks the box, as it would in any checklist.
        check.set_child(label)

        row.append(check)
        # asst list, right-aligned. The group header already says whose it is.
        if todo.list_name:
            where = Gtk.Label(label=todo.list_name, xalign=1.0)
            where.add_css_class("hint")
            where.add_css_class("todo-list")
            where.set_valign(Gtk.Align.START)
            where.set_margin_top(4)
            row.append(where)

        def toggled(button):
            set_done(s.dir, todo.key, button.get_active())
            label.remove_css_class("todo-done")
            if button.get_active():
                label.add_css_class("todo-done")
            self.todo_count.set_text(
                words.todo_count(
                    sum(not t.done and t.owner != "them"
                        for t in ensure_todos(s.dir))
                )
            )
            self._on_session_changed(s)

        check.connect("toggled", toggled)
        return row

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
        self._clear_hits()
        if not self._records:
            self.transcript_list.append(hint_label("No transcript."))
        self._update_player(s)

    def _update_player(self, s: Session) -> None:
        tracks = session_tracks(s.dir)
        playable = bool(tracks) and audio_bytes(s.dir) > 0 and self.player.available()
        self.player_bar.set_visible(playable)
        duration = max((audio_duration(p) for p in tracks.values()), default=0.0)
        self.scrubber.set_range(0, max(1.0, duration))
        self.scrubber.set_value(0)
        self.position_label.set_text(format_offset(0))
        self.duration_label.set_text(format_offset(duration))

    # ----------------------------------------------------------------- playback

    def _apply_mix(self) -> None:
        _label, levels = MIXES[min(self.mix_choice.get_selected(), len(MIXES) - 1)]
        self.player.set_levels(levels)

    def toggle_play(self) -> None:
        """Play, pause, or start from the top.

        A play button that does nothing until you have already clicked a line
        reads as broken, so with nothing loaded this starts from the beginning.
        """
        if not self.player_bar.get_visible():
            return
        if not self.player.tracks:
            self._play_at(0.0)
            return
        self.player.toggle()

    def _play_at(self, seconds: float) -> None:
        if self.current is None:
            return
        tracks = session_tracks(self.current.dir)
        if not tracks:
            self._toast("The audio for this meeting is gone")
            return
        self._apply_mix()
        self.player.play(tracks, seconds)

    def _play_line(self, index: int) -> None:
        """Play the meeting from where this line starts.

        Both sides, on one timeline. The offsets are measured from the same
        instant in both files, so a line's position in its own track is its
        position in the mix, and the reply that follows it arrives on time.
        """
        if not 0 <= index < len(self._records):
            return
        self._play_at(self._offsets[index])
        self._highlight(index)

    def _on_line_activated(self, _list, row) -> None:
        self._play_line(row.get_index())

    def _on_scrub(self, _scale, _scroll, value: float) -> bool:
        if self.player.tracks:
            self.player.seek(value)
        else:
            self._play_at(value)
        return False

    def _on_tick(self, position: float, duration: float, playing: bool) -> None:
        icon = "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic"
        self.play_button.set_icon_name(icon)
        self.position_label.set_text(format_offset(position))
        if duration > 0:
            self.scrubber.set_range(0, duration)
            self.duration_label.set_text(format_offset(duration))
        self.scrubber.set_value(position)
        index = index_at(self._offsets, position)
        if index >= 0:
            self._highlight(index)

    def _highlight(self, index: int) -> None:
        if index == self._playing_index or not 0 <= index < len(self._rows):
            return
        if 0 <= self._playing_index < len(self._rows):
            self._rows[self._playing_index].remove_css_class("line-playing")
        self._rows[index].add_css_class("line-playing")
        self._playing_index = index

    def stop_playback(self) -> None:
        """Let go of the audio device — on window close, and between meetings."""
        self.player.stop()
        if 0 <= self._playing_index < len(self._rows):
            self._rows[self._playing_index].remove_css_class("line-playing")
        self._playing_index = -1
        self.play_button.set_icon_name("media-playback-start-symbolic")

    # -------------------------------------------------------------------- asking

    def _ask(self, question: str) -> None:
        if self.current is not None:
            self._on_ask(self.current.dir, question)

    # ------------------------------------------------------------ this meeting

    def _copy_meeting(self) -> None:
        s = self.current
        if s is None:
            return
        self.flush_notes()
        md = meeting_markdown(s.dir)
        clipboard = self.get_clipboard()
        if clipboard is None:
            display = Gdk.Display.get_default()
            clipboard = display.get_clipboard() if display is not None else None
        if clipboard is None:
            self._toast("No clipboard available")
            return
        clipboard.set(md)
        self._toast("Copied this meeting")

    def _save_meeting(self) -> None:
        s = self.current
        if s is None:
            return
        self.flush_notes()
        dialog = Gtk.FileDialog()
        dialog.set_initial_name(default_filename(s.dir))

        def on_save(dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                file = dlg.save_finish(result)
            except GLib.Error:
                return  # cancelled
            if file is None:
                return
            path = file.get_path()
            if path is None:
                self._toast("Could not write that file")
                return
            try:
                Path(path).write_text(meeting_markdown(s.dir))
            except OSError as e:
                self._toast(f"Could not save: {e}")
                return
            self._toast(f"Saved to {Path(path).name}")

        root = self.get_root()
        parent = root if isinstance(root, Gtk.Window) else None
        dialog.save(parent, None, on_save)

    def _set_project(self) -> None:
        s = self.current
        if s is None:
            return
        self.flush_notes()
        names = list(list_projects(projects_root()))
        # A link to a directory that has since been renamed or deleted must
        # still be visible, or the picker would silently drop it on save.
        if s.project and s.project not in names:
            names.insert(0, s.project)
        labels = ["None", *names]
        choice = Gtk.DropDown.new_from_strings(labels)
        try:
            choice.set_selected(labels.index(s.project) if s.project else 0)
        except ValueError:
            choice.set_selected(0)

        dialog = Adw.AlertDialog(
            heading="Project",
            body="Which project is this meeting about?",
        )
        dialog.set_extra_child(choice)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")

        def on_response(_d, response: str) -> None:
            if response != "save":
                return
            index = choice.get_selected()
            project = names[index - 1] if index >= 1 else None
            save_project(s.dir, project)
            s.project = project
            todos = ensure_todos(s.dir)
            list_name = asstlink.target_list(s.dir, project)
            moved = move_with_asst(s.dir, todos, list_name)
            if moved:
                self._load_todos(s, todos)
            self._on_session_changed(s)
            where = list_name or "Inbox"
            base = f"Project: {project}" if project else "Project cleared"
            if moved:
                base = f"{base} · {moved} task(s) → {where}"
            self._toast(base)

        dialog.connect("response", on_response)
        dialog.present(self)

    def _dialog(self, heading: str, body: str, confirm: str, on_confirm,
                destructive: bool = True) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("go", confirm)
        dialog.set_response_appearance(
            "go",
            Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED,
        )
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
        if s.open_todos:
            parts.append(
                f"{s.open_todos} open to-do{'s' if s.open_todos != 1 else ''}"
            )
        return " · ".join(parts)

    def _confirm_delete(self) -> None:
        s = self.current
        if s is None:
            return
        self._dialog(
            f"Delete “{s.title or s.dir.name}”?",
            f"{self._describe(s)}\n\n"
            "The recording, transcript, summary, to-dos and your notes all go to "
            "the trash together. Your action items are also removed from asst.",
            "Move to trash",
            lambda: self._delete(s),
        )

    def _delete(self, s: Session) -> None:
        self.stop_pull()
        self.stop_playback()
        self.notes.bind(None)
        forget_asst(s.dir)
        outcome, where = delete_session(s.dir)
        if outcome == "failed":
            self._dialog(
                "Could not move it to the trash",
                f"{s.dir} is still there. This usually means the trash is on a "
                "different filesystem.\n\nDeleting it permanently cannot be undone.",
                "Delete permanently",
                lambda: self._delete_permanently(s),
            )
            return
        self.current = None
        self._on_deleted(s.dir)
        self._toast(f"Moved to trash: {where.name if where else s.dir.name}")

    def _delete_permanently(self, s: Session) -> None:
        forget_asst(s.dir)
        outcome, _ = delete_session(s.dir, permanent=True)
        if outcome == "deleted":
            self.current = None
            self._on_deleted(s.dir)

    def _confirm_delete_audio(self) -> None:
        s = self.current
        if s is None:
            return
        size = audio_bytes(s.dir)
        if not size:
            self._toast("This meeting has no audio to delete")
            return
        self._dialog(
            "Delete the audio for this meeting?",
            f"Frees {human_bytes(size)}. The transcript, summary, to-dos and your "
            "notes stay.\n\nPlayback will no longer work for this meeting, and "
            "the recording is not recoverable.",
            "Delete audio",
            lambda: self._delete_audio(s),
        )

    def _delete_audio(self, s: Session) -> None:
        self.stop_playback()
        freed = delete_audio(s.dir)
        self._load_transcript(s)
        self._toast(f"Freed {human_bytes(freed)}")

    def _confirm_realign(self) -> None:
        s = self.current
        if s is None or self._on_realign is None or self._realigning:
            return
        job = plan(s.dir)
        if job.minutes <= 0:
            self._toast("This meeting has no audio to re-time against")
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
        self._toast("Re-timing against the audio…")
        self._on_realign(s.dir, self._realigned)

    def _realigned(self, session_dir: Path, lines: int, error: str) -> None:
        """Called back on the GTK thread when the pass finishes."""
        self._realigning = False
        if error:
            self._toast(f"Re-timing failed: {error}")
            return
        self._toast(f"Re-timed {lines} lines")
        if self.current is not None and self.current.dir == session_dir:
            self.reload()

    def flush_notes(self) -> None:
        self.notes.flush()


def _transcript_row(record: dict, started_at: float) -> Gtk.ListBoxRow:
    """One line of transcript, and one seek point."""
    row = Gtk.ListBoxRow()
    row.set_activatable(True)
    speaker = str(record.get("speaker", "them"))

    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    box.add_css_class("transcript-line")
    box.add_css_class("from-them" if speaker == "them" else "from-you")

    stamp = Gtk.Label(label=format_offset(display_offset(record, started_at)), xalign=1.0)
    stamp.add_css_class("line-stamp")
    stamp.set_valign(Gtk.Align.START)
    stamp.set_width_chars(5)

    tag = Gtk.Label(label="Them" if speaker == "them" else "You", xalign=0.0)
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
