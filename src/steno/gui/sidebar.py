"""The sidebar: what Steno is doing right now, and every meeting it has kept.

A meeting in progress is not a separate screen. It is the newest meeting, at
the top of the list under "Now", and it opens by itself when it starts — so
there is one place to look, whether a call is happening or not.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")

import contextlib

from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from .. import citations, projectnotes
from ..cost import format_meter, prerecorded_cost, stream_cost
from ..merge import previous_session
from ..notes import ensure_todos, open_count
from ..summarize import (
    SUMMARY_FILENAME,
    duration_minutes,
    is_archived,
    iter_sessions,
    load_project,
    load_records,
    load_title,
    session_stamp,
    stamp_with_archived,
)
from . import words
from .menu import meeting_menu_model
from .widgets import hint_label

LIVE = "live"
# The last row of the list, which shows or hides the archived meetings.
ARCHIVE = "archive"
STATES = ("idle", "recording", "finalizing", "paused")
# Opening the archive scrolls to it once the frame clock has laid the
# new rows out: asked every SCROLL_POLL_MS, dropped after
# SCROLL_GIVE_UP_US.
SCROLL_POLL_MS = 16
SCROLL_GIVE_UP_US = 2 * GLib.USEC_PER_SEC


class Session:
    """A meeting on disk, read cheaply enough to list."""

    def __init__(self, path: Path) -> None:
        self.dir = path
        self.title = load_title(path)
        self.project = load_project(path)
        self.archived = is_archived(path)
        self.has_notes = projectnotes.read_record(path) is not None
        try:
            self.start = datetime.strptime(path.name, "%Y%m%d-%H%M%S")
        except ValueError:
            self.start = datetime.fromtimestamp(path.stat().st_mtime)
        self.open_todos = open_count(path)
        self.minutes = duration_minutes(load_records(path))
        self.cost = _session_cost(path, self.minutes)
        self.stamp = session_stamp(path)
        self._haystack: str | None = None

    @property
    def summary_path(self) -> Path:
        return self.dir / SUMMARY_FILENAME

    def matches(self, query: str) -> bool:
        """Substring search over everything the meeting produced.

        Reading the transcripts is fine at this scale and keeps the archive a
        directory of files rather than a database. If this ever gets slow, that
        is the moment to add an index — not before.
        """
        return self.locate(query) is not None

    def locate(self, query: str) -> dict | None:
        """Where a query hit, or None. `{"where": label, "line": n}`.

        `where` is the surface the reader cares about — summary, notes, or the
        transcript — and `line` is a 0-based transcript line so the meeting
        page can scroll to the sentence rather than just opening the meeting
        and leaving the reader to hunt for it.
        """
        if not query:
            return {"where": "", "line": 0}
        words = query.lower().split()

        def hit(text: str) -> bool:
            low = text.lower()
            return all(w in low for w in words)

        if hit(self.title or "") or hit(self.project or "") or hit(self.dir.name):
            return {"where": "title", "line": -1}
        for name, label in ((SUMMARY_FILENAME, "summary"), ("notes.md", "notes")):
            try:
                text = _read_searchable(self.dir / name)
            except OSError:
                continue
            if hit(text):
                return {"where": label, "line": -1}
        try:
            lines = (self.dir / "transcript.txt").read_text().splitlines()
        except OSError:
            # A meeting that was quit before it was finished has only the
            # far side's jsonl; it must still be findable.
            lines = [str(r.get("text", "")) for r in load_records(self.dir)]
        for i, line in enumerate(lines):
            if hit(line):
                return {"where": "transcript", "line": i}
        # A last resort: the jsonl is the same words as the txt, but a summary
        # written before transcript.txt existed may be all there is.
        if self._haystack is None:
            parts = [self.title or "", self.project or "", self.dir.name]
            for name in (SUMMARY_FILENAME, "notes.md", "transcript.txt"):
                with contextlib.suppress(OSError):
                    parts.append(_read_searchable(self.dir / name))
            self._haystack = "\n".join(parts).lower()
        if all(w in self._haystack for w in words):
            return {"where": "meeting", "line": -1}
        return None


def _read_searchable(path: Path) -> str:
    """A meeting file as search should see it: a citation marker is not a
    word, and "cite" must not find every cited summary."""
    text = path.read_text()
    return citations.strip(text) if path.name == SUMMARY_FILENAME else text


def _session_cost(path: Path, minutes: float) -> float:
    """A meeting's stamped estimate, or one derived from its length.

    Older sessions predate `cost_usd`; they still have to count toward the
    meter or the total lies the moment the archive is not brand new.
    """
    try:
        stamped = json.loads((path / "meta.json").read_text()).get("cost_usd")
        if stamped is not None:
            return float(stamped)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return stream_cost(minutes) + prerecorded_cost(minutes)


Item = Session | str


def row_menu(item: Item | None) -> Gio.Menu | None:
    """The menu a row offers, or None when it offers none.

    A past meeting gets what the page header's menu has. The meeting in
    progress gets nothing: it is stopped from the status card, and nothing
    in that menu should be done to a recording that is still being written.
    The row that shows the archive is not a meeting and gets nothing.
    """
    if not isinstance(item, Session):
        return None
    return meeting_menu_model(
        archived=item.archived,
        has_notes=item.has_notes,
        has_previous=previous_session(item.dir) is not None,
    )


class Sidebar(Gtk.Box):
    def __init__(
        self,
        root: Path,
        on_select: Callable[[Item | None], None],
        on_status_button: Callable[[str], None],
        on_activate: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.root = root
        self._on_select = on_select
        self._on_status_button = on_status_button
        self.sessions: list[Session] = []
        self.items: list[Item] = []
        self.query = ""
        # Whether the archived meetings are in the list. Not kept: a
        # window that opens on its archive every time has no archive.
        self.show_archived = False
        self.live_dir: Path | None = None
        self.live_app = ""
        self.selected: Item | None = None
        self._status = "idle"
        self._menu: Gtk.PopoverMenu | None = None

        self.append(self._build_status())
        self.search = self._build_search()
        self.append(self.search)

        self.list = Gtk.ListBox()
        self.list.add_css_class("navigation-sidebar")
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.set_header_func(self._header)
        self.list.connect("row-selected", self._on_row_selected)
        self.list.connect("row-activated", self._on_row_activated)
        self.list.add_controller(self._menu_shortcuts())
        self.placeholder = Gtk.Label(label="", justify=Gtk.Justification.CENTER)
        self.placeholder.add_css_class("empty-hint")
        self.placeholder.set_wrap(True)
        self.placeholder.set_margin_top(24)
        self.placeholder.set_margin_start(16)
        self.placeholder.set_margin_end(16)
        self.list.set_placeholder(self.placeholder)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_child(self.list)
        self.scroll.set_vexpand(True)
        self.append(self.scroll)

        self._on_activate = on_activate
        self.append(self._build_owed())

        self.footer = hint_label()
        # Four facts do not fit a 260px column on one line; wrap, don't clip.
        self.footer.set_wrap(True)
        self.footer.set_margin_end(16)
        self.footer.set_margin_start(16)
        self.footer.set_margin_top(8)
        self.footer.set_margin_bottom(10)
        self.append(self.footer)

    def _menu_shortcuts(self) -> Gtk.ShortcutController:
        keys = Gtk.ShortcutController()
        for key in ("Menu", "<Shift>F10"):
            keys.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(key),
                Gtk.CallbackAction.new(self._on_menu_key),
            ))
        return keys

    # -------------------------------------------------------------------- owed

    def _build_search(self) -> Gtk.SearchEntry:
        """The box above the list, with the keys it owes the reader.

        Esc, Enter and the arrows belong to it: Gtk.SearchEntry takes
        Esc as `stop-search` and never hands it on (the same annoyance
        as in asst's search), and the list must answer the rest.
        """
        search = Gtk.SearchEntry()
        search.set_placeholder_text("Search meetings and transcripts")
        search.set_margin_start(8)
        search.set_margin_end(8)
        search.set_margin_bottom(6)
        search.connect("search-changed", self._on_search)
        search.connect("stop-search", self._on_stop_search)
        search.connect("activate", self._on_search_activate)
        search.connect("next-match", lambda _e: self._walk_hits(1))
        search.connect("previous-match", lambda _e: self._walk_hits(-1))
        # Arrows walk the hits while typing, so the box keeps the keys.
        # Capture: the text inside binds Up and Down (to the line ends)
        # and would take them before a bubbling controller saw them.
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_search_key)
        search.add_controller(keys)
        return search

    def _build_owed(self) -> Gtk.Widget:
        """What you still owe, across meetings, one flat list.

        Afterwards you come back cold wanting "what did I promise?", and the
        answer should not need a terminal or a meeting-by-meeting hunt. It is
        a list of sentences, not folders or tags: a row opens its meeting.
        """
        self.owed = Gtk.MenuButton()
        self.owed.add_css_class("flat")
        self.owed.set_halign(Gtk.Align.START)
        self.owed.set_margin_start(8)
        self.owed.set_margin_top(4)
        self.owed.set_visible(False)

        self._owed_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._owed_box.set_margin_top(8)
        self._owed_box.set_margin_bottom(8)
        self._owed_box.set_margin_start(8)
        self._owed_box.set_margin_end(8)
        # A popover otherwise takes the width of its narrowest line.
        self._owed_box.set_size_request(340, -1)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_height(True)
        scroll.set_max_content_height(440)
        scroll.set_min_content_width(340)
        scroll.set_child(self._owed_box)
        self._owed_popover = Gtk.Popover()
        self._owed_popover.set_child(scroll)
        self._owed_popover.connect("show", lambda *_a: self._fill_owed())
        self.owed.set_popover(self._owed_popover)
        return self.owed

    def _fill_owed(self) -> None:
        while (child := self._owed_box.get_first_child()) is not None:
            self._owed_box.remove(child)
        now = datetime.now()
        for s in self.sessions:
            # A meeting put away has no open to-dos: they were closed.
            if s.archived:
                continue
            mine = [
                t for t in ensure_todos(s.dir)
                if t.open and t.owner != "them"
            ]
            if not mine:
                continue
            head = Gtk.Label(
                label=" · ".join(p for p in (
                    s.title or "Untitled",
                    words.when(s.start, now),
                ) if p),
                xalign=0.0,
            )
            head.add_css_class("sec")
            head.set_ellipsize(Pango.EllipsizeMode.END)
            head.set_margin_top(0 if self._owed_box.get_first_child() is None else 10)
            self._owed_box.append(head)
            for todo in mine:
                button = Gtk.Button()
                button.add_css_class("flat")
                button.add_css_class("owed-item")
                text = Gtk.Label(label=todo.text, xalign=0.0)
                text.set_wrap(True)
                text.set_max_width_chars(40)
                button.set_child(text)
                button.connect("clicked", lambda _b, d=s.dir: self._open_owed(d))
                self._owed_box.append(button)

    def _open_owed(self, session_dir: Path) -> None:
        self._owed_popover.popdown()
        if self.search.get_text():
            self.search.set_text("")
        self.select(session_dir)
        self._on_activate()

    # ------------------------------------------------------------------ status

    def _build_status(self) -> Gtk.Widget:
        self.card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.card.add_css_class("status-card")
        self.card.add_css_class("state-idle")
        self.card.set_margin_start(8)
        self.card.set_margin_end(8)
        self.card.set_margin_bottom(8)

        self.dot = Gtk.Label(label="●")
        self.dot.add_css_class("state-dot")
        self.card.append(self.dot)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.set_hexpand(True)
        text.set_valign(Gtk.Align.CENTER)
        self.status_title = Gtk.Label(label="Listening", xalign=0.0)
        self.status_title.add_css_class("status-title")
        self.status_sub = hint_label()
        self.status_sub.set_wrap(True)
        text.append(self.status_title)
        text.append(self.status_sub)
        self.card.append(text)

        self.status_button = Gtk.Button(label="Record")
        self.status_button.set_valign(Gtk.Align.CENTER)
        self.status_button.connect(
            "clicked", lambda _b: self._on_status_button(self.status_button.get_label() or "")
        )
        self.card.append(self.status_button)
        return self.card

    def set_status(self, status: str, title: str, subtitle: str, button: str) -> None:
        for name in STATES:
            self.card.remove_css_class(f"state-{name}")
        self.card.add_css_class(f"state-{status}")
        self._status = status
        self.status_title.set_text(title)
        self.status_sub.set_text(subtitle)
        self.status_sub.set_tooltip_text(subtitle)
        self.status_button.set_visible(bool(button))
        if button:
            self.status_button.set_label(button)
        self.status_button.remove_css_class("destructive-action")
        if button == "Stop":
            self.status_button.add_css_class("destructive-action")

    # -------------------------------------------------------------------- list

    def set_live(self, session_dir: Path | None, app: str = "") -> None:
        """Show or drop the meeting in progress at the top of the list."""
        self.live_dir = session_dir
        self.live_app = app
        keep = LIVE if session_dir is not None else self._selected_dir()
        if session_dir is None and self.selected == LIVE:
            keep = None
        self._repopulate(keep)

    def refresh(self, select: Path | str | None = None) -> None:
        """Re-read the sessions directory, keeping the selection where it can."""
        keep = select if select is not None else self._selected_key()
        # A meeting that is still there keeps its Session object: the meeting
        # page holds one, and a fresh copy would leave it editing an orphan
        # whose to-do ticks and project never reach the list.
        known = {s.dir: s for s in self.sessions}
        fresh = []
        for path in iter_sessions(self.root):
            session = Session(path)
            old = known.get(session.dir)
            if old is not None:
                old.__dict__.update(session.__dict__)
                session = old
            fresh.append(session)
        self.sessions = fresh
        if select is not None:
            self._reveal(select)
        self._repopulate(keep)

    def refresh_if_stale(self) -> bool:
        """Reload only when the files behind the list have actually changed.

        `steno realign` and `steno summarize` rewrite a meeting from a terminal
        while the window sits there, and a transcript replaced on disk but not
        on screen looks like the tool not having worked. Reloading every time
        the window is raised would throw away scroll positions, so check first.
        """
        current = [session_stamp(p) for p in iter_sessions(self.root)]
        if current == [s.stamp for s in self.sessions]:
            return False
        self.refresh()
        return True

    def archived_count(self) -> int:
        """How many meetings are put away, shown in the list or not."""
        return sum(1 for s in self.sessions if s.archived)

    def _selected_dir(self) -> Path | None:
        return self.selected.dir if isinstance(self.selected, Session) else None

    def _selected_key(self) -> Path | str | None:
        return LIVE if self.selected == LIVE else self._selected_dir()

    def _repopulate(self, keep: Path | str | None) -> None:
        # The menu is parented to a row that is about to go.
        self._drop_menu()
        while (row := self.list.get_first_child()) is not None:
            self.list.remove(row)

        found = [
            s for s in self.sessions
            if s.dir != self.live_dir and s.matches(self.query)
        ]
        away = [s for s in found if s.archived]
        self.items = (
            ([LIVE] if self.live_dir is not None else [])
            + [s for s in found if not s.archived]
            + ([ARCHIVE] if away else [])
            + (away if self.show_archived else [])
        )
        now = datetime.now()
        for item in self.items:
            if isinstance(item, Session):
                row = self._session_row(item, now)
                self._add_menu_triggers(row)
            elif item == ARCHIVE:
                row = self._archive_row(len(away))
            else:
                row = self._live_row()
            self.list.append(row)

        self.placeholder.set_text(
            "No meeting matches that." if self.query
            else "No meetings yet.\nStart a call in any app that uses the mic."
        )
        self._update_footer()

        index = -1
        for i, item in enumerate(self.items):
            if (item == LIVE and keep == LIVE) or (
                isinstance(item, Session) and isinstance(keep, Path) and item.dir == keep
            ):
                index = i
                break
        if index < 0 and keep is None and not self.query:
            index = next(
                (i for i, it in enumerate(self.items) if it != ARCHIVE), -1
            )
        if index < 0 and self.query:
            # The meeting on the page is not among the hits: the page
            # moves to the first hit, so the list and the page agree
            # (issue #20).
            index = next(
                (i for i, it in enumerate(self.items)
                 if isinstance(it, Session)), -1
            )
        row = self.list.get_row_at_index(index) if index >= 0 else None
        if row is not None:
            self.list.select_row(row)
        elif not self.query and not self._holds_meeting(keep) and all(
            it == ARCHIVE for it in self.items
        ):
            self.selected = None
            self._on_select(None)
        # Otherwise the meeting on the page is merely filtered out of the
        # list (by the search, or by being archived). Forgetting it
        # would blank the page header, and clearing the search would
        # then jump to the newest meeting instead of back here.

    def _holds_meeting(self, key: Path | str | None) -> bool:
        """Is this a meeting that exists, shown in the list or not?"""
        return isinstance(key, Path) and any(
            s.dir == key for s in self.sessions
        )

    def _update_footer(self) -> None:
        total = len(self.sessions)
        open_todos = sum(s.open_todos for s in self.sessions)
        minutes = sum(s.minutes for s in self.sessions)
        dollars = sum(s.cost for s in self.sessions)
        parts = [f"{total} meeting{'' if total == 1 else 's'}"]
        if minutes:
            parts.append(format_meter(minutes, dollars))
        self.footer.set_text(" · ".join(parts))
        self.owed.set_visible(bool(open_todos))
        self.owed.set_label(words.owed_label(open_todos))

    def update_session(self, session: Session) -> None:
        """The meeting page changed this session's project, to-dos,
        archive flag or project notes in place."""
        was_archived = session.archived
        session.archived = is_archived(session.dir)
        session.has_notes = projectnotes.read_record(session.dir) is not None
        session.project = load_project(session.dir)
        session.open_todos = open_count(session.dir)
        session._haystack = None  # search includes project
        # The flag is part of the stamp, and was just read again.
        session.stamp = stamp_with_archived(session.stamp, session.archived)
        if session.archived != was_archived:
            self._move_between(session)
            return
        self._update_footer()
        index = next((i for i, it in enumerate(self.items) if it is session), -1)
        row = self.list.get_row_at_index(index) if index >= 0 else None
        if row is not None:
            row.set_child(self._session_row(session, datetime.now()).get_child())

    def _header(self, row: Gtk.ListBoxRow, before: Gtk.ListBoxRow | None) -> None:
        now = datetime.now()
        name = self._section_of(row.get_index(), now)
        # The row that opens the archive is its heading already.
        if self._item_at(row.get_index()) == ARCHIVE or (
            before is not None
            and self._section_of(before.get_index(), now) == name
        ):
            row.set_header(None)
            return
        label = Gtk.Label(label=name, xalign=0.0)
        label.add_css_class("list-section")
        row.set_header(label)

    def _item_at(self, index: int) -> Item | None:
        return self.items[index] if 0 <= index < len(self.items) else None

    def _section_of(self, index: int, now: datetime) -> str:
        item = self._item_at(index)
        if item is None:
            return ""
        if item == ARCHIVE or (isinstance(item, Session) and item.archived):
            return "Archived"
        return words.list_section(item.start, now) if isinstance(item, Session) else "Now"

    def _live_row(self) -> Gtk.ListBoxRow:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        title = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        dot = Gtk.Label(label="●")
        dot.add_css_class("state-dot")
        title_box = Gtk.Box()
        title_box.add_css_class("state-recording")
        title_box.append(dot)
        title.append(title_box)
        label = Gtk.Label(label="Meeting in progress", xalign=0.0)
        label.add_css_class("session-title")
        title.append(label)
        box.append(title)
        meta = Gtk.Label(label=self.live_app or "Recording", xalign=0.0)
        meta.add_css_class("session-meta")
        box.append(meta)
        row = Gtk.ListBoxRow()
        row.set_child(box)
        return row

    def _archive_row(self, count: int) -> Gtk.ListBoxRow:
        """Shows or hides the archived meetings. Not a meeting: it cannot
        be selected, so the page never mistakes it for one."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        label = Gtk.Label(label=f"Archived ({count})", xalign=0.0)
        label.add_css_class("sec")
        label.set_hexpand(True)
        box.append(label)
        open_ = "pan-down-symbolic"
        shut = "pan-end-symbolic"
        box.append(Gtk.Image.new_from_icon_name(
            open_ if self.show_archived else shut
        ))
        row = Gtk.ListBoxRow()
        row.add_css_class("archive-toggle")
        row.set_selectable(False)
        row.set_child(box)
        row.set_tooltip_text(
            "Meetings you put away. Their open to-dos still count."
        )
        row.update_state(
            [Gtk.AccessibleState.EXPANDED], [self.show_archived]
        )
        return row

    def _session_row(self, s: Session, now: datetime) -> Gtk.ListBoxRow:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)

        title = Gtk.Label(label=s.title or "Untitled", xalign=0.0)
        title.add_css_class("session-title")
        if not s.title:
            title.add_css_class("session-untitled")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(title)

        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        parts = words.row_meta(
            s.start, now, s.minutes, s.project, summarized=s.summary_path.is_file()
        )
        if self.query:
            hit = s.locate(self.query)
            if hit and hit["where"] in ("summary", "notes", "transcript"):
                parts.append(f"in {hit['where']}")
        meta = Gtk.Label(label="  ·  ".join(parts), xalign=0.0)
        meta.add_css_class("session-meta")
        meta.set_hexpand(True)
        meta.set_ellipsize(Pango.EllipsizeMode.END)
        line.append(meta)
        if s.open_todos:
            badge = Gtk.Label(label=words.todo_count(s.open_todos), xalign=1.0)
            badge.add_css_class("count")
            line.append(badge)
        box.append(line)

        row = Gtk.ListBoxRow()
        row.set_child(box)
        if s.archived:
            row.add_css_class("session-archived")
        return row

    def _on_search(self, entry) -> None:
        self.query = entry.get_text().strip()
        # A new query is a new question: its first hit is what it means,
        # so the page starts there rather than at whatever was open
        # (issue #20). Clearing it keeps the meeting being read.
        self._repopulate(None if self.query else self._selected_key())

    def _on_stop_search(self, entry) -> None:
        """Esc in the search box: clear it, or with nothing left to
        clear, hand the keyboard to the selected row (issue #20). A
        SearchEntry takes Esc as `stop-search` and never lets it go."""
        if entry.get_text():
            entry.set_text("")
            self._on_search(entry)
            return
        row = self.list.get_selected_row()
        if row is not None:
            row.grab_focus()

    def _on_search_activate(self, _entry) -> None:
        """Enter opens the hit being looked at — the first one, right
        after typing — and hands the keyboard to the meeting page, so
        Esc and Enter reach it from there (issue #20)."""
        row = self.list.get_selected_row()
        if row is None or not isinstance(self._item_at(row.get_index()), Session):
            row = self._first_hit_row()
            if row is None:
                return
            self.list.select_row(row)
        self._on_activate()

    def _on_search_key(
        self, _keys: Gtk.EventControllerKey, key: int, _code: int, _state
    ) -> bool:
        """Arrows walk the hits while the box keeps the keys (issue #20)."""
        step = {Gdk.KEY_Down: 1, Gdk.KEY_Up: -1}.get(key)
        if step is None:
            return False
        self._walk_hits(step)
        return True

    def _first_hit_row(self) -> Gtk.ListBoxRow | None:
        """The row of the first meeting the query matched, if any."""
        for i, item in enumerate(self.items):
            if isinstance(item, Session):
                return self.list.get_row_at_index(i)
        return None

    def _walk_hits(self, step: int) -> None:
        """Move the selection through the hits; the page follows it."""
        rows = [
            self.list.get_row_at_index(i)
            for i, item in enumerate(self.items)
            if isinstance(item, Session)
        ]
        if not rows:
            return
        current = self.list.get_selected_row()
        try:
            at = rows.index(current) + step
        except ValueError:
            at = 0 if step > 0 else len(rows) - 1
        row = rows[max(0, min(at, len(rows) - 1))]
        self.list.select_row(row)
        give_up = GLib.get_monotonic_time() + SCROLL_GIVE_UP_US
        GLib.timeout_add(SCROLL_POLL_MS, self._scroll_to, row, give_up)

    def _on_row_activated(self, _list, row: Gtk.ListBoxRow) -> None:
        if self._item_at(row.get_index()) == ARCHIVE:
            self.toggle_archived()
        else:
            self._on_activate()

    def _on_row_selected(self, _list, row) -> None:
        if row is None:
            return
        index = row.get_index()
        if not 0 <= index < len(self.items):
            return
        item = self.items[index]
        same = item == self.selected or (
            isinstance(item, Session) and isinstance(self.selected, Session)
            and item.dir == self.selected.dir
        )
        self.selected = item
        # A rebuilt list re-selects the same meeting; that is not a new choice,
        # and reloading on it would stop playback every time a file changed.
        if not same:
            self._on_select(item)

    # -------------------------------------------------------------------- menu

    def _add_menu_triggers(self, row: Gtk.ListBoxRow) -> None:
        """Right-click, or a long press on a touchscreen, asks for the menu."""
        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect("pressed", lambda g, _n, x, y: self._on_row_menu(g, x, y))
        row.add_controller(click)
        hold = Gtk.GestureLongPress(touch_only=True)
        hold.connect("pressed", self._on_row_menu)
        row.add_controller(hold)

    def _on_row_menu(self, gesture, x: float, y: float) -> None:
        # Claimed, so the list cannot also take a touch long press for a
        # click and open the meeting's page over the menu.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        row = gesture.get_widget()
        if isinstance(row, Gtk.ListBoxRow):
            self.popup_menu(row, x, y)

    def _on_menu_key(self, *_args) -> bool:
        """Menu or Shift+F10 on the row with the keyboard focus."""
        row = self.list.get_focus_child() or self.list.get_selected_row()
        return isinstance(row, Gtk.ListBoxRow) and self.popup_menu(row)

    def popup_menu(
        self, row: Gtk.ListBoxRow, x: float | None = None, y: float = 0
    ) -> bool:
        """Open a row's menu at the pointer, or under the row without one.

        The menu's actions work on the meeting that is open, so the row is
        opened first: never the meeting that happened to be open before.
        """
        index = row.get_index()
        item = self.items[index] if 0 <= index < len(self.items) else None
        menu = row_menu(item)
        if menu is None:
            return False
        self.list.select_row(row)
        self._drop_menu()
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(row)
        popover.set_has_arrow(False)
        popover.set_halign(Gtk.Align.START)
        if x is not None:
            area = Gdk.Rectangle()
            area.x, area.y, area.width, area.height = int(x), int(y), 1, 1
            popover.set_pointing_to(area)
        popover.connect("closed", self._on_menu_closed)
        self._menu = popover
        popover.popup()
        return True

    def _on_menu_closed(self, popover: Gtk.PopoverMenu) -> None:
        # Not here: the click that closed it is still delivering its action.
        GLib.idle_add(self._drop_menu, popover)

    def _drop_menu(self, popover: Gtk.PopoverMenu | None = None) -> bool:
        popover = popover or self._menu
        if popover is self._menu:
            self._menu = None
        if popover is not None and popover.get_parent() is not None:
            popover.unparent()
        return GLib.SOURCE_REMOVE

    def select(self, key: Path | str | None) -> None:
        self._reveal(key)
        self._repopulate(key)

    def _reveal(self, key: Path | str | None) -> None:
        """Asked for by name, an archived meeting opens the archive: the
        to-do list and a notification's Open lead here."""
        if isinstance(key, Path) and any(
            s.dir == key and s.archived for s in self.sessions
        ):
            self.show_archived = True

    def toggle_archived(self) -> None:
        """Show the archived meetings, or hide them again."""
        self.show_archived = not self.show_archived
        self._repopulate(self._selected_key())
        row = self._archive_row_widget()
        if row is not None:
            row.grab_focus()
            if self.show_archived:
                give_up = GLib.get_monotonic_time() + SCROLL_GIVE_UP_US
                GLib.timeout_add(
                    SCROLL_POLL_MS, self._scroll_to, row, give_up
                )

    def _archive_row_widget(self) -> Gtk.ListBoxRow | None:
        index = next(
            (i for i, it in enumerate(self.items) if it == ARCHIVE), -1
        )
        return self.list.get_row_at_index(index) if index >= 0 else None

    def _scroll_to(self, row: Gtk.ListBoxRow, give_up: int) -> bool:
        """Put the row at the top, so that what opened under it is seen.

        The rows were only just made, and the frame clock lays them out
        when it next runs: until then the list has no extent to scroll
        in. So ask until it has one; True means ask again.
        """
        if row.get_parent() is None:  # the list was rebuilt meanwhile
            return GLib.SOURCE_REMOVE
        point = row.translate_coordinates(self.list, 0, 0)
        if point is None or not row.get_height():
            return GLib.get_monotonic_time() < give_up
        adj = self.scroll.get_vadjustment()
        top = max(0.0, min(point[1], adj.get_upper() - adj.get_page_size()))
        adj.set_value(top)
        return GLib.SOURCE_REMOVE

    def forget(self, session_dir: Path) -> None:
        """Drop a deleted meeting and move to its neighbour."""
        self.sessions = [s for s in self.sessions if s.dir != session_dir]
        self._step_off(session_dir)

    def _move_between(self, session: Session) -> None:
        """A meeting was put away or brought back: its row changes
        section.

        Put away with the archive closed, it leaves the list and the
        page goes to its neighbour. Otherwise it is still in the list,
        so the page stays where it is: a meeting just brought back is
        the one being looked at, and so is one put away in the open
        archive.
        """
        if session.archived and not self.show_archived:
            self._step_off(session.dir)
        else:
            self._repopulate(session.dir)

    def _step_off(self, session_dir: Path) -> None:
        """Move the page to the meeting next to one that has left the
        list, or to the empty page when there is none."""
        meetings = [it for it in self.items if it != ARCHIVE]

        def is_it(item: Item) -> bool:
            return isinstance(item, Session) and item.dir == session_dir

        index = next((i for i, it in enumerate(meetings) if is_it(it)), -1)
        self.selected = None
        remaining = [it for it in meetings if not is_it(it)]
        neighbour = remaining[min(index, len(remaining) - 1)] if remaining and index >= 0 else None
        key = LIVE if neighbour == LIVE else (neighbour.dir if isinstance(neighbour, Session) else None)
        self._repopulate(key)
        if self.selected is None:
            # Nothing to move to (the search left no neighbour): the page
            # must not keep showing, and editing, a meeting that is in
            # the trash or has been put away.
            self._on_select(None)
