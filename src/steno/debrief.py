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
import signal
import subprocess
import time
import wave
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TextIO

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


@contextlib.contextmanager
def stop_when_told(stop: asyncio.Event, stream: TextIO | None) -> Iterator[bool]:
    """Set `stop` on Enter (or end of input) on `stream`, or on SIGTERM.

    The running loop watches the descriptor; no thread reads it. A thread
    blocked in readline outlives the recording it was meant to stop: on a
    timeout or Ctrl+C, asyncio.run waits minutes for it to finish, and
    the process then waits for an Enter nobody is going to press.

    Yields whether Enter can stop the recording. It cannot when `stream`
    is not something the loop can watch (a regular file, /dev/null, no
    stdin at all); then the time limit, Ctrl+C or SIGTERM does.
    """
    loop = asyncio.get_running_loop()
    watched = _watch_for_enter(loop, stream, stop.set)
    terms = _on_sigterm(loop, stop.set)
    try:
        yield watched is not None
    finally:
        if watched is not None:
            loop.remove_reader(watched)
        if terms:
            loop.remove_signal_handler(signal.SIGTERM)


def _watch_for_enter(
    loop: asyncio.AbstractEventLoop, stream: TextIO | None, callback: Callable[[], None]
) -> int | None:
    """Call `callback` once a line, or the end of input, arrives on `stream`.

    Returns the descriptor the loop now watches, or None when it cannot
    watch one: epoll refuses regular files and /dev/null.
    """
    try:
        fd = stream.fileno() if stream is not None else -1
    except (OSError, ValueError):  # io.UnsupportedOperation is both
        return None
    if fd < 0:
        return None

    def readable() -> None:
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data or b"\n" in data:
            loop.remove_reader(fd)
            callback()

    try:
        loop.add_reader(fd, readable)
    except (OSError, ValueError, NotImplementedError):
        return None
    return fd


def _on_sigterm(
    loop: asyncio.AbstractEventLoop, callback: Callable[[], None]
) -> bool:
    """Call `callback` on SIGTERM while recording. False where it cannot."""
    try:
        loop.add_signal_handler(signal.SIGTERM, callback)
    except (NotImplementedError, RuntimeError, ValueError):
        # Not the main thread, or a loop without signal support.
        return False
    return True


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
