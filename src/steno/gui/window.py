"""The window: a list of meetings, and the meeting you picked.

Nothing here decides anything about a meeting — it routes engine events into
widgets and user gestures back into the engine, which the *app* owns. The window
is closable, disposable, and often absent; closing it stops nothing.

It is one `Adw.NavigationSplitView` whose shape follows the window's: a sidebar
and a two-column meeting page when wide, the same page stacked into one column
when narrower, and the list and the meeting as two pages you move between when
the window is narrow — the tall one parked beside a call.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from .. import data_dir
from ..events import (
    AdviceDelta,
    AdviceDone,
    EngineError,
    Event,
    Heard,
    MeetingEnded,
    MeetingStarted,
    SummaryReady,
)
from ..session import paused_until
from ..state import PAUSE_CHOICES
from ..summarize import load_records
from . import words
from .live import LivePage
from .meeting import MeetingPage
from .sidebar import LIVE, Session, Sidebar
from .style import palette, stylesheet
from .widgets import AskBox

if TYPE_CHECKING:  # the app imports the window lazily; this keeps the cycle unreal
    from .app import App

# Below this the meeting stacks into one column; below NARROW the sidebar and
# the meeting become separate pages.
MEDIUM = "max-width: 1000sp"
NARROW = "max-width: 600sp"

SHORTCUTS = (
    ("Ctrl+F", "Search meetings"),
    ("Ctrl+G", "Ask about this meeting"),
    ("Ctrl+R", "Start or stop recording"),
    ("Ctrl+Space", "Play or pause a past meeting"),
    ("Ctrl+ + / −", "Live transcript size"),
    ("Ctrl+W", "Close the window (keeps listening)"),
    ("Ctrl+Q", "Quit (stops listening)"),
)


class Window(Adw.ApplicationWindow):
    def __init__(self, app: App, root: Path | None = None):
        super().__init__(application=app, title="Steno")
        self.app = app
        self.add_css_class("steno")
        self.set_default_size(1100, 720)
        self.set_size_request(360, 420)
        self.root = root or (data_dir() / "sessions")
        self._asker: AskBox | None = None
        self._live_started = 0.0
        self._live_app = ""

        self.live = LivePage(on_ask=self._ask_live)
        self._css = Gtk.CssProvider()
        self._apply_css()

        self.meeting = MeetingPage(
            on_ask=self._ask_past,
            on_realign=self._realign,
            on_deleted=lambda d: self.sidebar.forget(d),
            on_todos_changed=lambda s: self.sidebar.update_todo_counts(s),
            on_toast=self._toast,
        )
        self.insert_action_group("meeting", self.meeting.actions)

        self.sidebar = Sidebar(
            self.root,
            on_select=self._on_select,
            on_status_button=self._on_status_button,
            on_activate=lambda: self.split.set_show_content(True),
        )

        self.split = Adw.NavigationSplitView()
        self.split.set_min_sidebar_width(260)
        self.split.set_max_sidebar_width(300)
        self.split.set_sidebar(self._build_sidebar_page())
        self.split.set_content(self._build_content_page())
        self.split.connect("notify::collapsed", lambda *_a: self._update_header())

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        medium = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(MEDIUM))
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(NARROW))
        narrow.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(medium)
        self.add_breakpoint(narrow)
        self.connect("notify::current-breakpoint", lambda *_a: self._on_breakpoint())

        self._install_actions()
        self.connect("close-request", self._on_close)
        GLib.timeout_add_seconds(1, self._tick)
        self.sidebar.refresh()
        self._adopt_running_meeting()
        self.set_state(app.status, app.detail)

    # ----------------------------------------------------------------- building

    def _build_sidebar_page(self) -> Adw.NavigationPage:
        header = Adw.HeaderBar()
        menu = Gio.Menu()
        pause = Gio.Menu()
        for minutes, label in PAUSE_CHOICES:
            pause.append_item(Gio.MenuItem.new(label, f"app.pause({minutes})"))
        pause.append("Resume listening", "app.unpause")
        menu.append_section("Pause listening", pause)
        rest = Gio.Menu()
        rest.append("Start at login", "app.autostart")
        rest.append("Keyboard shortcuts", "win.shortcuts")
        rest.append("Quit, stop listening", "app.quit")
        menu.append_section(None, rest)
        button = Gtk.MenuButton()
        button.set_icon_name("open-menu-symbolic")
        button.set_tooltip_text("Main menu")
        button.set_menu_model(menu)
        header.pack_end(button)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self.sidebar)
        return Adw.NavigationPage.new(view, "Steno")

    def _build_content_page(self) -> Adw.NavigationPage:
        header = Adw.HeaderBar()
        header.set_show_title(False)

        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        titles.set_valign(Gtk.Align.CENTER)
        titles.set_margin_start(8)
        self.page_title = Gtk.Label(label="", xalign=0.0)
        self.page_title.add_css_class("page-title")
        self.page_title.set_ellipsize(Pango.EllipsizeMode.END)
        self.page_subtitle = Gtk.Label(label="", xalign=0.0)
        self.page_subtitle.add_css_class("page-subtitle")
        self.page_subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        titles.append(self.page_title)
        titles.append(self.page_subtitle)
        header.pack_start(titles)

        menu = Gio.Menu()
        menu.append("Re-time against the audio…", "meeting.realign")
        danger = Gio.Menu()
        danger.append("Delete the audio, keep the text…", "meeting.delete-audio")
        danger.append("Delete this meeting…", "meeting.delete")
        menu.append_section(None, danger)
        self.meeting_menu = Gtk.MenuButton()
        self.meeting_menu.set_icon_name("view-more-symbolic")
        self.meeting_menu.set_tooltip_text("This meeting")
        self.meeting_menu.set_menu_model(menu)
        header.pack_end(self.meeting_menu)

        self.stop_button = Gtk.Button(label="Stop")
        self.stop_button.add_css_class("destructive-action")
        self.stop_button.connect("clicked", lambda _b: self.app.toggle_record())
        header.pack_end(self.stop_button)

        empty = Adw.StatusPage()
        empty.set_icon_name("audio-input-microphone-symbolic")
        empty.set_title("No meetings yet")
        empty.set_description(
            "Steno records by itself when an app uses the microphone. "
            "Start a call, or press Record."
        )

        self.stack = Gtk.Stack()
        self.stack.add_css_class("page")
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.meeting, "meeting")
        self.stack.add_named(self.live, "live")
        self.stack.add_named(empty, "empty")

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self.stack)
        self.content_page = Adw.NavigationPage.new(view, "Meeting")
        return self.content_page

    def _apply_css(self) -> None:
        self._css.load_from_string(stylesheet(palette(), self.live.transcript_pt))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, self._css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _install_actions(self) -> None:
        def add(name: str, fn, accels: list[str] | None = None):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p: fn())
            self.add_action(action)
            app = self.get_application()
            if accels and app is not None:
                app.set_accels_for_action(f"win.{name}", accels)

        add("shortcuts", self._show_shortcuts, ["<Control>question"])
        add("ask", self._focus_ask, ["<Control>g"])
        add("find", self._focus_search, ["<Control>f"])
        add("toggle-record", self.app.toggle_record, ["<Control>r"])
        add("play", self.meeting.toggle_play, ["<Control>space"])
        add("zoom-in", lambda: self._zoom(1), ["<Control>plus", "<Control>equal"])
        add("zoom-out", lambda: self._zoom(-1), ["<Control>minus"])
        # Close is not quit: it hides the window and the listener keeps going.
        add("close", self.close, ["<Control>w"])

    def _adopt_running_meeting(self) -> None:
        """Catch up if a meeting was already going when this window opened.

        Normal now that the app runs headless: the window is often opened
        halfway through, and a live transcript that starts from the moment you
        happened to look is nearly useless.
        """
        engine = self.app.engine
        if engine is None or engine.current is None:
            return
        current = engine.current
        self._begin_live(current.session_dir, current.app, current.started_at)
        for record in load_records(current.session_dir):
            self.live.add_line(record.get("speaker", "them"), record.get("text", ""))

    # ------------------------------------------------------------------- layout

    def _on_breakpoint(self) -> None:
        stacked = self.get_current_breakpoint() is not None
        self.meeting.set_stacked(stacked)
        self.live.set_stacked(stacked)
        self._update_header()

    def _on_select(self, item) -> None:
        if isinstance(item, Session):
            self.meeting.load(item)
            self.stack.set_visible_child_name("meeting")
        elif item == LIVE:
            self.stack.set_visible_child_name("live")
        else:
            self.stack.set_visible_child_name("empty")
        self._update_header()

    def _update_header(self) -> None:
        item = self.sidebar.selected
        collapsed = self.split.get_collapsed()
        if isinstance(item, Session):
            title = item.title or "Untitled"
            subtitle = words.page_subtitle(item.start, item.minutes, item.project)
        elif item == LIVE:
            title = "Meeting in progress"
            started = datetime.fromtimestamp(self._live_started)  # noqa: DTZ006
            parts = [f"Started {started:%H:%M}", self._live_app]
            if not collapsed:
                parts.append("the summary and to-dos arrive when it ends")
            subtitle = " · ".join(p for p in parts if p)
        else:
            title, subtitle = "", ""
        self.page_title.set_text(title)
        self.page_subtitle.set_text(subtitle)
        self.page_subtitle.set_visible(bool(subtitle))
        self.content_page.set_title(title or "Meeting")
        self.meeting_menu.set_visible(isinstance(item, Session))
        # With the sidebar showing, Stop is on its status card; without it, here.
        self.stop_button.set_visible(item == LIVE and collapsed)

    # ------------------------------------------------------------ engine events

    def on_event(self, event: Event) -> None:
        """Called by the app, on the GTK thread."""
        if isinstance(event, MeetingStarted):
            self.live.clear()
            self._begin_live(event.session_dir, event.app, event.started_at)

        elif isinstance(event, Heard):
            if event.final:
                self.live.add_line(event.speaker, event.text)
            else:
                self.live.set_interim(event.text)

        elif isinstance(event, MeetingEnded):
            self.live.flush_notes()
            was_watching = self.sidebar.selected == LIVE
            self.sidebar.live_dir = None
            self.sidebar.refresh(select=event.session_dir if was_watching else None)

        elif isinstance(event, SummaryReady):
            self.sidebar.refresh()
            selected = self.sidebar.selected
            if isinstance(selected, Session) and selected.dir == event.session_dir:
                self.meeting.load(selected)
                self._update_header()
            self._toast(f"Summary ready: {event.title}", open_session=event.session_dir)

        elif isinstance(event, AdviceDelta):
            if self._asker is not None:
                self._asker.delta(event.text)

        elif isinstance(event, AdviceDone):
            if self._asker is not None:
                self._asker.done(event.text)
            self._asker = None

        elif isinstance(event, EngineError):
            self._toast(f"{event.where}: {event.message}")

    def _begin_live(self, session_dir: Path, app: str, started_at: float) -> None:
        self._live_started = started_at
        self._live_app = app
        self.live.bind_session(session_dir)
        self.sidebar.set_live(session_dir, app)
        self.split.set_show_content(True)
        self._update_header()

    def set_state(self, status: str, detail: str = "") -> None:
        title, subtitle, button = words.status_card(
            status, detail, time.time(), self.app.since, paused_until()
        )
        self.sidebar.set_status(status, title, subtitle, button)

    def _tick(self) -> bool:
        """Let the status card count a recording up and a pause down."""
        if self.app.status in ("recording", "paused", "idle"):
            if self.app.status != "recording":
                status = "paused" if paused_until() else "idle"
                self.set_state(status)
            else:
                self.set_state(self.app.status, self.app.detail)
        return GLib.SOURCE_CONTINUE

    def refresh_if_stale(self) -> None:
        """Catch up with anything the CLI changed while the window was hidden."""
        if self.sidebar.refresh_if_stale():
            selected = self.sidebar.selected
            if isinstance(selected, Session):
                self.meeting.load(selected)
            self._update_header()

    def show_session(self, session_dir: Path | None) -> None:
        """Open one meeting — where a summary notification lands."""
        self.sidebar.refresh(select=session_dir)
        self.split.set_show_content(True)

    # ------------------------------------------------------------ user gestures

    def _on_status_button(self, label: str) -> None:
        if label == "Resume":
            self.app.pause(None)
        else:
            self.app.toggle_record()

    def _ask_live(self, question: str) -> None:
        self._submit_question(self.live.ask, question, None)

    def _ask_past(self, session_dir: Path, question: str) -> None:
        self._submit_question(self.meeting.ask, question, session_dir)

    def _submit_question(self, asker: AskBox, question: str, session_dir: Path | None) -> None:
        engine, bridge = self.app.engine, self.app.bridge
        if engine is None or bridge is None:
            asker.done("Steno isn't running, so there is nothing to ask.")
            return
        if self._asker is not None and self._asker is not asker:
            # One question at a time: the engine drops a second one, and a box
            # left saying "Thinking…" forever reads as a hang.
            asker.done("Still answering the question asked elsewhere; ask again in a moment.")
            return
        self._asker = asker
        bridge.submit(engine.ask(question, session_dir=session_dir))

    def _focus_ask(self) -> None:
        self.split.set_show_content(True)
        page = self.stack.get_visible_child_name()
        if page == "live":
            self.live.ask.focus()
        elif page == "meeting":
            self.meeting.ask.focus()

    def _focus_search(self) -> None:
        self.split.set_show_content(False)
        self.sidebar.search.grab_focus()

    def _realign(self, session_dir: Path, done) -> None:
        """Run a re-timing pass on the engine's loop, and report back on GTK's.

        It is one long network call per track, so it cannot run here: the main
        loop is also drawing a live meeting.
        """
        bridge = self.app.bridge
        if bridge is None:
            done(session_dir, 0, "engine is not running")
            return
        from ..realign import realign_session

        future = bridge.submit(realign_session(session_dir))
        if future is None:
            done(session_dir, 0, "engine is not running")
            return

        def finished(f) -> None:
            try:
                lines, error = f.result(), ""
            except Exception as e:  # noqa: BLE001 — report it, never take the window down
                lines, error = 0, str(e)
            GLib.idle_add(done, session_dir, lines, error)

        future.add_done_callback(finished)

    def _zoom(self, delta: int) -> None:
        self.live.zoom(delta)
        self._apply_css()

    def _toast(self, text: str, open_session: Path | None = None) -> None:
        # Never steal focus or animate during a meeting; a toast is the loudest
        # this window is allowed to be while someone is talking.
        print(f"[steno] {text}", flush=True)
        toast = Adw.Toast.new(text)
        toast.set_timeout(5)
        if open_session is not None:
            toast.set_button_label("Open")
            toast.set_action_name("app.open-session")
            toast.set_action_target_value(GLib.Variant("s", str(open_session)))
        self.toasts.add_toast(toast)

    def _show_shortcuts(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Keyboard shortcuts",
            body="\n".join(f"{keys}\t{what}" for keys, what in SHORTCUTS),
        )
        dialog.add_response("close", "Close")
        dialog.present(self)

    # ----------------------------------------------------------------- shutdown

    def _on_close(self, *_a) -> bool:
        """Closing the window is not quitting.

        The app is a listener first; a window that took the detector down with
        it would mean the next meeting went unrecorded because someone tidied
        their desktop. Save what's open, hide, and say so once.
        """
        self.live.flush_notes()
        self.meeting.flush_notes()
        self.meeting.stop_playback()
        self.set_visible(False)
        self.app.note_still_listening()
        return True

    def confirm_quit(self) -> None:
        """Asked for by the app when quitting mid-meeting."""
        self.present()
        dialog = Adw.AlertDialog(
            heading="A meeting is still recording",
            body=(
                "Quitting will end the recording, transcribe your side, and write "
                "the summary. That takes a few seconds.\n\n"
                "To put the window away without stopping anything, close it "
                "instead — Steno keeps listening."
            ),
        )
        dialog.add_response("cancel", "Keep recording")
        dialog.add_response("quit", "Stop and quit")
        dialog.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def answered(_d, response: str) -> None:
            if response != "quit":
                return
            self.set_state("finalizing", "Wrapping up")
            self.app.quit()

        dialog.connect("response", answered)
        dialog.present(self)
