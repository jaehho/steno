#!/usr/bin/env python
"""Wire the Steno indicator into an existing waybar config.

Waybar's config is JSONC — it has comments, and the user wrote them — so this
edits the text rather than parsing and re-emitting it, which would silently
delete every comment in the file. Two insertions, both anchored and both
idempotent, with a timestamped backup of anything it touches.

    python packaging/install-waybar.py            # install
    python packaging/install-waybar.py --remove   # take it back out
    python packaging/install-waybar.py --dry-run  # show what would change
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

WAYBAR = Path.home() / ".config" / "waybar"
CONFIG = WAYBAR / "config.jsonc"
STYLE = WAYBAR / "style.css"
HERE = Path(__file__).resolve().parent

BEGIN = "/* >>> steno >>> */"
END = "/* <<< steno <<< */"

def module() -> str:
    """The module definition, with the icons the app itself publishes.

    Imported rather than duplicated: a bar whose glyphs drifted from the app's
    states would be a slow, confusing kind of wrong.
    """
    icons = ",\n".join(
        f'            "{name}": "{glyph}"' for name, glyph in _icons().items()
    )
    return (
        '\n    "custom/steno": {\n'
        '        "format": "{icon} {text}",\n'
        '        "format-icons": {\n' + icons + "\n"
        "        },\n"
        '        "return-type": "json",\n'
        '        "exec": "steno bar",\n'
        '        "restart-interval": 5,\n'
        '        "on-click": "steno show",\n'
        '        "on-click-right": "steno toggle",\n'
        '        "on-click-middle": "steno pause 60m",\n'
        '        "tooltip": true\n'
        "    },\n"
    )


def _icons() -> dict:
    sys.path.insert(0, str(HERE.parent / "src"))
    from steno.state import ICONS

    return ICONS


def backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    copy = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, copy)
    return copy


def add_module(text: str) -> str:
    """Insert the module definition just after the modules-right array."""
    if '"custom/steno"' in text and '"exec": "steno bar"' in text:
        return text
    match = re.search(r'"modules-right"\s*:\s*\[.*?\],\n', text, re.DOTALL)
    if match is None:
        raise SystemExit("could not find a modules-right array in config.jsonc")
    at = match.end()
    return text[:at] + module() + text[at:]


def add_to_bar(text: str) -> str:
    """Name it in modules-right, to the left of the notification bell."""
    if re.search(r'"modules-right"\s*:\s*\[[^\]]*"custom/steno"', text, re.DOTALL):
        return text
    match = re.search(r'("modules-right"\s*:\s*\[\n)(\s*)', text)
    if match is None:
        raise SystemExit("could not find a modules-right array in config.jsonc")
    indent = match.group(2)
    return text[:match.end(1)] + f'{indent}"custom/steno",\n' + text[match.end(1):]


def remove_module(text: str) -> str:
    text = re.sub(r'\n\s*"custom/steno"\s*:\s*\{.*?\n    \},\n', "\n", text, flags=re.DOTALL)
    return re.sub(r'\n\s*"custom/steno",', "", text)


def css() -> str:
    """Style the indicator in the user's own palette, not in ours."""
    sys.path.insert(0, str(HERE.parent / "src"))
    from steno.gui.style import palette

    c = palette()
    return f"""{BEGIN}
#custom-steno {{ padding: 0 10px; color: {c['overlay1']}; }}
#custom-steno.idle {{ color: {c['overlay0']}; }}
#custom-steno.off {{ color: {c['surface2']}; }}
#custom-steno.finalizing {{ color: {c['blue']}; }}
#custom-steno.paused {{ color: {c['yellow']}; }}
/* The one state allowed to be loud. Anything quieter stops being read, and an
   indicator nobody reads does not tell anyone they are being recorded. */
#custom-steno.recording {{
    color: {c['crust']};
    background: {c['red']};
    border-radius: 6px;
    font-weight: 600;
}}
{END}
"""


def splice_css(text: str, block: str | None) -> str:
    without = re.sub(
        re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", text, flags=re.DOTALL
    ).rstrip() + "\n"
    return without if block is None else without + "\n" + block


def reload_waybar() -> None:
    try:
        subprocess.run(["pkill", "-SIGUSR2", "waybar"], check=False)
    except OSError:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--remove", action="store_true", help="take the module back out")
    ap.add_argument("--dry-run", action="store_true", help="print, don't write")
    args = ap.parse_args()

    if not CONFIG.is_file() or not STYLE.is_file():
        raise SystemExit(f"no waybar config at {WAYBAR}")

    config_text = CONFIG.read_text()
    style_text = STYLE.read_text()

    if args.remove:
        new_config = remove_module(config_text)
        new_style = splice_css(style_text, None)
    else:
        new_config = add_to_bar(add_module(config_text))
        new_style = splice_css(style_text, css())

    changed = []
    for path, old, new in ((CONFIG, config_text, new_config), (STYLE, style_text, new_style)):
        if old == new:
            continue
        changed.append(path)
        if args.dry_run:
            print(f"--- would rewrite {path}")
            continue
        print(f"backed up {backup(path).name}")
        path.write_text(new)

    if not changed:
        print("waybar already up to date")
        return
    if not args.dry_run:
        reload_waybar()
        print("waybar reloaded")


if __name__ == "__main__":
    main()
