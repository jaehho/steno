"""The application object: the long-running listener, and the window's owner.

The window is a view onto this, not the other way round. The app holds the
engine, publishes state for the bar, raises notifications, and stays alive with
no window open — which is the only way "it starts itself when a meeting starts"
can be true, since a meeting rarely begins with the user opening this app.

Importing this package pulls in GTK, libadwaita and a display connection, which
is why the CLI imports it inside the `gui` subcommand and nowhere else.
"""
from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib

from .. import migrate_legacy
from ..control import APP_ID
from ..events import (
    Event,
    Finalizing,
    MeetingEnded,
    MeetingStarted,
    StatusChanged,
    SummaryReady,
)
from ..notes import sync_todos
from ..retention import prune_audio
from ..session import paused_until, set_pause
from ..state import clear_state, write_state
from .bridge import EngineBridge

RECORDING_NOTIFICATION = "recording"
REPUBLISH_S = 20


class App(Adw.Application):
    """Runs whether or not anything is on screen.

    Every action here works with no window: they are reachable over D-Bus (from
    the bar, or a keybind) as well as from the header.
    """

    def __init__(
        self, root: Path | None = None, background: bool = False, **engine_kw
    ) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.root = root
        self.engine_kw = engine_kw
        self.background = background
        self.status = "idle"
        self.detail = ""
        self.since = 0.0
        self.bridge: EngineBridge | None = None
        self._window = None
        self._first_activation = True
        self._said_still_listening = False

    # ------------------------------------------------------------------ startup

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        # Without this the app would exit the moment its last window closed,
        # taking the detector with it. The pipeline outlives every window.
        self.hold()
        for old, new in migrate_legacy():
            print(f"[steno] moved {old} -> {new}", flush=True)
        prune_audio()
        self._install_actions()
        self.bridge = EngineBridge(
            on_event=self._on_event, root=self.root, **self.engine_kw
        )
        self.bridge.start()
        self._publish("paused" if paused_until() else "idle")
        # Republish on a slow timer so the bar heals itself: the runtime file
        # can be swept, or overwritten by a second instance, and an indicator
        # stuck on `off` while the mic is open is the one lie it must not tell.
        GLib.timeout_add_seconds(REPUBLISH_S, self._republish)

    def do_activate(self) -> None:
        # `--background` is the autostart path: come up listening, silently,
        # without throwing a window at someone who just logged in.
        if self.background and self._first_activation:
            self._first_activation = False
            return
        self._first_activation = False
        self.present_window()

    def do_shutdown(self) -> None:
        if self.bridge is not None:
            self.bridge.stop()
        clear_state()
        Adw.Application.do_shutdown(self)

    # ------------------------------------------------------------------ actions

    def _install_actions(self) -> None:
        def add(name: str, fn, param: str | None = None):
            action = Gio.SimpleAction.new(
                name, GLib.VariantType.new(param) if param else None
            )
            action.connect("activate", fn)
            self.add_action(action)

        add("show", lambda _a, _p: self.present_window())
        add("toggle-record", lambda _a, _p: self.toggle_record())
        add("stop-record", lambda _a, _p: self.stop_record())
        add("pause", lambda _a, p: self.pause(p.get_int32()), "i")
        add("unpause", lambda _a, _p: self.pause(None))
        add("quit", lambda _a, _p: self.request_quit())
        add("open-session", lambda _a, p: self.open_session(p.get_string()), "s")

        self.set_accels_for_action("app.quit", ["<Control>q"])

    def present_window(self):
        from .window import Window

        window = self._window
        if window is None:
            window = Window(self, root=self.root)
            window.connect("destroy", self._forget_window)
            self._window = window
        window.present()
        window.refresh_if_stale()
        return window

    def _forget_window(self, *_a) -> None:
        self._window = None

    @property
    def engine(self):
        return self.bridge.engine if self.bridge is not None else None

    def toggle_record(self) -> None:
        engine = self.engine
        if engine is None:
            return
        if engine.current is None:
            engine.start_now()
        else:
            engine.stop_now()

    def stop_record(self) -> None:
        engine = self.engine
        if engine is not None and engine.current is not None:
            engine.stop_now()

    def pause(self, minutes: int | None) -> None:
        set_pause(minutes * 60 if minutes else None)
        until = paused_until()
        if until:
            self._publish("paused", since=until)
        else:
            self._publish("idle")

    def open_session(self, name: str) -> None:
        window = self.present_window()
        window.show_session(Path(name) if name else None)

    def request_quit(self) -> None:
        """Quit, but never silently discard a meeting that is still recording."""
        engine = self.engine
        if engine is not None and engine.current is not None and self._window is not None:
            self._window.confirm_quit()
            return
        self.quit()

    # ------------------------------------------------------------------- events

    def _on_event(self, event: Event) -> None:
        """The engine's single subscriber. Runs on the GTK thread."""
        if isinstance(event, StatusChanged):
            since = paused_until() if event.status == "paused" else None
            self._publish(event.status, event.detail, since=since)

        elif isinstance(event, MeetingStarted):
            self._notify_recording(event.app)

        elif isinstance(event, MeetingEnded):
            self.withdraw_notification(RECORDING_NOTIFICATION)

        elif isinstance(event, Finalizing):
            self._publish("finalizing", event.step)

        elif isinstance(event, SummaryReady):
            # Derived here, not in the engine, and not in the window: the window
            # is usually closed, and todos that only appear when someone happens
            # to be watching are not todos.
            sync_todos(event.session_dir, event.body)
            self._notify_summary(event.session_dir, event.title)

        if self._window is not None:
            self._window.on_event(event)

    def _republish(self) -> bool:
        """Re-assert what we are doing, and notice a pause that has lapsed."""
        if self.status in ("idle", "paused"):
            until = paused_until()
            self._publish("paused" if until else "idle", since=until)
        else:
            write_state(self.status, self.detail, since=self.since)
        return GLib.SOURCE_CONTINUE

    def _publish(self, status: str, detail: str = "", since: float | None = None) -> None:
        self.status = status
        self.detail = detail
        self.since = since or GLib.get_real_time() / 1e6
        write_state(status, detail, since=self.since)
        if self._window is not None:
            self._window.set_state(status, detail)

    # ------------------------------------------------------------ notifications

    def _notify_recording(self, app: str) -> None:
        """Say it out loud, every time, with the stop button attached.

        Recording other people without their knowing is the one thing this tool
        must never do quietly — and with the window closed, this notification is
        the only signal the user gets at the moment it starts.
        """
        note = Gio.Notification.new("Recording")
        note.set_body(f"{app} opened the microphone.")
        note.add_button("Stop", "app.stop-record")
        note.set_default_action("app.show")
        note.set_priority(Gio.NotificationPriority.NORMAL)
        self.send_notification(RECORDING_NOTIFICATION, note)

    def _notify_summary(self, session_dir: Path, title: str) -> None:
        note = Gio.Notification.new(title or "Meeting summarized")
        note.set_body("Summary and todos are ready.")
        note.set_default_action_and_target(
            "app.open-session", GLib.Variant("s", str(session_dir))
        )
        self.send_notification(f"summary-{session_dir.name}", note)

    def note_still_listening(self) -> None:
        """Once per run, explain that closing the window didn't stop anything."""
        if self._said_still_listening:
            return
        self._said_still_listening = True
        note = Gio.Notification.new("Steno is still listening")
        note.set_body("It keeps watching for meetings with the window closed.")
        note.set_default_action("app.show")
        self.send_notification("still-listening", note)


def run(root: Path | None = None, background: bool = False, **engine_kw) -> int:
    return App(root=root, background=background, **engine_kw).run(None)
