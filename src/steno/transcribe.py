"""Deepgram streaming and transcript-level helpers."""
from __future__ import annotations

import asyncio
import json
import os
import wave
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx
import websockets

from .audio import CHUNK_BYTES, SAMPLE_RATE, spawn_parec, stop_parec

# Deepgram closes an idle socket after ~10s of silence, and a meeting where
# nobody speaks for ten seconds is a normal meeting.
KEEPALIVE_S = 8.0

# Deepgram's keyterm limit is generous; cap so a verbose brief cannot make the
# request URL enormous. Plain terms only — a weight suffix is treated literally.
MAX_KEYTERMS = 30


def listen_url(keyterms: list[str] | None = None) -> str:
    """The streaming endpoint, with the brief's proper nouns boosted.

    `keyterm` is repeatable and takes plain terms; multi-word names go through
    as one term, because splitting them is how "Jane Doe" arrives as "chain
    dough". Plain terms only — Deepgram silently treats `term:0.15` as literal.
    """
    query = {
        "model": "nova-3",
        "encoding": "linear16",
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "smart_format": "true",
        "interim_results": "true",
        "endpointing": 300,
    }
    pairs = list(query.items())
    # Far-side diarization: who among *them* said it. The near side is one
    # person by construction and is not run through this socket.
    pairs.append(("diarize_model", "latest"))
    pairs.extend(
        ("keyterm", term.strip())
        for term in (keyterms or [])[:MAX_KEYTERMS]
        if term.strip()
    )
    return "wss://api.deepgram.com/v1/listen?" + urlencode(pairs)


@dataclass
class Utterance:
    speaker: str  # "you" or "them"
    text: str
    final: bool
    # Seconds into this speaker's WAV where the words start. Deepgram counts the
    # audio it has been sent, and the WAV is fed the same chunks, so this stays
    # true to the file even if the socket drops and reconnects mid-meeting.
    offset: float | None = None
    # False for a display-only stream: the mic is batch-transcribed after the
    # meeting, and that pass is the transcript. These lines exist so someone
    # watching the window sees both sides as they talk, and must not be written
    # beside the batch or the meeting ends up with every sentence twice.
    persist: bool = True
    # Diarized label among the far side ("speaker_0"), or "" when unknown.
    who: str = ""


async def stream_speaker(  # noqa: PLR0913, PLR0917 - one capture pipeline config
    speaker: str,
    source: str,
    queue: asyncio.Queue[Utterance],
    wav: wave.Wave_write | None = None,
    keyterms: list[str] | None = None,
    persist: bool = True,
) -> None:
    """Spawn parec on `source`, tee PCM into `wav` (when given) and Deepgram.

    With no `wav` this is display-only: the caller owns the file already and
    only wants `Heard` events out of the socket.
    """
    api_key = os.environ["DEEPGRAM_API_KEY"]
    parec = await spawn_parec(source)
    # A reconnect starts Deepgram's clock at zero again, but the WAV keeps
    # growing; everything it reports has to be measured from what is already
    # written or the second half of a meeting seeks to the first half.
    base = _wav_seconds(wav) if wav is not None else 0.0

    try:
        async with websockets.connect(
            listen_url(keyterms),
            additional_headers={"Authorization": f"Token {api_key}"},
            max_size=None,
        ) as ws:

            async def feed() -> None:
                assert parec.stdout is not None
                while True:
                    chunk = await parec.stdout.read(CHUNK_BYTES)
                    if not chunk:
                        return
                    if wav is not None:
                        wav.writeframes(chunk)
                    await ws.send(chunk)

            async def keepalive() -> None:
                while True:
                    await asyncio.sleep(KEEPALIVE_S)
                    await ws.send(json.dumps({"type": "KeepAlive"}))

            feeder = asyncio.create_task(feed())
            pinger = asyncio.create_task(keepalive())
            try:
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") != "Results":
                        continue
                    alt = data["channel"]["alternatives"][0]
                    text = alt.get("transcript", "").strip()
                    if not text:
                        continue
                    start = data.get("start")
                    await queue.put(Utterance(
                        speaker=speaker,
                        text=text,
                        final=bool(data.get("is_final")),
                        offset=(base + float(start)) if start is not None else None,
                        persist=persist,
                        who=_majority_speaker(alt.get("words") or []),
                    ))
            finally:
                feeder.cancel()
                pinger.cancel()
    finally:
        await stop_parec(parec)


def _majority_speaker(words: list[dict]) -> str:
    """The diarized voice that owns most of the words in this fragment.

    A fragment can straddle a turn; attributing it to whoever said most of it
    is better than dropping the turn or inventing a split the model did not
    give us. `speaker_0` style, matching Deepgram's numbering from 0.
    """
    votes: dict[int, int] = {}
    for word in words:
        spk = word.get("speaker")
        if isinstance(spk, int):
            votes[spk] = votes.get(spk, 0) + 1
    if not votes:
        return ""
    return f"speaker_{max(votes, key=votes.get)}"


def _wav_seconds(wav: wave.Wave_write | None) -> float:
    """How much audio is already in the file."""
    if wav is None:
        return 0.0
    try:
        return wav.tell() / SAMPLE_RATE
    except (OSError, AttributeError, ValueError):
        return 0.0


PRERECORDED_URL = "https://api.deepgram.com/v1/listen"


async def transcribe_file(
    path: Path,
    started_at: float,
    speaker: str,
    timeout_s: float = 600.0,
    keyterms: list[str] | None = None,
) -> list[dict]:
    """Batch-transcribe a finished WAV into transcript records.

    Used for the microphone side, which the daemon records but does not stream:
    the prerecorded API is cheaper per minute than streaming and more accurate,
    since it sees the whole file at once. `ts` is anchored to `started_at` using
    each utterance's *end*, matching the live path — where a line's timestamp is
    when Deepgram finalized it, not when the speaker began — while `offset`
    keeps the exact position in the file, for playback.
    """
    api_key = os.environ["DEEPGRAM_API_KEY"]
    params: list[tuple[str, str]] = [
        ("model", "nova-3"),
        ("smart_format", "true"),
        ("utterances", "true"),
        ("punctuate", "true"),
    ]
    params.extend(
        ("keyterm", term.strip())
        for term in (keyterms or [])[:MAX_KEYTERMS]
        if term.strip()
    )
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(
            PRERECORDED_URL,
            params=params,
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "audio/wav",
            },
            content=path.read_bytes(),
        )
        resp.raise_for_status()
        data = resp.json()

    utterances = data.get("results", {}).get("utterances", []) or []
    records = []
    for u in utterances:
        text = (u.get("transcript") or "").strip()
        if not text:
            continue
        row = {
            "ts": started_at + float(u.get("end", 0.0)),
            "speaker": speaker,
            "text": text,
            # Exact, because the whole file was timed at once.
            "offset": float(u.get("start", 0.0)),
        }
        # Prerecorded diarization puts the number on the utterance itself.
        spk = u.get("speaker")
        if isinstance(spk, int):
            row["who"] = f"speaker_{spk}"
        records.append(row)
    return records
