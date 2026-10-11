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

from gi.repository import Gtk, Pango

from ..cost import format_meter, prerecorded_cost, stream_cost
from ..notes import ensure_todos, open_count
from ..summarize import (
    SUMMARY_FILENAME,
    duration_minutes,
    iter_sessions,
    load_project,
    load_records,
    load_title,
    session_stamp,
)
from . import words
from .widgets import hint_label

LIVE = "live"
STATES = ("idle", "recording", "finalizing", "paused")


class Session:
    """A meeting on disk, read cheaply enough to list."""

    def __init__(self, path: Path) -> None:
        self.dir = path
        self.title = load_title(path)
        self.project = load_project(path)
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
                text = (self.dir / name).read_text()
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
                    parts.append((self.dir / name).read_text())
            self._haystack = "\n".join(parts).lower()
        if all(w in self._haystack for w in words):
            return {"where": "meeting", "line": -1}
        return None


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
        self.live_dir: Path | None = None
        self.live_app = ""
        self.selected: Item | None = None
        self._status = "idle"

        self.append(self._build_status())

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search meetings and transcripts")
        self.search.set_margin_start(8)
        self.search.set_margin_end(8)
        self.search.set_margin_bottom(6)
        self.search.connect("search-changed", self._on_search)
        self.append(self.search)

        self.list = Gtk.ListBox()
        self.list.add_css_class("navigation-sidebar")
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.set_header_func(self._header)
        self.list.connect("row-selected", self._on_row_selected)
        self.list.connect("row-activated", lambda *_a: on_activate())
        self.placeholder = Gtk.Label(label="", justify=Gtk.Justification.CENTER)
        self.placeholder.add_css_class("empty-hint")
        self.placeholder.set_wrap(True)
        self.placeholder.set_margin_top(24)
        self.placeholder.set_margin_start(16)
        self.placeholder.set_margin_end(16)
        self.list.set_placeholder(self.placeholder)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.list)
        scroll.set_vexpand(True)
        self.append(scroll)

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

    # -------------------------------------------------------------------- owed

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
            mine = [t for t in ensure_todos(s.dir) if not t.done and t.owner != "them"]
            if not mine:
                continue
            head = Gtk.Label(
                label=" · ".join(p for p in (s.title or "Untitled", words.when(s.start, now)) if p),
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

    def _selected_dir(self) -> Path | None:
        return self.selected.dir if isinstance(self.selected, Session) else None

    def _selected_key(self) -> Path | str | None:
        return LIVE if self.selected == LIVE else self._selected_dir()

    def _repopulate(self, keep: Path | str | None) -> None:
        while (row := self.list.get_first_child()) is not None:
            self.list.remove(row)

        shown = [
            s for s in self.sessions
            if s.dir != self.live_dir and s.matches(self.query)
        ]
        self.items = ([LIVE] if self.live_dir is not None else []) + shown
        now = datetime.now()
        for item in self.items:
            self.list.append(
                self._session_row(item, now) if isinstance(item, Session) else self._live_row()
            )

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
        if index < 0 and self.items and keep is None and not self.query:
            index = 0
        row = self.list.get_row_at_index(index) if index >= 0 else None
        if row is not None:
            self.list.select_row(row)
        elif not self.items and not self.query:
            self.selected = None
            self._on_select(None)
        # Otherwise the meeting on the page is merely filtered out of the
        # list. Forgetting it would blank the page header, and clearing the
        # search would then jump to the newest meeting instead of back here.

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
        """The meeting page changed this session's project or to-dos in place."""
        session.project = load_project(session.dir)
        session.open_todos = open_count(session.dir)
        session._haystack = None  # search includes project
        self._update_footer()
        index = next((i for i, it in enumerate(self.items) if it is session), -1)
        row = self.list.get_row_at_index(index) if index >= 0 else None
        if row is not None:
            row.set_child(self._session_row(session, datetime.now()).get_child())

    def _header(self, row: Gtk.ListBoxRow, before: Gtk.ListBoxRow | None) -> None:
        now = datetime.now()
        name = self._section_of(row.get_index(), now)
        if before is not None and self._section_of(before.get_index(), now) == name:
            row.set_header(None)
            return
        label = Gtk.Label(label=name, xalign=0.0)
        label.add_css_class("list-section")
        row.set_header(label)

    def _section_of(self, index: int, now: datetime) -> str:
        if not 0 <= index < len(self.items):
            return ""
        item = self.items[index]
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
        return row

    def _on_search(self, entry) -> None:
        self.query = entry.get_text().strip()
        self._repopulate(self._selected_key())

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

    def select(self, key: Path | str | None) -> None:
        self._repopulate(key)

    def forget(self, session_dir: Path) -> None:
        """Drop a deleted meeting and move to its neighbour."""
        index = next((i for i, it in enumerate(self.items)
                      if isinstance(it, Session) and it.dir == session_dir), -1)
        self.sessions = [s for s in self.sessions if s.dir != session_dir]
        self.selected = None
        remaining = [it for it in self.items if not (isinstance(it, Session) and it.dir == session_dir)]
        neighbour = remaining[min(index, len(remaining) - 1)] if remaining and index >= 0 else None
        key = LIVE if neighbour == LIVE else (neighbour.dir if isinstance(neighbour, Session) else None)
        self._repopulate(key)
