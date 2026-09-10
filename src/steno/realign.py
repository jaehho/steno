"""Re-time an old meeting against its own audio.

Sessions recorded before the tool kept per-line offsets can only be placed in
the audio by inference: time zero from the file's mtime, and the length of each
line guessed from its word count. That lands within a second or two, which is
enough to find a passage and not enough to trust.

This runs both WAVs through the prerecorded API, which times every utterance
against the file it came from, and rewrites the transcript with those numbers.
It costs one transcription pass per track, so it asks before spending anything.

It is also a quality upgrade for the far side: the live stream was transcribed
in fragments as it arrived, while this sees the whole conversation at once.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .playback import TRACKS, audio_duration, recorded_offset, track_path
from .session import write_transcripts
from .summarize import load_records, transcript_path
from .transcribe import transcribe_file

BACKUP_SUFFIX = ".before-realign.jsonl"

# Deepgram nova-3 prerecorded, USD per minute of audio. Only ever used to warn
# someone before they spend money; it is not billing.
COST_PER_MINUTE = 0.0043


@dataclass(frozen=True)
class Plan:
    """What realigning a session would involve, before committing to it."""

    session_dir: Path
    minutes: float
    already_aligned: bool

    @property
    def cost(self) -> float:
        return self.minutes * COST_PER_MINUTE

    @property
    def name(self) -> str:
        return self.session_dir.name


def is_aligned(session_dir: Path) -> bool:
    """Does every line already know where it is in the audio?"""
    records = load_records(session_dir)
    if not records:
        return False
    return all(recorded_offset(r) is not None for r in records)


def has_audio(session_dir: Path) -> bool:
    return any((session_dir / name).is_file() for name in TRACKS.values())


def plan(session_dir: Path) -> Plan:
    minutes = sum(
        audio_duration(session_dir / name) for name in TRACKS.values()
    ) / 60.0
    return Plan(session_dir, minutes, is_aligned(session_dir))


def format_plan(plans: list[Plan]) -> str:
    total = sum(p.minutes for p in plans)
    lines = [
        f"  {p.name}  {p.minutes:6.1f} min  ~${p.cost:.2f}"
        + ("  (already aligned)" if p.already_aligned else "")
        for p in plans
    ]
    lines.append(f"  {'total':<15} {total:6.1f} min  ~${total * COST_PER_MINUTE:.2f}")
    return "\n".join(lines)


async def realign_session(session_dir: Path, started_at: float | None = None) -> int:
    """Rewrite one session's transcript with exact offsets. Returns line count.

    The previous transcript is kept beside the new one. This replaces words the
    user may have read before, and a transcription that cannot be compared with
    what it replaced is a transcription that has to be taken on faith.
    """
    if not has_audio(session_dir):
        raise FileNotFoundError(f"no audio in {session_dir}")

    anchor = started_at if started_at is not None else _anchor(session_dir)
    records: list[dict] = []
    for speaker in TRACKS:
        # The echo-cancelled copy when there is one: transcribing a microphone
        # track that still has the far side bleeding through it is how a
        # conversation ends up attributed to whoever was louder.
        path = track_path(session_dir, speaker)
        if not path.is_file():
            continue
        records.extend(await transcribe_file(path, anchor, speaker))

    if not records:
        raise ValueError(f"nothing transcribed from {session_dir}")

    records.sort(key=lambda r: r.get("offset", 0.0))
    _back_up(session_dir)
    write_transcripts(session_dir, records)
    _stamp(session_dir, anchor, len(records))
    return len(records)


def _anchor(session_dir: Path) -> float:
    """Wall-clock time for offset zero.

    Kept only so `ts` stays meaningful for ordering and for the summary's
    relative stamps; playback reads `offset`, which is what actually matters
    now.
    """
    from .playback import session_start

    return session_start(session_dir, load_records(session_dir))


def _back_up(session_dir: Path) -> Path | None:
    source = transcript_path(session_dir)
    if not source.is_file():
        return None
    backup = source.with_name(source.stem + BACKUP_SUFFIX)
    if backup.exists():  # an earlier realign already kept the original
        return backup
    try:
        shutil.copy2(source, backup)
    except OSError:
        return None
    return backup


def _stamp(session_dir: Path, anchor: float, lines: int) -> None:
    meta_path = session_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta.update({
        "started_at": anchor,
        "realigned_at": time.time(),
        "lines": lines,
    })
    # It is no longer an inference: every line now carries a measured offset.
    meta.pop("started_at_derived", None)
    try:
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    except OSError:
        pass
