"""A room kept on this laptop, marked so a backup leaves it out too.

Room mode keeps a room's recording on the laptop until the user says
everyone there agreed (`room.awaits_agreement`: the mic pass has not run,
`mic_pending`, and the meeting is a room). Sync reads meta.json and
leaves such a meeting alone (`sync.is_kept`). A backup reads no
meta.json, so for as long as a room waits an empty `.steno-kept` file
lies beside it, and restic's `--exclude-if-present .steno-kept` leaves
the folder out of every snapshot. A call never carries it, not even one
whose mic pass failed: that is synced and backed up as it always was.

A recording started by hand is marked from its first second, since no
one knows yet whether it is a room. Order keeps the marker a superset of
the wait: it is written before meta.json says a room waits, and removed
only after it no longer does. A crash in between leaves a meeting out of
one backup, never in one it should not be. `mark_all` puts any drift
right at startup.
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path

from .room import awaits_agreement

MARKER = ".steno-kept"
META_FILENAME = "meta.json"
TRANSCRIPT_FILENAME = "transcript.jsonl"


def set_kept(session_dir: Path, *, kept: bool) -> None:
    """Write or remove the marker. Raises OSError if that fails."""
    marker = session_dir / MARKER
    if kept:
        marker.touch(exist_ok=True)
    else:
        marker.unlink(missing_ok=True)


def has_marker(session_dir: Path) -> bool:
    return (session_dir / MARKER).is_file()


def read_meta(meta_path: Path) -> dict | None:
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def read_speakers(transcript_path: Path) -> list[dict]:
    """Who spoke on each line of a transcript.jsonl, as much of each
    line as `room.is_room` reads; a line that does not parse is left
    out, a file that cannot be read gives none."""
    try:
        lines = transcript_path.read_text().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        with contextlib.suppress(ValueError):
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append({"speaker": row.get("speaker")})
    return rows


def waits(session_dir: Path) -> bool:
    """Whether a meeting is a room kept for the user's word. A meeting
    whose meta.json cannot be read says nothing."""
    meta = read_meta(session_dir / META_FILENAME)
    if meta is None or not meta.get("mic_pending"):
        return False
    return awaits_agreement(meta, read_speakers(
        session_dir / TRANSCRIPT_FILENAME))


def mark_all(session_dirs: list[Path]) -> list[Path]:
    """Make each meeting's marker match whether it waits. Returns the
    meetings whose marker changed. A meeting whose meta.json cannot be
    read keeps whatever marker it has."""
    changed = []
    for session_dir in session_dirs:
        if read_meta(session_dir / META_FILENAME) is None:
            continue
        kept = waits(session_dir)
        if kept == has_marker(session_dir):
            continue
        with contextlib.suppress(OSError):
            set_kept(session_dir, kept=kept)
            changed.append(session_dir)
    return changed
