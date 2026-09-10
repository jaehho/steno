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

CONFIG = Path.home() / ".config" / "hypr" / "hyprland.lua"
MARK = "steno gui --background"


def command() -> str:
    """Absolute path if we can find one.

    Hyprland's exec inherits the environment the compositor was started with,
    which often does not include `~/.local/bin` — the neighbouring lines in this
    very config spell that directory out for exactly this reason.
    """
    stable = [Path.home() / ".local/bin/steno", Path("/usr/bin/steno")]
    found = next((p for p in stable if p.is_file()), None)
    # `which` last: run under `uv run`, it points into the project venv, which is
    # the one path that stops working the moment the checkout moves.
    exe = str(found) if found else (shutil.which("steno") or "steno")
    return f"{exe} gui --background"


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
    raise SystemExit("could not find the autostart block in hyprland.lua")


def remove(text: str) -> str:
    return "".join(row for row in text.splitlines(keepends=True) if MARK not in row)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--remove", action="store_true", help="take the line back out")
    ap.add_argument("--dry-run", action="store_true", help="print, don't write")
    args = ap.parse_args()

    if not CONFIG.is_file():
        raise SystemExit(f"no hyprland config at {CONFIG}")
    before = CONFIG.read_text()
    after = remove(before) if args.remove else add(before)

    if before == after:
        print("hyprland.lua already up to date")
        return
    if args.dry_run:
        print(f"--- would rewrite {CONFIG}")
        return
    copy = CONFIG.with_suffix(CONFIG.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(CONFIG, copy)
    CONFIG.write_text(after)
    print(f"backed up {copy.name}")
    print("steno will start with the session from the next login")


if __name__ == "__main__":
    main()
