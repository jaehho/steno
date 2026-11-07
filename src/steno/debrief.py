"""A debrief: you, alone, talking through a meeting you have just left.

Recording the room means recording everyone in it, which needs their
consent, a battery that outlasts the meeting, and someone to replay hours
of audio. A debrief records one voice for a few minutes at the door, so
it needs none of that. It is a session like any other (meta.json says
`kind: debrief`), with your side only, transcribed on this machine by
default and summarized with a prompt that knows the promises you report
from others are theirs.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import time
import wave
from pathlib import Path

from .audio import SAMPLE_RATE, capture_mic, open_wav, record_only
from .session import write_transcripts
from .summarize import DEBRIEF, META_FILENAME, update_meta
from .transcribe import asr_backend, asr_label, transcribe_file

TRACK = "you.wav"
# Long enough for any debrief; short enough that a forgotten one stops.
MAX_SECONDS = 15 * 60


def session_name(started_at: float) -> str:
    """A session directory's name, as the engine names a meeting's."""
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(started_at))


def debrief_meta(started_at: float) -> dict:
    """meta.json for a debrief before it is transcribed.

    `manual` because you started it; `mic_pending` until your lines are
    on disk, as for a call; `room: false` so that a room-mode build does
    not read a recording with no far side as a room to hold for consent.
    """
    return {
        "started_at": started_at,
        "app": DEBRIEF,
        "manual": True,
        "kind": DEBRIEF,
        "room": False,
        "mic_pending": True,
    }


def create_session(root: Path, started_at: float) -> Path:
    """A new, empty debrief session under `root`. Refuses to reuse one."""
    session_dir = root / session_name(started_at)
    session_dir.mkdir(parents=True, exist_ok=False)
    (session_dir / META_FILENAME).write_text(
        json.dumps(debrief_meta(started_at), indent=2) + "\n"
    )
    return session_dir


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate() or SAMPLE_RATE)


def convert_audio(source: Path, dest: Path) -> float:
    """Any audio ffmpeg reads (a phone's voice memo) as a steno track.

    Returns its length in seconds. Raises CalledProcessError when ffmpeg
    cannot read it.
    """
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16", str(dest)],
        check=True,
    )
    return wav_seconds(dest)


async def record(dest: Path, stop: asyncio.Event, max_s: float = MAX_SECONDS) -> str:
    """Record the default mic into `dest` until `stop` is set or `max_s`.

    Only the mic: there is no far side and nothing is streamed anywhere.
    The capture is tagged as steno's own, so meeting detection ignores
    it. Returns the source it recorded from.
    """
    source = await capture_mic()
    wav = open_wav(dest)
    task = asyncio.create_task(record_only(source, wav))
    try:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max_s)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        wav.close()
    return source


async def transcribe(
    session_dir: Path, started_at: float, backend: str | None = None
) -> int:
    """Transcribe a debrief's track and write its transcript. Line count.

    A debrief is transcribed on this machine unless STENO_ASR names
    another engine: it is your own voice, and there is no reason for it
    to leave. Clears `mic_pending` and records the engine, as the end of
    a call does.
    """
    chosen = backend or asr_backend(os.environ, default="local")
    rows = await transcribe_file(
        session_dir / TRACK, started_at, "you", backend=chosen
    )
    write_transcripts(session_dir, rows)
    update_meta(
        session_dir, mic_pending=None, asr=asr_label(chosen, os.environ)
    )
    return len(rows)
