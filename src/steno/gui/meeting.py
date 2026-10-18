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

from .. import asstlink, citations, projectnotes
from ..cost import SUMMARY_GUESS, format_usd, prerecorded_cost
from ..export import default_filename, meeting_markdown
from ..notes import (
    Todo,
    deliver_done,
    ensure_todos,
    forget_asst,
    move_with_asst,
    pull_todos,
    set_done_local,
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
    load_meta,
    load_records,
    projects_root,
    save_project,
    set_archived,
)
from . import words
from .player import Player
from .republish import QUIT_WAIT_S, Republisher
from .sidebar import Session
from .summary import CiteBar, CitedBox, CiteSource, SummaryView, make_citable
from .widgets import AskBox, NotesBox, hint_label, section, wrapping_label

NUDGE_S = 10.0
# asstd emits a D-Bus `Changed` on every task write, which is how a phone
# check-off arrives within a second. The timer is only a fallback for when
# that signal is missing (daemon restarted under us, no session bus).
PULL_S = 60
# A search jump asks every JUMP_POLL_MS whether the transcript has been laid
# out, and drops the jump after JUMP_GIVE_UP_US (a window that never shows).
JUMP_POLL_MS = 16
JUMP_GIVE_UP_US = 2 * GLib.USEC_PER_SEC

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
    def __init__(  # noqa: PLR0913, PLR0917 - one callback per gesture the page hands up
        self,
        on_ask: Callable[[Path, str], None],
        on_realign: Callable[..., None] | None,
        on_deleted: Callable[[Path], None],
        on_session_changed: Callable[[Session], None],
        on_toast: Callable[..., None],
        on_summarize: Callable[[Path], None] | None = None,
        is_recording: Callable[[], bool] | None = None,
        recording_dir: Callable[[], Path | None] | None = None,
        republisher: Republisher | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        # What keeps the meeting's pages in its project current; None
        # keeps nothing.
        self._republisher = republisher
        self._on_ask = on_ask
        self._on_summarize = on_summarize
        self._is_recording = is_recording or (lambda: False)
        # The folder being recorded into, if any: the one meeting that
        # cannot be put away.
        self._recording_dir = recording_dir or (lambda: None)
        # session dir -> what is being done to it ("Writing the summary…").
        self._working: dict[Path, str] = {}
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
        # How much of the summary on the page cites, for the note by its
        # heading.
        self._seen = citations.Coverage()
        self._offsets: list[float] = []
        # Each record's second on the gutter's clock, which is the clock a
        # cite is written in; `_offsets` is where its audio starts.
        self._display: list[int] = []
        self._rows: list[Gtk.ListBoxRow] = []
        self._playing_index = -1
        # What was last asked where it was said, and the lines that answer.
        self._active: CiteSource | None = None
        self._cited: list[int] = []
        self._places: list[words.Place] = []
        self._place = -1  # the one in `_places` the page was last taken to
        # When an item last took the page to its lines (GLib monotonic µs).
        self._jumped_at = 0
        # The to-dos on the page that cite, to find the lit one again when
        # the list is rebuilt.
        self._todo_sources: list[CiteSource] = []
        # Which to-dos are ticked, as last shown: a pull that finds
        # another state changed something the project's copy shows.
        self._ticks: dict[str, bool] = {}
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
        self.cite_bar = CiteBar(
            on_step=self._step_place,
            on_pick=self._pick_place,
            on_close=self._clear_cites,
        )
        self.append(self.cite_bar)
        self.append(self._build_player())

        # Esc puts the transcript back as it was, from wherever in the page.
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_page_key)
        self.add_controller(keys)

        self.actions = Gio.SimpleActionGroup()
        for name, handler in (
            ("copy", self._copy_meeting),
            ("save", self._save_meeting),
            ("project", self._set_project),
            ("show-notes", self._show_notes),
            ("summarize", self._confirm_summarize),
            ("realign", self._confirm_realign),
            ("archive", self._archive),
            ("unarchive", self._unarchive),
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
        self.summary_view = SummaryView(self._toggle_cites)
        # Quiet, beside the heading: a summary from before citations has
        # nothing wrong, and is not nagged about. While one is being
        # written over it says so instead, since the old text stays up.
        self.summary_hint = hint_label()
        self.summary_hint.set_visible(False)
        self.summary_hint.connect("activate-link", self._on_hint_link)
        self.summarize_button = Gtk.Button(label="Summarize now")
        self.summarize_button.set_halign(Gtk.Align.START)
        self.summarize_button.set_visible(False)
        self.summarize_button.connect(
            "clicked", lambda _b: self._summarize_now()
        )
        summary = section(
            "Summary", self.summary_view, self.summary_label,
            self.summarize_button, beside=self.summary_hint,
        )

        self.todo_count = Gtk.Label(label="")
        self.todo_count.add_css_class("count")
        self.todo_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        todos = section("To do", self.todo_box, beside=self.todo_count)

        self.notes = NotesBox(on_saved=self._changed)
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
        # The rows were only just added, and the page may not be on screen
        # yet. The frame clock lays them out when it next runs, which can
        # come after any idle pass, and until then the scroller has no
        # extent to move in: set_value clamps to nothing. So ask until it
        # has one.
        give_up = GLib.get_monotonic_time() + JUMP_GIVE_UP_US
        GLib.timeout_add(
            JUMP_POLL_MS, self._scroll_to_row, self._rows[first], give_up
        )

    def _scroll_to_row(self, row: Gtk.ListBoxRow, give_up: int) -> bool:
        """Bring a transcript line to just under the top of whichever scroller
        holds it (the wide and the stacked layouts have their own).

        Runs until the line has a place; True means ask again.
        """
        scroller = row.get_ancestor(Gtk.ScrolledWindow)
        if scroller is None:  # another meeting replaced the rows
            return GLib.SOURCE_REMOVE
        if row.get_height() == 0:  # not laid out yet
            return GLib.get_monotonic_time() < give_up
        content = scroller.get_child()
        if isinstance(content, Gtk.Viewport):
            content = content.get_child()
        # (x, y), or None when they share no ancestor: pygobject's Gtk
        # override drops the success flag the C call returns.
        point = row.translate_coordinates(content, 0, 0)
        if point is not None:
            scroller.get_vadjustment().set_value(max(0.0, point[1] - 48))
        return GLib.SOURCE_REMOVE

    def load(self, s: Session) -> None:
        left = self.current
        if left is not None and left.dir != s.dir:
            self.stop_playback()
        self.current = s

        todos = ensure_todos(s.dir)
        # Saves the notes of the meeting left, which the next line writes up.
        self.notes.bind(s.dir)
        if left is not None and left.dir != s.dir and self._republisher:
            self._republisher.leave(left.dir)
        self.ask.clear()
        self._load_transcript(s)
        self._show_summary(s, todos)
        self._arm_pull(s)

    def _show_summary(self, s: Session, todos: list[Todo]) -> None:
        """The summary slot and the to-do list, which depend on the same file.

        With no summary the slot says why and, when it can be fixed from here,
        offers to: a failed summary must not be a note to go and use a terminal.
        """
        # Whatever was lit belongs to the summary being replaced.
        self._clear_cites()
        if s.summary_path.is_file():
            md = s.summary_path.read_text()
            self.summary_view.show(words.summary_blocks(md, has_todos=bool(todos)))
            self._seen = citations.coverage(md)
            self._show_note(s)
            self.summary_view.set_visible(True)
            self.summary_label.set_visible(False)
            self.summarize_button.set_visible(False)
        else:
            text, offer = words.summary_missing(
                len(self._records), self._working.get(s.dir, "")
            )
            self.summary_view.clear()
            self.summary_view.set_visible(False)
            self._seen = citations.Coverage()
            self._show_note(s)
            self.summary_label.set_markup(f"<i>{words.escape(text)}</i>")
            self.summary_label.set_visible(True)
            self.summarize_button.set_visible(offer and self._on_summarize is not None)
        self._load_todos(s, todos)
        self._update_actions()

    def _show_note(self, s: Session) -> None:
        """The line by the Summary heading, for the summary on the page.

        A meeting with no summary says what is happening in the slot
        below instead, so the heading has nothing to add.
        """
        has_summary = s.summary_path.is_file()
        note = words.summary_note(
            self._seen, self._working.get(s.dir, "") if has_summary else ""
        )
        self.summary_hint.set_markup(note)
        self.summary_hint.set_visible(bool(note))

    def _can_summarize(self) -> bool:
        """Not while it is being written (a second ask would only be
        told it is already running), and not with nothing to write it
        from."""
        s = self.current
        return (
            s is not None
            and self._on_summarize is not None
            and bool(self._records)
            and s.dir not in self._working
        )

    def _can_archive(self) -> bool:
        """Not the meeting being recorded (its folder is still being
        written), and not one that is put away already."""
        s = self.current
        return (
            s is not None
            and not s.archived
            and s.dir != self._recording_dir()
        )

    def _update_actions(self) -> None:
        """Switch off what the open meeting cannot do right now."""
        self.actions.lookup_action("summarize").set_enabled(
            self._can_summarize()
        )
        self.actions.lookup_action("archive").set_enabled(
            self._can_archive()
        )
        self.actions.lookup_action("unarchive").set_enabled(
            self.current is not None and self.current.archived
        )

    def show_summary(self, s: Session) -> None:
        """A summary arrived for the open meeting.

        Not `load`: that rebinds the notes (cursor to the top of what you are
        typing), clears an answer in flight, and rebuilds the transcript under
        your scroll position.
        """
        self.current = s
        self._working.pop(s.dir, None)
        # The batch pass of your side was merged into the transcript before
        # the summary was written; the page still shows the far side alone.
        self.refresh_transcript()
        self._show_summary(s, ensure_todos(s.dir))

    def refresh_transcript(self, session_dir: Path | None = None) -> None:
        """Re-read the transcript if it changed on disk. Notes and the ask box
        are left alone; an unchanged transcript is not rebuilt (scroll stays)."""
        s = self.current
        if s is None or (session_dir is not None and s.dir != session_dir):
            return
        if load_records(s.dir) != self._records:
            # New rows have none of the lines lit; light them again.
            active = self._active
            self._load_transcript(s)
            self._update_actions()
            if active is not None:
                self._show_cites(active, scroll=False)

    def mark_working(self, session_dir: Path, text: str) -> None:
        """Say what is being done to a meeting, or (empty text) that it is done."""
        if text:
            self._working[session_dir] = text
        else:
            self._working.pop(session_dir, None)
        current = self.current
        if current is None or current.dir != session_dir:
            return
        if current.summary_path.is_file():
            # The summary being written over stays up, and whatever is
            # lit in it: the note by its heading is all that changes.
            self._show_note(current)
            self._update_actions()
        else:
            self._show_summary(current, ensure_todos(session_dir))

    def clear_working(self, session_dir: Path | None = None) -> None:
        """Whatever was being written failed: stop saying it is in progress.

        For that meeting when the failure says which; otherwise for all, since
        that is the only safe reading of an error that names none.
        """
        for pending in [session_dir] if session_dir is not None else list(self._working):
            self.mark_working(pending, "")

    def _summarize_now(self) -> None:
        """The button of a meeting that has no summary."""
        if self.current is not None:
            self._summarize(self.current)

    def _summarize(self, s: Session) -> None:
        if self._on_summarize is None:
            return
        self.mark_working(s.dir, "Writing the summary…")
        self._on_summarize(s.dir)

    def _on_hint_link(self, _label: Gtk.Label, href: str) -> bool:
        if href != words.SUMMARIZE_AGAIN:
            return False
        # Through the action, so that it is as switched off as the menu
        # is.
        self.actions.activate_action("summarize", None)
        return True

    def _summary_cost(self, s: Session) -> float:
        """What writing the summary will cost, in dollars: the model,
        and your side's transcription too when the engine has to do
        that first (a meeting quit before its mic was transcribed)."""
        cost = SUMMARY_GUESS
        pending = load_meta(s.dir).get("mic_pending")
        if pending and (s.dir / "you.wav").is_file():
            cost += prerecorded_cost(s.minutes)
        return cost

    def _confirm_summarize(self) -> None:
        s = self.current
        if s is None or not self._can_summarize():
            return
        again = s.summary_path.is_file()
        body = (
            "The model reads the transcript and writes the summary and the "
            "to-do list"
            + (" again" if again else "")
            + ", with a citation on each point it can support. It costs about "
            f"{format_usd(self._summary_cost(s))}."
        )
        if again:
            body += (
                "\n\nThe new summary replaces this one. Your notes, the "
                "project and the to-dos you ticked are kept."
            )
        self._dialog(
            "Summarize this meeting again?" if again else "Write the summary?",
            body,
            "Summarize",
            lambda: self._summarize(s),
            destructive=False,
        )

    def stop_pull(self) -> None:
        """Leave the meeting page: stop asking asst about this one."""
        if self._pull_timer:
            GLib.source_remove(self._pull_timer)
            self._pull_timer = 0

    def leave(self) -> None:
        """The page is put away for another (the recording, or none): what
        was typed is saved, and the meeting's project notes are written now
        rather than in a minute."""
        self.stop_pull()
        self.notes.flush()
        if self.current is not None and self._republisher is not None:
            self._republisher.leave(self.current.dir)

    def close_meeting(self) -> None:
        """No meeting is open any more. The page is out of sight, and its
        player buttons out of reach, so the audio must not go on; and a
        meeting nobody sees is not asked about."""
        self.stop_playback()
        self.current = None

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

    def pull_now(self) -> None:
        """The window was shown again: catch up with what the phone did."""
        if self.current is not None:
            self._start_pull(self.current)

    def _on_asst_changed(self, *_args) -> None:
        # A burst of signals (sync, a multi-edit) is one pull, already
        # coalesced by `_pull_in_flight`.
        if self.current is not None and self.get_mapped():
            self._start_pull(self.current)

    def _on_pull_tick(self) -> bool:
        # The window is usually closed. A page nobody can see has no use for
        # a subprocess every minute; opening it pulls again (`load`).
        if self.current is not None and self.get_mapped():
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
        if _ticks(todos) != self._ticks:  # a tick made on the phone
            self._changed(s.dir)
        self._load_todos(s, todos)
        self._on_session_changed(s)
        return GLib.SOURCE_REMOVE

    def reload(self) -> None:
        if self.current is not None:
            self.load(self.current)

    def _changed(self, session_dir: Path) -> None:
        """A to-do, a note or the transcript changed: the meeting's pages
        in its project follow, a minute after the last change."""
        if self._republisher is not None:
            self._republisher.changed(session_dir)

    def _load_todos(self, s: Session, todos: list[Todo]) -> None:
        while (child := self.todo_box.get_first_child()) is not None:
            self.todo_box.remove(child)
        self._todo_sources = []
        self._ticks = _ticks(todos)
        yours = [t for t in todos if t.owner != "them"]
        theirs = [t for t in todos if t.owner == "them"]
        self.todo_count.set_text(words.todo_count(sum(not t.done for t in yours)))
        if not todos:
            self.todo_box.append(hint_label(
                "Nothing to do out of this one." if s.summary_path.is_file()
                else "To-dos come with the summary."
            ))
            self._keep_todo_cite()
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
        self._keep_todo_cite()

    def _keep_todo_cite(self) -> None:
        """The to-do list was rebuilt (asst answered, a project moved the
        tasks): a to-do that was lit is still the one lit."""
        active = self._active
        if active is None or active.ref[0] != "todo":
            return
        for source in self._todo_sources:
            if source.ref == active.ref:
                source.mark.add_css_class("item-active")
                self._active = source
                return
        self._clear_cites()

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
        if todo.cites:
            # Clicking these words shows where they were said, so they stand
            # beside the box, which still ticks.
            check.set_hexpand(False)
            label.set_valign(Gtk.Align.START)
            # Where the box's own label would sit, in its padding, so the
            # words stay in line with those of a to-do that cites nothing.
            label.set_margin_top(3)
            words_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=1)
            words_row.set_hexpand(True)
            words_row.append(check)
            clickable = CitedBox(orientation=Gtk.Orientation.HORIZONTAL)
            clickable.set_hexpand(True)
            clickable.append(label)
            words_row.append(clickable)
            row.append(words_row)
            source = CiteSource(
                row, todo.cites, words.escape(todo.text), ("todo", todo.key)
            )
            make_citable(clickable, source, self._toggle_cites)
            self._todo_sources.append(source)
        else:
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
            done = button.get_active()
            # The tick itself is a local file write. Telling asst is a
            # subprocess that can take seconds when its daemon is struggling,
            # so it never runs on the thread that draws the window.
            now = set_done_local(s.dir, todo.key, done)
            threading.Thread(
                target=deliver_done, args=(s.dir, todo.key, done),
                name="steno-asst", daemon=True,
            ).start()
            label.remove_css_class("todo-done")
            if done:
                label.add_css_class("todo-done")
            self.todo_count.set_text(
                words.todo_count(sum(not t.done and t.owner != "them" for t in now))
            )
            self._ticks = _ticks(now)
            self._changed(s.dir)
            self._on_session_changed(s)

        check.connect("toggled", toggled)
        return row

    def _load_transcript(self, s: Session) -> None:
        while (row := self.transcript_list.get_first_child()) is not None:
            self.transcript_list.remove(row)
        self._clear_cites()
        self._records = load_records(s.dir)
        # Repairs and caches `started_at` for sessions recorded before the tool
        # wrote one, so the gutter and playback agree from the first click.
        self._started_at = repair_started_at(s.dir, self._records)
        self._offsets = [seek_offset(r, self._started_at) for r in self._records]
        self._display = [
            display_offset(r, self._started_at) for r in self._records
        ]
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
        if self._is_recording():
            self.stop_playback()
            return
        self.player.toggle()

    def _play_at(self, seconds: float) -> None:
        if self.current is None:
            return
        if self._is_recording():
            # The far side is recorded from the default sink's monitor, so
            # whatever plays here would be transcribed as the other person.
            self._toast("Playback is off while recording; it would be recorded as them")
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
        # The second click of a double click on an item lands where the
        # first one took the page: on a transcript line, which would start
        # playing. A click that soon after is not meant for the line.
        settings = Gtk.Settings.get_default()
        window_ms = settings.props.gtk_double_click_time if settings else 400
        if GLib.get_monotonic_time() - self._jumped_at < window_ms * 1000:
            return
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
        following = True
        if 0 <= self._playing_index < len(self._rows):
            old = self._rows[self._playing_index]
            # Follow only a reader who is following: if they scrolled away,
            # pulling the view back every few seconds would be a fight.
            following = self._row_in_view(old) is not False
            old.remove_css_class("line-playing")
        self._rows[index].add_css_class("line-playing")
        self._playing_index = index
        if following:
            self._bring_into_view(self._rows[index])

    @staticmethod
    def _row_frame(row: Gtk.ListBoxRow):
        """(scroller, y within its content, row height), or None if unplaced."""
        scroller = row.get_ancestor(Gtk.ScrolledWindow)
        if scroller is None:
            return None
        content = scroller.get_child()
        if isinstance(content, Gtk.Viewport):
            content = content.get_child()
        point = row.translate_coordinates(content, 0, 0)  # (x, y) or None
        return None if point is None else (scroller, point[1], row.get_height())

    def _row_in_view(self, row: Gtk.ListBoxRow) -> bool | None:
        frame = self._row_frame(row)
        if frame is None:
            return None
        scroller, y, height = frame
        adj = scroller.get_vadjustment()
        return y >= adj.get_value() and y + height <= adj.get_value() + adj.get_page_size()

    def _bring_into_view(self, row: Gtk.ListBoxRow) -> None:
        """Scroll just enough that the playing line is on screen. No focus."""
        if self._row_in_view(row) is not False:
            return
        frame = self._row_frame(row)
        if frame is not None:
            scroller, y, _height = frame
            scroller.get_vadjustment().set_value(max(0.0, y - 80))

    # ------------------------------------------------------------ where it was said

    def _toggle_cites(self, source: CiteSource) -> None:
        """An item was chosen: light the lines it came from, or, if it was
        the one already lit, put them out."""
        if source is self._active:
            self._clear_cites()
        else:
            self._show_cites(source, scroll=True)
            self._jumped_at = GLib.get_monotonic_time()

    def _show_cites(self, source: CiteSource, scroll: bool) -> None:
        self._clear_cites()
        places = words.cite_places(source.cites, self._records, self._display)
        if not places:
            self._toast("Those lines are not in this transcript")
            return
        self._active = source
        source.mark.add_css_class("item-active")
        self._cited = sorted({i for place in places for i in place.rows})
        for i in self._cited:
            self._rows[i].add_css_class("line-cited")
        self._places = places
        self.cite_bar.show_places(
            source.markup, places, playable=self.player_bar.get_visible()
        )
        self._place = 0
        self.cite_bar.set_current(0)
        if scroll:
            self._bring_into_view(self._rows[self._cited[0]])

    def _go_to_place(self, index: int) -> None:
        self._place = index
        self.cite_bar.set_current(index)
        row = self._rows[self._places[index].rows[0]]
        # Now if the line has a place, else as a search jump asks.
        give_up = GLib.get_monotonic_time() + JUMP_GIVE_UP_US
        if self._scroll_to_row(row, give_up):
            GLib.timeout_add(JUMP_POLL_MS, self._scroll_to_row, row, give_up)

    def _step_place(self, delta: int) -> None:
        if 0 <= self._place + delta < len(self._places):
            self._go_to_place(self._place + delta)

    def _pick_place(self, index: int) -> None:
        """A place was chosen in the bar: take the transcript there and play
        from it. With no audio, or while recording, `_play_at` says why it
        did not, and the transcript has moved all the same."""
        self._go_to_place(index)
        self._play_line(self._places[index].rows[0])

    def _clear_cites(self) -> None:
        if self._active is not None:
            self._active.mark.remove_css_class("item-active")
        for i in self._cited:
            if i < len(self._rows):
                self._rows[i].remove_css_class("line-cited")
        self._active = None
        self._cited = []
        self._places = []
        self._place = -1
        self.cite_bar.hide()

    def _on_page_key(self, _keys, key: int, _code: int, _state) -> bool:
        if key != Gdk.KEY_Escape or self._active is None:
            return False
        self._clear_cites()
        return True

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
            # Into the new project's notes and out of the old one's; the
            # menu hears of it when that is done.
            if self._republisher is not None:
                self._republisher.publish(
                    s.dir, then=lambda _r: self._on_session_changed(s)
                )

        dialog.connect("response", on_response)
        dialog.present(self)

    def _show_notes(self) -> None:
        """Open the folder in the project that holds this meeting's pages."""
        s = self.current
        folder = None if s is None else projectnotes.published_folder(s.dir)
        if folder is None:
            self._toast("This meeting is not in a project's notes")
            return
        self._open_folder(folder)

    def _open_folder(self, folder: Path) -> None:
        """Hand a folder to whatever the desktop opens folders with."""
        root = self.get_root()

        def opened(launcher: Gtk.FileLauncher, result: Gio.AsyncResult) -> None:
            try:
                launcher.launch_finish(result)
            except GLib.Error as err:
                self._toast(f"Could not open {folder.name}: {err.message}")

        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(folder)))
        launcher.launch(root if isinstance(root, Gtk.Window) else None, None, opened)

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

    def _archive(self) -> None:
        """Put the open meeting away. No question: nothing is lost, and
        the toast takes it back."""
        s = self.current
        if s is None or not self._can_archive():
            return
        self.flush_notes()
        set_archived(s.dir, True)
        # The list takes the page to the next meeting, or keeps it here
        # when the archive is open.
        self._on_session_changed(s)
        self._update_actions()
        self._toast(
            words.archived_toast(s.title or s.dir.name, s.open_todos),
            undo=lambda: self._bring_back(s),
        )

    def _unarchive(self) -> None:
        if self.current is not None and self.current.archived:
            self._bring_back(self.current)

    def _bring_back(self, s: Session) -> None:
        if not s.dir.is_dir():  # an Undo of a meeting deleted since
            return
        set_archived(s.dir, False)
        self._on_session_changed(s)
        self._update_actions()

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

    def _take_out_of_project(self, s: Session) -> bool:
        """Remove the meeting's pages from its project before the meeting
        goes: the record of where they are goes with it. Says so in a
        toast if some stayed. True if any were taken out."""
        if self._republisher is not None:
            self._republisher.forget(s.dir)
            # A rewrite that has begun finishes first, or it could put the
            # pages back after they were taken out.
            self._republisher.wait(QUIT_WAIT_S)
        gone = projectnotes.try_unpublish(s.dir)
        if problem := words.project_notes_problem(gone):
            self._toast(problem)
        return bool(gone.altered)

    def _delete(self, s: Session) -> None:
        self.stop_pull()
        self.stop_playback()
        self.notes.bind(None)
        forget_asst(s.dir)
        was_in_project = self._take_out_of_project(s)
        outcome, where = delete_session(s.dir)
        if outcome == "failed":
            if was_in_project and self._republisher is not None:
                self._republisher.publish(s.dir)  # it is still here
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
        was_in_project = self._take_out_of_project(s)
        outcome, _ = delete_session(s.dir, permanent=True)
        if outcome == "deleted":
            self.current = None
            self._on_deleted(s.dir)
        elif was_in_project and self._republisher is not None:
            self._republisher.publish(s.dir)

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
        self._changed(session_dir)
        if self.current is not None and self.current.dir == session_dir:
            self.reload()

    def flush_notes(self) -> None:
        self.notes.flush()


def _ticks(todos: list[Todo]) -> dict[str, bool]:
    """Which to-dos are done, by key."""
    return {t.key: t.done for t in todos}


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
