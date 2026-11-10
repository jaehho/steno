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

With STENO_ASR=local the pass runs on this machine instead (localasr.py):
nothing is uploaded and nothing is spent. A debrief is re-transcribed here
unless STENO_ASR says otherwise, as it was transcribed here in the first
place. Whisper writes spoken alphanumeric names joined ("X10", "X10a")
where Deepgram splits them ("X 10 a"); neither always keeps the letter.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .kept import set_kept
from .playback import TRACKS, audio_duration, recorded_offset, session_start, track_path
from .room import is_room
from .session import write_transcripts
from .speakers import FAR_VOICE, MIC_VOICE, NAMES_FILENAME
from .summarize import DEBRIEF, load_meta, load_records, transcript_path
from .transcribe import asr_backend, asr_label, transcribe_file

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
    # "deepgram", or "local": this machine, which costs nothing.
    backend: str = "deepgram"

    @property
    def cost(self) -> float:
        if self.backend == "local":
            return 0.0
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


def backend_for(
    session_dir: Path, environ: Mapping[str, str] = os.environ
) -> str:
    """The engine that re-transcribes `session_dir`.

    STENO_ASR when it names one. Otherwise Deepgram for a call, and this
    machine for a debrief: its audio was transcribed here and never
    uploaded, and a retry should not be what uploads it. Raises
    ValueError for a STENO_ASR it does not know.
    """
    local_first = load_meta(session_dir).get("kind") == DEBRIEF
    return asr_backend(environ, default="local" if local_first else "deepgram")


def plan(session_dir: Path) -> Plan:
    """What re-timing `session_dir` would take. ValueError: bad STENO_ASR."""
    minutes = sum(
        audio_duration(session_dir / name) for name in TRACKS.values()
    ) / 60.0
    return Plan(
        session_dir, minutes, is_aligned(session_dir), backend_for(session_dir)
    )


def format_plan(plans: list[Plan]) -> str:
    total = sum(p.minutes for p in plans)
    lines = [
        f"  {p.name}  {p.minutes:6.1f} min  ~${p.cost:.2f}"
        + ("  (already aligned)" if p.already_aligned else "")
        for p in plans
    ]
    lines.append(f"  {'total':<15} {total:6.1f} min  ~${sum(p.cost for p in plans):.2f}")
    return "\n".join(lines)


async def realign_session(
    session_dir: Path,
    started_at: float | None = None,
    backend: str | None = None,
) -> int:
    """Rewrite one session's transcript with exact offsets. Returns line count.

    The previous transcript is kept beside the new one. This replaces words the
    user may have read before, and a transcription that cannot be compared with
    what it replaced is a transcription that has to be taken on faith.
    `backend` None is `backend_for(session_dir)`.
    """
    if not has_audio(session_dir):
        msg = f"no audio in {session_dir}"
        raise FileNotFoundError(msg)
    chosen = backend or backend_for(session_dir)

    anchor = started_at if started_at is not None else _anchor(session_dir)
    # Voices are asked for on both sides: they cost nothing on a file, and
    # without them a re-timed meeting loses every name it had learned.
    room = is_room(load_meta(session_dir), load_records(session_dir))
    voices_as = {"them": FAR_VOICE, "you": MIC_VOICE if room else ""}
    records: list[dict] = []
    for speaker in TRACKS:
        # The echo-cancelled copy when there is one: transcribing a microphone
        # track that still has the far side bleeding through it is how a
        # conversation ends up attributed to whoever was louder.
        path = track_path(session_dir, speaker)
        if not path.is_file():
            continue
        records.extend(await transcribe_file(
            path, anchor, speaker,
            voices_as=voices_as.get(speaker, ""), backend=chosen,
        ))

    if not records:
        msg = f"nothing transcribed from {session_dir}"
        raise ValueError(msg)

    records.sort(key=lambda r: r.get("offset", 0.0))
    _back_up(session_dir)
    _set_names_aside(session_dir)
    write_transcripts(session_dir, records)
    engine = asr_label(chosen, os.environ)
    _stamp(session_dir, anchor, len(records), engine)
    return len(records)


def _set_names_aside(session_dir: Path) -> Path | None:
    """Move speakers.json out of the way: this pass numbered the voices
    afresh, and an old name on a renumbered voice is a wrong name. The
    next summary learns them again; the old map stays beside it."""
    source = session_dir / NAMES_FILENAME
    if not source.is_file():
        return None
    aside = source.with_name("speakers.before-realign.json")
    with contextlib.suppress(OSError):
        source.replace(aside)
        return aside
    return None


def _anchor(session_dir: Path) -> float:
    """Wall-clock time for offset zero.

    Kept only so `ts` stays meaningful for ordering and for the summary's
    relative stamps; playback reads `offset`, which is what actually matters
    now.
    """
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


def _stamp(session_dir: Path, anchor: float, lines: int, engine: str) -> None:
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
        # Which engine wrote these words, since either may have.
        "asr": engine,
    })
    # It is no longer an inference: every line now carries a measured offset.
    meta.pop("started_at_derived", None)
    # Both tracks were just transcribed: your side is no longer missing.
    meta.pop("mic_pending", None)
    with contextlib.suppress(OSError):
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
        set_kept(session_dir, kept=False)
