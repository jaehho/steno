"""Turning a transcript line back into a position in the recording.

Nothing here touches audio; it is the arithmetic that click-to-play needs, kept
out of the GUI so it can be tested without a sound card.

The subtlety this file exists for: a record's timestamp is when the line was
*finalized*, not when the speaker began. Deepgram emits a final after its
endpointing window closes, and the batch pass anchors each utterance to its
`end`. Seeking straight to that timestamp lands you after the sentence you
wanted to hear. So a line's play position is its finalize time minus an estimate
of how long it took to say.
"""
from __future__ import annotations

import contextlib
import json
import wave
from pathlib import Path

# Conversational speech runs about 150 words a minute; 2.5/s is a fair middle.
WORDS_PER_SECOND = 2.5
MIN_UTTERANCE_S = 1.0
MAX_UTTERANCE_S = 30.0
# A beat of lead-in, so playback starts on the breath before the first word.
LEAD_S = 0.4

TRACKS = {"you": "you.wav", "them": "them.wav"}
# An echo-cancelled copy of a track sits beside the original under this suffix.
# The original is never replaced: cancellation is lossy and best-effort, and a
# recording is the one thing here that cannot be made again.
CLEAN_SUFFIX = ".clean.wav"


def track_for(speaker: str) -> str:
    """Which file holds this speaker.

    The two sides are recorded separately, so that each can be transcribed
    knowing who it is. They are played back together: a meeting was one
    conversation, and hearing half of it is not what anyone came for.
    """
    return TRACKS.get(speaker, TRACKS["them"])


def clean_path(track: Path) -> Path:
    return track.with_name(track.name.removesuffix(".wav") + CLEAN_SUFFIX)


def track_path(session_dir: Path, speaker: str) -> Path:
    """The best copy of this speaker's audio.

    A cleaned track wins when there is one. It is the same recording with the
    other side's bleed taken out of it, so it is what you want to hear and what
    you want transcribed; the original stays on disk either way.
    """
    original = session_dir / track_for(speaker)
    cleaned = clean_path(original)
    return cleaned if cleaned.is_file() else original


def session_tracks(session_dir: Path) -> dict[str, Path]:
    """Every side of this meeting that is still on disk, best copy first."""
    found = {}
    for speaker in TRACKS:
        path = track_path(session_dir, speaker)
        if path.is_file():
            found[speaker] = path
    return found


def spoken_duration(text: str) -> float:
    words = len(text.split())
    return min(MAX_UTTERANCE_S, max(MIN_UTTERANCE_S, words / WORDS_PER_SECOND))


def audio_duration(path: Path) -> float:
    """Seconds of audio in a WAV, from its header alone."""
    try:
        with wave.open(str(path)) as handle:
            rate = handle.getframerate()
            return handle.getnframes() / rate if rate else 0.0
    except (OSError, wave.Error):
        return 0.0


def derived_start(session_dir: Path) -> float:
    """Infer time zero from the recording itself.

    The last frame written is the file's mtime, so `mtime - duration` is when
    capture began — accurate to a frame, and available for sessions recorded
    before the tool wrote `started_at` at all.
    """
    for name in TRACKS.values():
        path = session_dir / name
        duration = audio_duration(path)
        if duration <= 0:
            continue
        try:
            return path.stat().st_mtime - duration
        except OSError:
            continue
    return 0.0


def session_start(session_dir: Path, records: list[dict] | None = None) -> float:
    """When recording began, which is time zero in both WAV files.

    `meta.json` knows exactly, when it was written by a version that recorded
    it. Otherwise the audio itself is asked, because the remaining fallback —
    the first transcript line — is time zero only for a meeting that began
    mid-sentence. Every second of quiet before the first word would shift the
    whole recording, and on real meetings that was minutes.
    """
    try:
        meta = json.loads((session_dir / "meta.json").read_text())
        started = float(meta.get("started_at", 0.0))
        if started > 0:
            return started
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    derived = derived_start(session_dir)
    if derived > 0:
        return derived
    if records:
        return float(records[0].get("ts", 0.0))
    return 0.0


def repair_started_at(session_dir: Path, records: list[dict] | None = None) -> float:
    """Persist a derived start time, so it is computed once and stays put."""
    started = session_start(session_dir, records)
    meta_path = session_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        meta = {}
    if not isinstance(meta, dict) or float(meta.get("started_at", 0.0) or 0.0) > 0:
        return started
    if started > 0:
        meta["started_at"] = started
        # Say where it came from: it is an inference, not something observed.
        meta["started_at_derived"] = True
        with contextlib.suppress(OSError):
            meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return started


def recorded_offset(record: dict) -> float | None:
    """The offset the transcriber itself reported, if this record has one.

    Deepgram counts from the audio it has been sent, so this is exact and stays
    exact across a dropped stream — the wav and the socket are fed the same
    chunks. Records written before the tool kept this field have to be
    estimated instead.
    """
    value = record.get("offset")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def seek_offset(record: dict, started_at: float) -> float:
    """Seconds into the speaker's track where this line starts being said."""
    exact = recorded_offset(record)
    if exact is not None:
        return max(0.0, exact - LEAD_S)
    ts = float(record.get("ts", 0.0))
    end = ts - started_at
    start = end - spoken_duration(str(record.get("text", ""))) - LEAD_S
    return max(0.0, start)


def display_offset(record: dict, started_at: float) -> int:
    """Whole seconds from the start of the meeting, for the `mm:ss` gutter.

    The same number playback uses, so the gutter and the audio can never
    disagree about where a line is.
    """
    exact = recorded_offset(record)
    if exact is not None:
        return max(0, int(exact))
    return max(0, int(float(record.get("ts", 0.0)) - started_at))


def format_offset(seconds: float) -> str:
    total = max(0, int(seconds))
    if total >= 3600:
        return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return f"{total // 60:02d}:{total % 60:02d}"


def index_at(offsets: list[float], position: float) -> int:
    """Which line is being spoken at `position`. -1 before the first one.

    The latest line that has started, scanning the whole list rather than
    stopping at the first one that has not. A transcript is written in the
    order lines were finalized and the two sides are interleaved afterwards, so
    a pair that arrived out of order would otherwise stop playback tracking
    dead at that point. The lists are hundreds of entries and this runs a few
    times a second.
    """
    found = -1
    best = float("-inf")
    for i, offset in enumerate(offsets):
        if best <= offset <= position:
            found, best = i, offset
    return found
