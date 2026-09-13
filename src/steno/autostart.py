"""Starting the listener at login, as something the user turns on.

The package used to install this system-wide, which made every account on the
machine start recording detected calls at its next login without anyone having
asked. Now it is one per-user XDG autostart entry, written when the user flips
"Start at login" and removed when they flip it back.

Headless and GTK-free, so the CLI and the tests share it with the window.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

FILENAME = "steno.desktop"
SYSTEM_ENTRY = Path("/etc/xdg/autostart") / FILENAME

# Desktops whose session manager reads ~/.config/autostart by itself.
READS_AUTOSTART = {
    "GNOME", "KDE", "XFCE", "X-Cinnamon", "MATE", "LXQt", "LXDE", "Budgie",
    "Pantheon", "Unity", "Deepin", "COSMIC",
}

# Compositor configs that start the listener themselves (`make hypr` writes one).
COMPOSITOR_CONFIGS = ("hypr/hyprland.lua", "hypr/hyprland.conf", "sway/config", "niri/config.kdl")
MARK = "steno gui --background"


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))


def started_by_compositor() -> Path | None:
    """The compositor config that starts the listener, if one does.

    Read-only: that line is the user's own, so the toggle reports it and leaves
    removing it to them.
    """
    for rel in COMPOSITOR_CONFIGS:
        path = _config_home() / rel
        try:
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            # Whole-line comments only: the mark itself contains Lua's `--`.
            if MARK in line and not line.lstrip().startswith(("--", "#", "//")):
                return path
    return None


def user_entry() -> Path:
    return _config_home() / "autostart" / FILENAME


def listener_command() -> str:
    """Absolute path if we can find one.

    A session's startup exec inherits the environment the session was started
    with, which often lacks `~/.local/bin`.
    """
    stable = [Path.home() / ".local/bin/steno", Path("/usr/bin/steno")]
    found = next((p for p in stable if p.is_file()), None)
    # `which` last: under `uv run` it points into the project venv, the one path
    # that stops working the moment the checkout moves.
    exe = str(found) if found else (shutil.which("steno") or "steno")
    return f"{exe} gui --background"


def entry_text(hidden: bool = False) -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=Steno (listener)",
        "Comment=Watches for meetings in the background",
        # --background: someone who has just logged in did not ask for a window.
        f"Exec={listener_command()}",
        "Icon=dev.jaeho.Steno",
        "Terminal=false",
        "NoDisplay=true",
        "X-GNOME-Autostart-enabled=true",
    ]
    if hidden:
        lines.append("Hidden=true")
    return "\n".join(lines) + "\n"


def _hidden(path: Path) -> bool:
    try:
        return any(
            line.strip().lower() == "hidden=true" for line in path.read_text().splitlines()
        )
    except OSError:
        return False


def is_enabled() -> bool:
    """The user's entry wins over a system one, per the XDG autostart spec."""
    if started_by_compositor() is not None:
        return True
    user = user_entry()
    if user.is_file():
        return not _hidden(user)
    return SYSTEM_ENTRY.is_file()


def enable() -> None:
    path = user_entry()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(entry_text())


def disable() -> None:
    path = user_entry()
    if SYSTEM_ENTRY.is_file():
        # An older package installed one for everyone; only a user entry marked
        # Hidden can switch that off without root.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entry_text(hidden=True))
    else:
        path.unlink(missing_ok=True)


def session_reads_autostart() -> bool:
    """Will this session actually run the entry? False means "not sure".

    Compositors like Hyprland and sway ignore autostart entries unless they were
    started through systemd (uwsm, for one), which runs them via this target.
    """
    try:
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "xdg-desktop-autostart.target"],
            capture_output=True, text=True, timeout=2, check=False,
        ).stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        active = False
    if active:
        return True
    desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").split(":")
    return any(d in READS_AUTOSTART for d in desktops)
