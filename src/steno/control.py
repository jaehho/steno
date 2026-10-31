"""Talking to the running app from outside it.

The app already exports its actions on the session bus — every GApplication
does — so a bar click or a keybind can drive it without a socket, a pidfile, or
a second copy of the pipeline. This module imports `Gio` only: no Gtk, no
display, so it stays usable from a bar script and over SSH.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping

APP_ID = "dev.jaeho.Steno"
OBJECT_PATH = "/" + APP_ID.replace(".", "/")
# A launched app owns its bus name before its main loop runs, and an action
# call waits for that loop. Seconds to wait for the name, and for the answer.
START_WAIT_S = 20


def _bus():
    from gi.repository import Gio  # noqa: PLC0415  # gi loads a display; keep it lazy

    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def is_running() -> bool:
    """Does an app own our name on the session bus?"""
    try:
        from gi.repository import (  # noqa: PLC0415  # gi loads a display; keep it lazy
            Gio,
            GLib,
        )

        bus = _bus()
        reply = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", (APP_ID,)),
            GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 1000, None,
        )
        return bool(reply.unpack()[0])
    except Exception:
        return False


def platform_data(environ: Mapping[str, str]) -> dict[str, str]:
    """The activation token this command was started with, under the two
    names GTK reads it by, so the window it asks for may take focus."""
    token = environ.get("XDG_ACTIVATION_TOKEN") or environ.get(
        "DESKTOP_STARTUP_ID"
    )
    return (
        {"activation-token": token, "desktop-startup-id": token}
        if token else {}
    )


def activate(
    action: str, args: list | None = None, timeout_ms: int = 2000
) -> bool:
    """Activate one app action in the running instance. False if it isn't up."""
    if not is_running():
        return False
    try:
        from gi.repository import (  # noqa: PLC0415  # gi loads a display; keep it lazy
            Gio,
            GLib,
        )

        bus = _bus()
        params = [GLib.Variant("i", a) if isinstance(a, int) else GLib.Variant("s", a)
                  for a in (args or [])]
        platform = {
            k: GLib.Variant("s", v)
            for k, v in platform_data(os.environ).items()
        }
        bus.call_sync(
            APP_ID, OBJECT_PATH, "org.gtk.Actions", "Activate",
            GLib.Variant("(sava{sv})", (action, params, platform)),
            None, Gio.DBusCallFlags.NONE, timeout_ms, None,
        )
    except Exception:
        return False
    else:
        return True


def _spawn(background: bool) -> subprocess.Popen | None:
    """Start the app detached, so a bar click doesn't own the process."""
    exe = shutil.which("steno")
    cmd = [exe] if exe else [sys.executable, "-m", "steno.cli"]
    cmd.append("gui")
    if background:
        cmd.append("--background")
    try:
        return subprocess.Popen(  # our own binary, no shell, fixed argv
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),
        )
    except OSError:
        return None


def launch(background: bool = False) -> bool:
    """Start the app detached. False if it could not even be spawned."""
    return _spawn(background) is not None


def show() -> bool:
    """Bring the window up, starting the app first if it isn't running."""
    if activate("show"):
        return True
    return launch()


def _app_is_up() -> bool:
    """Is an app on the bus, after starting one with no window if not?

    False when it would not start, or exited on its own (its preflight says
    why), or never appeared.
    """
    if is_running():
        return True
    proc = _spawn(background=True)
    if proc is None:
        return False
    deadline = time.monotonic() + START_WAIT_S
    while not is_running():
        if proc.poll() is not None or time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def show_session(name: str) -> bool:
    """Open one meeting in the window, starting the app first if it isn't
    running. False when the call did not reach an app."""
    if not _app_is_up():
        return False
    return activate("open-session", [name], timeout_ms=START_WAIT_S * 1000)


def toggle_record() -> bool:
    """Start or stop recording, starting the app first if it isn't running.

    False when the call did not reach an app: it would not start, it exited
    on its own (its preflight says why), or it never answered.
    """
    if not _app_is_up():
        return False
    # One long call, never a retry: a toggle that was answered late and then
    # sent again would stop the recording it had just started.
    return activate("toggle-record", timeout_ms=START_WAIT_S * 1000)
