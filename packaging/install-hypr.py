#!/usr/bin/env python
"""Start the listener with the Hyprland session.

`~/.config/autostart` is an XDG convention that Hyprland does not read on its
own — this config starts its daemons from an `hyprland.start` block instead, so
that is where Steno belongs. Same rules as the waybar installer: one anchored,
idempotent insertion, a timestamped backup, and `--remove` to undo it.

    python packaging/install-hypr.py            # install
    python packaging/install-hypr.py --remove   # take it back out
    python packaging/install-hypr.py --dry-run  # show what would change
"""
from __future__ import annotations

import argparse
import re
import shutil
import time
from pathlib import Path

from steno.autostart import MARK, listener_command

CONFIG = Path.home() / ".config" / "hypr" / "hyprland.lua"


def command() -> str:
    return listener_command()


def line() -> str:
    return f'    hl.exec_cmd("{command()}")  -- meeting listener, no window\n'


# Where to slip it in: after the last daemon in the start block, before the
# workspace tools, falling back to the end of the block.
ANCHORS = (
    re.compile(r"^\s*-- Workspace management daemons\s*$", re.MULTILINE),
    re.compile(r"^end\)\s*$", re.MULTILINE),
)


def add(text: str) -> str:
    if MARK in text:
        return text
    for anchor in ANCHORS:
        match = anchor.search(text)
        if match is None:
            continue
        at = text.rfind("\n", 0, match.start()) + 1
        return text[:at] + line() + text[at:]
    msg = "could not find the autostart block in hyprland.lua"
    raise SystemExit(msg)


def remove(text: str) -> str:
    return "".join(row for row in text.splitlines(keepends=True) if MARK not in row)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--remove", action="store_true", help="take the line back out")
    ap.add_argument("--dry-run", action="store_true", help="print, don't write")
    args = ap.parse_args()

    if not CONFIG.is_file():
        msg = f"no hyprland config at {CONFIG}"
        raise SystemExit(msg)
    before = CONFIG.read_text()
    after = remove(before) if args.remove else add(before)

    if before == after:
        return
    if args.dry_run:
        return
    copy = CONFIG.with_suffix(CONFIG.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(CONFIG, copy)
    CONFIG.write_text(after)


if __name__ == "__main__":
    main()
