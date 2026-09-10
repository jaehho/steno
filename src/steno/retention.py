"""Dropping old audio, and removing meetings entirely.

An hour of a two-sided meeting is around 200 MB of WAV, and the text that came
out of it is 20 KB. The words are what gets reread; the audio matters for the
week or so where you might want to hear the sentence again.

Off by default. Deleting a user's recording is not something a tool should
decide to start doing on its own, so this only runs when a number is set — and
even then it refuses to touch a session whose words were never extracted.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from .summarize import SUMMARY_FILENAME, iter_sessions, transcript_path

AUDIO_FILES = ("you.wav", "them.wav", "you.clean.wav", "them.clean.wav")
KEEP_DAYS_ENV = "STENO_KEEP_AUDIO_DAYS"


def keep_days() -> int:
    """Days of audio to keep. 0 (the default) means keep everything, forever."""
    try:
        return max(0, int(os.environ.get(KEEP_DAYS_ENV, "0")))
    except ValueError:
        return 0


def audio_bytes(session_dir: Path) -> int:
    total = 0
    for name in AUDIO_FILES:
        try:
            total += (session_dir / name).stat().st_size
        except OSError:
            pass
    return total


def total_audio_bytes(root: Path | None = None) -> int:
    return sum(audio_bytes(p) for p in iter_sessions(root))


def is_prunable(session_dir: Path, cutoff: float) -> bool:
    """Old enough, and its words are safely on disk.

    Both conditions matter. A session with no summary may still be summarized
    later, and that needs the audio only if the transcript is missing — but a
    session with no transcript at all has nothing *but* audio, and deleting it
    would delete the meeting.
    """
    if not transcript_path(session_dir).is_file():
        return False
    if not (session_dir / SUMMARY_FILENAME).is_file():
        return False
    if audio_bytes(session_dir) == 0:
        return False
    try:
        return transcript_path(session_dir).stat().st_mtime < cutoff
    except OSError:
        return False


def prune_audio(root: Path | None = None, days: int | None = None) -> list[Path]:
    """Delete audio from sessions older than the retention window."""
    window = keep_days() if days is None else days
    if window <= 0:
        return []
    cutoff = time.time() - window * 86400
    pruned = []
    for session_dir in iter_sessions(root):
        if not is_prunable(session_dir, cutoff):
            continue
        freed = audio_bytes(session_dir)
        for name in AUDIO_FILES:
            try:
                (session_dir / name).unlink()
            except OSError:
                pass
        _mark_pruned(session_dir, freed)
        pruned.append(session_dir)
    return pruned


def _mark_pruned(session_dir: Path, freed: int) -> None:
    """Record that the audio is gone on purpose, so the archive can say so."""
    meta_path = session_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        meta = {}
    meta["audio_pruned_at"] = time.time()
    meta["audio_freed_bytes"] = freed
    try:
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    except OSError:
        pass


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n} B"


# --------------------------------------------------------------------- deleting

def trash_dir() -> Path:
    """The freedesktop trash, which is where a deleted meeting should go.

    Implemented here rather than through `Gio.File.trash()` so that deleting
    works from the CLI and stays testable: nothing else in the engine imports a
    GUI toolkit, and this is the kind of operation that most needs a test.
    """
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "Trash"


def _unique(directory: Path, name: str) -> str:
    """A name not already taken in the trash, so nothing is overwritten."""
    if not (directory / name).exists():
        return name
    for n in range(1, 1000):
        candidate = f"{name}-{n}"
        if not (directory / candidate).exists():
            return candidate
    return f"{name}-{int(time.time())}"


def move_to_trash(path: Path) -> Path | None:
    """Move `path` into the trash. None if that is not possible.

    A meeting is gigabytes of audio; copying it across a filesystem boundary to
    delete it would be worse than saying plainly that it cannot be trashed.
    """
    files = trash_dir() / "files"
    info = trash_dir() / "info"
    written: Path | None = None
    try:
        files.mkdir(parents=True, exist_ok=True)
        info.mkdir(parents=True, exist_ok=True)
        name = _unique(files, path.name)
        written = info / f"{name}.trashinfo"
        written.write_text(
            "[Trash Info]\n"
            f"Path={urllib.parse.quote(str(path.resolve()))}\n"
            f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n"  # noqa: DTZ005
        )
        destination = files / name
        os.rename(path, destination)
    except OSError:
        # Cross-filesystem, no permission, no space for the info file: whatever
        # the reason, leave nothing behind pointing at a file still in place.
        if written is not None:
            with contextlib.suppress(OSError):
                written.unlink()
        return None
    return destination


def delete_session(session_dir: Path, permanent: bool = False) -> tuple[str, Path | None]:
    """Remove one meeting. Returns ("trashed"|"deleted"|"failed", where).

    Trash unless the caller explicitly insists otherwise, and **never** fall
    back from one to the other: this deletes a recording of a conversation that
    cannot be had again. A failed trash is reported as a failure so the person
    can decide, rather than being quietly upgraded to a permanent delete.
    """
    if not session_dir.is_dir():
        return ("failed", None)
    if not permanent:
        moved = move_to_trash(session_dir)
        return ("trashed", moved) if moved is not None else ("failed", None)
    try:
        shutil.rmtree(session_dir)
    except OSError:
        return ("failed", None)
    return ("deleted", None)


def delete_audio(session_dir: Path) -> int:
    """Drop just the recordings, keeping every word. Returns bytes freed."""
    freed = audio_bytes(session_dir)
    if not freed:
        return 0
    for name in AUDIO_FILES:
        try:
            (session_dir / name).unlink()
        except OSError:
            pass
    _mark_pruned(session_dir, freed)
    return freed
