"""Whether a detected call is kept by default, or only once you say so.

Where Steno has run so far, one party's consent is enough to record a call,
and you are that party. In Portugal it is not: Codigo Penal art. 199(1)
punishes recording another person's words, not meant for the public,
without their consent, even when they were said to you, and also using
such a recording, however lawfully it was made. So the right default
depends on where the laptop is, not on taste.

Either way a detected call is recorded from its first second, so the
start of a meeting is never lost to a question. `STENO_CONSENT=auto`
keeps it unless you press Don't record, which deletes it. `ask` deletes
it when the call ends unless you press Keep. Unset, a machine whose time
zone is in Europe asks and any other keeps. The zone is read at each
detection, so changing it on arrival needs no restart.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

SWITCH_ENV = "STENO_CONSENT"
ASK = "ask"
AUTO = "auto"
MODES = (ASK, AUTO)
# Zones where recording a call needs everyone on it to agree: Portugal's
# penal code, and in most of Europe a criminal or data-protection rule.
ASK_ZONE_PREFIXES = ("Europe/",)
LOCALTIME = Path("/etc/localtime")

log = logging.getLogger("steno.consent")


def consent_mode(setting: str, zone: str) -> str:
    """`ask` or `auto`, from a `STENO_CONSENT` value and an IANA zone.

    A blank setting follows the zone: `ask` in Europe/*, `auto` anywhere
    else, an unknown zone included. Raises ValueError for a setting that
    is neither blank, `ask` nor `auto`.
    """
    value = setting.strip().lower()
    if not value:
        return ASK if zone.startswith(ASK_ZONE_PREFIXES) else AUTO
    if value not in MODES:
        msg = f"{SWITCH_ENV}={setting!r}: expected 'ask' or 'auto'"
        raise ValueError(msg)
    return value


def machine_zone(tz: str = "", localtime: Path = LOCALTIME) -> str:
    """The IANA zone this machine runs in, or "" when nothing names one.

    `tz` (the `TZ` variable) wins when set; otherwise the zone is the
    part of `localtime`'s link target after `zoneinfo/`, which is what
    `timedatectl set-timezone` writes.
    """
    if tz:
        return tz.removeprefix(":")
    try:
        target = str(localtime.readlink())
    except OSError:
        return ""
    _, found, zone = target.partition("zoneinfo/")
    return zone if found else ""


def consent_from_env(environ: Mapping[str, str] = os.environ) -> str:
    """The mode the running app uses now. A setting it cannot read asks."""
    zone = machine_zone(environ.get("TZ", ""))
    try:
        return consent_mode(environ.get(SWITCH_ENV, ""), zone)
    except ValueError as e:
        log.warning("%s; asking before each recording", e)
        return ASK
