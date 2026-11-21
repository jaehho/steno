"""Following a change of time zone without a restart (issue #44).

Folder names, transcript stamps and the window's dates are local times,
which Python gets from glibc. glibc reads /etc/localtime once, and again
only when `time.tzset()` runs, so a zone changed under a running steno
went unseen until it restarted. `timedatectl set-timezone` writes the
file and then says so on the system bus: timedated emits
PropertiesChanged for its `Timezone` property (systemd v262,
`method_set_timezone` in src/timedate/timedated.c). The app listens for
that, calls `time.tzset()`, and draws its times again.

A zone set through `TZ` is the environment's, and no signal changes it.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

from gi.repository import Gio, GLib

TIMEDATE_PATH = "/org/freedesktop/timedate1"
TIMEDATE_INTERFACE = "org.freedesktop.timedate1"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
ZONE_PROPERTY = "Timezone"

log = logging.getLogger("steno.zone")


def names_zone(changed: dict, invalidated: list[str]) -> bool:
    """Does a PropertiesChanged from timedated say the zone changed?

    timedated sends the new value in `changed`; a property that is only
    invalidated would be named in `invalidated`, and counts the same.
    """
    return ZONE_PROPERTY in changed or ZONE_PROPERTY in invalidated


def watch_zone(
    bus: Gio.DBusConnection, on_change: Callable[[], None]
) -> int:
    """Re-read the zone and call `on_change` each time timedated says
    its `Timezone` changed. Returns the id `bus.signal_unsubscribe`
    takes.

    The match leaves the sender open: timedated is started on demand
    and exits when idle, and a signal from anyone else would only make
    `time.tzset()` read the same file again.
    """

    def on_signal(
        _bus: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        _member: str,
        parameters: GLib.Variant,
    ) -> None:
        _name, changed, invalidated = parameters.unpack()
        if not names_zone(changed, invalidated):
            return
        time.tzset()
        log.info(
            "time zone is now %s (%s)",
            changed.get(ZONE_PROPERTY, "?"), time.tzname[0],
        )
        on_change()

    return bus.signal_subscribe(
        None,
        PROPERTIES_INTERFACE,
        "PropertiesChanged",
        TIMEDATE_PATH,
        TIMEDATE_INTERFACE,
        Gio.DBusSignalFlags.NONE,
        on_signal,
    )
