"""Join two halves of one conversation that the recorder left as two meetings.

A crash mid-call (the meeting app dies, or the user re-joins from another
client) ends the session when the mic stream is gone for `end_grace_s`, and
the rejoin starts a new one. This is the explicit repair: take the later
folder's audio and words into the earlier one, trash the later folder, and
let the caller write one summary again.

Playback seeks both sides to the same second, so both tracks get the same
prefix length before the later half — silence fills whatever wall-clock gap
the crash left. Transcript `offset`s are shifted by that same prefix; `ts`
stays true wall clock.
"""
from __future__ import annotations

import contextlib
import wave
from dataclasses import dataclass
from pathlib import Path

from .audio import SAMPLE_RATE, open_wav
from .notes import load_todos, save_todos
from .playback import audio_duration, recorded_offset, session_start
from .retention import AUDIO_FILES, delete_session
from .session import write_transcripts
from .summarize import load_meta, load_records, update_meta

NOTES_FILENAME = "notes.md"


@dataclass(frozen=True)
class MergeResult:
    """What a join did. `trash` is `delete_session`'s outcome for the absorb."""

    keep: Path
    absorbed: Path
    shift_s: float
    lines: int
    trash: str


def previous_session(session_dir: Path) -> Path | None:
    """The meeting immediately before this one in the archive, if any.

    The list is newest-first by folder name, so "before" is the next entry
    older than this one. A meeting with no transcript is not a meeting yet
    and is skipped.
    """
    root = session_dir.parent
    names = sorted(
        (p.name for p in root.iterdir() if p.is_dir()),
        reverse=True,
    ) if root.is_dir() else []
    try:
        at = names.index(session_dir.name)
    except ValueError:
        return None
    for name in names[at + 1:]:
        candidate = root / name
        if (candidate / "transcript.jsonl").is_file():
            return candidate
    return None


def join_shift(
    keep_start: float, absorb_start: float, keep_track_seconds: float
) -> float:
    """Seconds of the joined timeline before the later half begins.

    At least the wall-clock gap (so `ts` and audio agree when the crash left
    silence), and at least as long as the earlier half's longest track (so
    both sides keep one shared clock and nothing overlaps).
    """
    return max(absorb_start - keep_start, keep_track_seconds)


def offset_shifted(records: list[dict], shift: float) -> list[dict]:
    """Copy of `records` with measured offsets moved onto the joined timeline.

    Rows without an `offset` are left alone: playback already estimates them
    from `ts`, which is wall clock and stays true.
    """
    out = []
    for record in records:
        row = dict(record)
        if recorded_offset(row) is not None:
            row["offset"] = round(recorded_offset(row) + shift, 3)
        out.append(row)
    return out


def merge_sessions(keep: Path, absorb: Path) -> MergeResult:
    """Join `absorb` into the earlier `keep`, then move `absorb` to the trash.

    `keep` must be the earlier folder. The caller re-summarizes `keep` after
    this returns so the summary and its citations cover both halves.
    """
    if keep.resolve() == absorb.resolve():
        msg = "cannot join a meeting with itself"
        raise ValueError(msg)
    if keep.name >= absorb.name:
        msg = f"{keep.name} is not earlier than {absorb.name}"
        raise ValueError(msg)
    if not (absorb / "transcript.jsonl").is_file():
        msg = f"{absorb.name} has no transcript to join"
        raise ValueError(msg)

    keep_start = session_start(keep)
    absorb_start = session_start(absorb)
    keep_seconds = max(
        (_track_seconds(keep, name) for name in AUDIO_FILES), default=0.0
    )
    shift = join_shift(keep_start, absorb_start, keep_seconds)

    for name in AUDIO_FILES:
        _join_track(keep / name, absorb / name, shift)

    keep_records = load_records(keep)
    absorb_records = offset_shifted(load_records(absorb), shift)
    merged = sorted(keep_records + absorb_records, key=lambda r: r.get("ts", 0.0))
    mic_pending = bool(load_meta(keep).get("mic_pending")) or bool(
        load_meta(absorb).get("mic_pending")
    )
    if mic_pending:
        # The combined `you.wav` is transcribed once by the caller's
        # re-summarize; leaving either half's mic lines would say them twice.
        merged = [r for r in merged if r.get("speaker") != "you"]
    write_transcripts(keep, merged)

    _join_notes(keep, absorb)
    save_todos(keep, load_todos(keep) + load_todos(absorb))
    update_meta(
        keep,
        started_at=keep_start,
        app=load_meta(keep).get("app") or load_meta(absorb).get("app"),
        mic_pending=True if mic_pending else None,
    )

    trash, _where = delete_session(absorb)
    return MergeResult(
        keep=keep,
        absorbed=absorb,
        shift_s=shift,
        lines=len(merged),
        trash=trash,
    )


def _track_seconds(session_dir: Path, name: str) -> float:
    path = session_dir / name
    return audio_duration(path) if path.is_file() else 0.0


def _read_frames(path: Path) -> bytes | None:
    if not path.is_file():
        return None
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.readframes(handle.getnframes())
    except (OSError, wave.Error):
        return None


def _join_track(keep_track: Path, absorb_track: Path, shift: float) -> None:
    """One WAV on the joined timeline: keep + silence to `shift` + absorb."""
    kept = _read_frames(keep_track)
    absorbed = _read_frames(absorb_track)
    if kept is None and absorbed is None:
        return
    kept = kept or b""
    absorbed = absorbed or b""
    # Bytes per frame at 16-bit mono; the silence has to be a whole frame.
    frame = 2
    fill = max(0.0, shift - len(kept) / (SAMPLE_RATE * frame))
    silence = b"\0" * int(fill * SAMPLE_RATE) * frame
    tmp = keep_track.with_name(keep_track.name + ".tmp")
    with contextlib.suppress(Exception):
        wav = open_wav(tmp)
        try:
            wav.writeframes(kept)
            wav.writeframes(silence)
            wav.writeframes(absorbed)
        finally:
            wav.close()
        tmp.replace(keep_track)


def _join_notes(keep: Path, absorb: Path) -> None:
    """Your own typing from the later half is kept under the earlier's file."""
    add = absorb / NOTES_FILENAME
    if not add.is_file():
        return
    extra = add.read_text(errors="replace")
    if not extra.strip():
        return
    dest = keep / NOTES_FILENAME
    if dest.is_file():
        prior = dest.read_text(errors="replace")
        if prior and not prior.endswith("\n"):
            prior += "\n"
        if extra and not extra.endswith("\n"):
            extra += "\n"
        dest.write_text(prior + "\n" + extra if prior else extra)
    else:
        dest.write_text(extra)


def moved_to_trash(result: MergeResult) -> bool:
    """Whether the absorbed folder is out of the archive."""
    return result.trash in {"trashed", "deleted"}
