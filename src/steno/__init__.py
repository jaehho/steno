"""Steno: live meeting capture, notes, and todos."""
from __future__ import annotations

import os
import time
from pathlib import Path

__version__ = "0.11.0"

def model_default() -> str:
    """STENO_MODEL wins; else the active provider's default. Flip it per run."""
    from . import claude  # noqa: PLC0415  # keep httpx out of bare `import steno`
    return (os.environ.get("STENO_MODEL") or os.environ.get("CLAUDE_MODEL")
            or claude.default_model())


ASK_MODEL_ENV = "STENO_ASK_MODEL"


def resolve_ask_model(env: str, pref: str, default: str) -> str:
    """The model that answers a question: the environment's, else the one
    picked in the window, else `default`. A blank one is not a choice."""
    return env.strip() or pref.strip() or default


def ask_model(pref: str = "") -> str:
    """The model for a question about a meeting, given the window's pick.

    Summaries keep `model_default()`; this is only for asking, and falls
    back to it when nobody has chosen.
    """
    return resolve_ask_model(
        os.environ.get(ASK_MODEL_ENV, ""), pref, model_default())


LEGACY_NAME = "meeting-copilot"
APP_NAME = "steno"
# The installed app icon is named for the app id: control.APP_ID, the
# desktop entry's Icon=, and this file all have to agree.
APP_ICON = "dev.jaeho.Steno"


def icon_dir() -> Path:
    """The icon theme shipped inside the package: the app icon and tray states."""
    return Path(__file__).parent / "data" / "icons"


def icon_theme_dir() -> Path:
    """The user hicolor theme, where hosts resolve `Icon=dev.jaeho.Steno`."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "icons" / "hicolor"


def _mtime(path: Path) -> float | None:
    """When `path` was last written, or None if it is not there."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _stamp(path: Path, at: float | None) -> str:
    """`path` with its mtime, or the word `missing` — for one log line."""
    if at is None:
        return f"{path} missing"
    when = time.strftime("%F %T", time.localtime(at))
    return f"{path} mtime {when}"


def cache_verdict(svg_at: float | None, cache_at: float | None) -> str:
    """What a stale or missing theme cache means for a host's lookup.

    `svg_at` and `cache_at` are the two files' mtimes, None when a file
    is absent. An older or missing cache is the proven failure mode:
    waybar and swaync then draw error placeholders instead of the icon.
    """
    if cache_at is None:
        return "cache missing: hosts fall back to error placeholders"
    if svg_at is not None and cache_at < svg_at:
        return "cache older than the icon: hosts fall back to error placeholders"
    if svg_at is None:
        return "icon missing"
    return "cache fresh"


def icon_host_report(theme_dir: Path, icon_name: str = APP_ICON) -> str:
    """One line on the icon a host resolves, and the theme cache beside it.

    Waybar and swaync look `Icon=dev.jaeho.Steno` up under the user
    hicolor theme. This names the installed SVG and `icon-theme.cache`
    there with their mtimes and says which case the host is in, so a
    wrong-looking bar can be told apart from a wrong icon file.
    """
    svg = theme_dir / "scalable" / "apps" / f"{icon_name}.svg"
    cache = theme_dir / "icon-theme.cache"
    svg_at = _mtime(svg)
    cache_at = _mtime(cache)
    return "; ".join((
        _stamp(svg, svg_at), _stamp(cache, cache_at),
        cache_verdict(svg_at, cache_at),
    ))


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def runtime_dir() -> Path:
    """Where the running app publishes its state for the bar to read.

    `XDG_RUNTIME_DIR` is per-login and wiped on logout, which is what we want:
    a state file left behind by a killed process must never be believed.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/{APP_NAME}-{os.getuid()}"
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def migrate_legacy() -> list[tuple[Path, Path]]:
    """Rename pre-`steno` config and data directories into place.

    A rename, not a copy: the archive is gigabytes of audio and duplicating it
    to rename a project would be absurd. Only ever moves onto a path that does
    not exist, so running this twice — or after the user moved things by hand —
    cannot clobber anything.
    """
    moved = []
    for new in (config_dir(), data_dir()):
        old = new.parent / LEGACY_NAME
        if old.is_dir() and not new.exists():
            try:
                old.rename(new)
            except OSError:
                continue
            moved.append((old, new))
    return moved
