"""The name this process goes by on the desktop."""
from __future__ import annotations

from gi.repository import GLib

NAME = "Steno"


def name_process() -> None:
    """Name the process "Steno" in notifications and PulseAudio, once.

    Notifications carry it as their app name, which swaync's rules
    match; PulseAudio keys a stream's saved volume by it, so without it
    the stream is `python3` and shares its volume with every other
    Python program. GLib warns when the name is set twice, so a second
    call does nothing.
    """
    if GLib.get_application_name() == NAME:
        return
    GLib.set_prgname("steno")
    GLib.set_application_name(NAME)
