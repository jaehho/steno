"""The window: two screens, one state indicator, and the wiring between them.

Nothing here decides anything about a meeting — it routes engine events into
widgets and user gestures back into the engine, which the *app* owns. The window
is closable, disposable, and often absent; closing it stops nothing.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

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
from ..summarize import load_records
from .archive import ArchiveView
from .live import LiveView
from .style import palette, stylesheet

if TYPE_CHECKING:  # the app imports the window lazily; this keeps the cycle unreal
    from .app import App

PAUSE_CHOICES = ((30, "30 minutes"), (60, "1 hour"), (240, "4 hours"))

STATE_WORDS = {
    "idle": "Listening for a meeting",
    "recording": "Recording",
    "finalizing": "Finishing up",
    "paused": "Paused",
}


class Window(Adw.ApplicationWindow):
    def __init__(self, app: App, root: Path | None = None):
        super().__init__(application=app, title="Steno")
        self.app = app
        self.add_css_class("steno")
        self.set_default_size(1100, 720)
        self.root = root or (data_dir() / "sessions")

        self._css = Gtk.CssProvider()
        self._apply_css()

        self.live = LiveView(on_ask=self._ask, on_toggle_record=self._toggle_record)
        self.archive = ArchiveView(self.root, on_realign=self._realign)

        self.stack = Adw.ViewStack()
        self.stack.add_titled_with_icon(
            self.live, "live", "Live", "media-record-symbolic"
        )
        self.stack.add_titled_with_icon(
            self.archive, "archive", "Archive", "document-open-recent-symbolic"
        )
        # Connected after the pages, so adding them does not fire it.
        self.stack.connect("notify::visible-child-name", self._on_view_changed)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self._build_header())
        toolbar.set_content(self.stack)
        self.set_content(toolbar)

        self._install_actions()
        self.connect("close-request", self._on_close)
        GLib.timeout_add_seconds(30, self._tick_pause)
        self._adopt_running_meeting()
        self.set_state(app.status, app.detail)

    # ----------------------------------------------------------------- building

    def _build_header(self) -> Gtk.Widget:
        header = Adw.HeaderBar()

        self.state_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.state_box.add_css_class("state-idle")
        self.state_dot = Gtk.Label(label="●")
        self.state_dot.add_css_class("state-dot")
        self.state_word = Gtk.Label(label=STATE_WORDS["idle"])
        self.state_word.add_css_class("chrome")
        self.state_box.append(self.state_dot)
        self.state_box.append(self.state_word)
        header.pack_start(self.state_box)

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        self.record_button = Gtk.Button(label="Record")
        self.record_button.add_css_class("suggested-action")
        self.record_button.connect("clicked", lambda _b: self._toggle_record())
        header.pack_end(self.record_button)

        menu = Gio.Menu()
        pause_menu = Gio.Menu()
        for minutes, label in PAUSE_CHOICES:
            pause_menu.append_item(Gio.MenuItem.new(label, f"app.pause({minutes})"))
        pause_menu.append("Resume detection", "app.unpause")
        menu.append_section("Pause detection", pause_menu)
        menu.append("Keyboard shortcuts", "win.shortcuts")
        menu.append("Quit Steno (stops listening)", "app.quit")

        button = Gtk.MenuButton()
        button.set_icon_name("open-menu-symbolic")
        button.set_menu_model(menu)
        header.pack_end(button)
        return header

    def _apply_css(self) -> None:
        self._css.load_from_string(
            stylesheet(palette(), self.live.transcript_pt)
            if hasattr(self, "live")
            else stylesheet(palette())
        )
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, self._css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _install_actions(self) -> None:
        def add(name: str, fn, param: str | None = None):
            action = Gio.SimpleAction.new(
                name, GLib.VariantType.new(param) if param else None
            )
            action.connect("activate", fn)
            self.add_action(action)

        add("shortcuts", lambda _a, _p: self._show_shortcuts())
        add("ask", lambda _a, _p: self._focus_ask())
        add("zoom-in", lambda _a, _p: self._zoom(1))
        add("zoom-out", lambda _a, _p: self._zoom(-1))
        add("toggle-record", lambda _a, _p: self._toggle_record())
        add("find", lambda _a, _p: self._focus_search())
        # Close is not quit: it hides the window and the listener keeps going.
        add("close", lambda _a, _p: self.close())

        app = self.get_application()
        if app is not None:
            app.set_accels_for_action("win.ask", ["<Control>g"])
            app.set_accels_for_action("win.zoom-in", ["<Control>plus", "<Control>equal"])
            app.set_accels_for_action("win.zoom-out", ["<Control>minus"])
            app.set_accels_for_action("win.toggle-record", ["<Control>r"])
            app.set_accels_for_action("win.find", ["<Control>f"])
            app.set_accels_for_action("win.close", ["<Control>w"])

    def _adopt_running_meeting(self) -> None:
        """Catch up if a meeting was already going when this window opened.

        Normal now that the app runs headless: the window is often opened
        halfway through, and a live transcript that starts from the moment you
        happened to look is nearly useless.
        """
        engine = self.app.engine
        if engine is None or engine.current is None:
            return
        session_dir = engine.current.session_dir
        self.live.bind_session(session_dir)
        for record in load_records(session_dir):
            self.live.add_line(record.get("speaker", "them"), record.get("text", ""))
        self._show_recording(True)

    # ------------------------------------------------------------ engine events

    def on_event(self, event: Event) -> None:
        """Called by the app, on the GTK thread."""
        if isinstance(event, MeetingStarted):
            self.live.clear()
            self.live.bind_session(event.session_dir)
            self.stack.set_visible_child_name("live")
            self._show_recording(True)

        elif isinstance(event, Heard):
            if event.final:
                self.live.add_line(event.speaker, event.text)
            else:
                self.live.set_interim(event.text)

        elif isinstance(event, MeetingEnded):
            self.live.flush_notes()
            self._show_recording(False)

        elif isinstance(event, SummaryReady):
            self.archive.refresh(select=event.session_dir)
            self._toast(f"Summary ready — {event.title}")

        elif isinstance(event, AdviceDelta):
            self.live.advice_delta(event.text)

        elif isinstance(event, AdviceDone):
            self.live.advice_done(event.text)

        elif isinstance(event, EngineError):
            self._toast(f"{event.where}: {event.message}")

    def set_state(self, status: str, detail: str = "") -> None:
        for name in STATE_WORDS:
            self.state_box.remove_css_class(f"state-{name}")
        self.state_box.add_css_class(f"state-{status}")
        word = STATE_WORDS.get(status, status)
        if status == "paused":
            until = paused_until()
            if until:
                mins = max(0, int((until - _now()) / 60))
                word = f"Paused · {mins}m left"
        elif detail:
            word = f"{word} · {detail}"
        self.state_word.set_text(word)
        self._show_recording(status == "recording")

    def _show_recording(self, recording: bool) -> None:
        self.record_button.set_label("Stop" if recording else "Record")
        self.record_button.remove_css_class(
            "suggested-action" if recording else "destructive-action"
        )
        self.record_button.add_css_class(
            "destructive-action" if recording else "suggested-action"
        )

    def refresh_if_stale(self) -> None:
        """Catch up with anything the CLI changed while the window was hidden."""
        self.archive.refresh_if_stale()

    def _on_view_changed(self, *_a) -> None:
        if self.stack.get_visible_child_name() == "archive":
            self.archive.refresh_if_stale()

    def show_session(self, session_dir: Path | None) -> None:
        """Open the archive on one meeting — where a summary notification lands."""
        self.stack.set_visible_child_name("archive")
        self.archive.refresh(select=session_dir)

    # ------------------------------------------------------------ user gestures

    def _toggle_record(self) -> None:
        self.app.toggle_record()

    def _ask(self, question: str) -> None:
        engine, bridge = self.app.engine, self.app.bridge
        if engine is None or bridge is None:
            return
        bridge.submit(engine.ask(question))

    def _focus_ask(self) -> None:
        self.stack.set_visible_child_name("live")
        self.live.ask_entry.grab_focus()

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

    def _focus_search(self) -> None:
        self.stack.set_visible_child_name("archive")
        self.archive.focus_search()

    def _zoom(self, delta: int) -> None:
        self.live.zoom(delta)
        self._apply_css()

    def _tick_pause(self) -> bool:
        """Let the header count a pause down, and notice when it lapses."""
        status = self.app.status
        if status in ("paused", "idle"):
            self.set_state("paused" if paused_until() else "idle")
        return GLib.SOURCE_CONTINUE

    def _toast(self, text: str) -> None:
        # Never steal focus or animate during a meeting; a toast is the loudest
        # this window is allowed to be while someone is talking.
        print(f"[steno] {text}", flush=True)
        self.state_word.set_tooltip_text(text)

    def _show_shortcuts(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Keyboard shortcuts",
            body=(
                "Ctrl+G\tAsk about this meeting\n"
                "Ctrl+F\tSearch the archive\n"
                "Ctrl+R\tStart or stop recording\n"
                "Ctrl+ +/-\tTranscript size\n"
                "Ctrl+W\tClose the window (keeps listening)\n"
                "Ctrl+Q\tQuit (stops listening)"
            ),
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
        self.archive.flush_notes()
        self.archive.stop_playback()
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
            self.set_state("finalizing", "wrapping up")
            self.app.quit()

        dialog.connect("response", answered)
        dialog.present(self)


def _now() -> float:
    import time

    return time.time()
