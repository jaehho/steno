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

APP_ID = "dev.jaeho.Steno"
OBJECT_PATH = "/" + APP_ID.replace(".", "/")


def _bus():
    from gi.repository import Gio

    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def is_running() -> bool:
    """Does an app own our name on the session bus?"""
    try:
        from gi.repository import Gio, GLib

        bus = _bus()
        reply = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", (APP_ID,)),
            GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 1000, None,
        )
        return bool(reply.unpack()[0])
    except Exception:  # noqa: BLE001 — no bus, no app; the caller decides
        return False


def activate(action: str, args: list | None = None) -> bool:
    """Activate one app action in the running instance. False if it isn't up."""
    if not is_running():
        return False
    try:
        from gi.repository import Gio, GLib

        bus = _bus()
        params = [GLib.Variant("i", a) if isinstance(a, int) else GLib.Variant("s", a)
                  for a in (args or [])]
        bus.call_sync(
            APP_ID, OBJECT_PATH, "org.gtk.Actions", "Activate",
            GLib.Variant("(sava{sv})", (action, params, {})),
            None, Gio.DBusCallFlags.NONE, 2000, None,
        )
        return True
    except Exception:  # noqa: BLE001 — a failed click must never take down a bar
        return False


def launch(background: bool = False) -> bool:
    """Start the app detached, so a bar click doesn't own the process."""
    exe = shutil.which("steno")
    cmd = [exe] if exe else [sys.executable, "-m", "steno.cli"]
    cmd.append("gui")
    if background:
        cmd.append("--background")
    try:
        subprocess.Popen(  # our own binary, no shell, fixed argv
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),
        )
        return True
    except OSError:
        return False


def show() -> bool:
    """Bring the window up, starting the app first if it isn't running."""
    if activate("show"):
        return True
    return launch()
