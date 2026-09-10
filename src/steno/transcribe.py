"""Deepgram streaming and transcript-level helpers."""
from __future__ import annotations

import asyncio
import json
import os
import wave
from dataclasses import dataclass
from pathlib import Path

import websockets

from .audio import CHUNK_BYTES, SAMPLE_RATE, spawn_parec, stop_parec

# Deepgram closes an idle socket after ~10s of silence, and a meeting where
# nobody speaks for ten seconds is a normal meeting.
KEEPALIVE_S = 8.0

DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    f"?model=nova-3&encoding=linear16&sample_rate={SAMPLE_RATE}&channels=1"
    "&smart_format=true&interim_results=true&endpointing=300"
)

@dataclass
class Utterance:
    speaker: str  # "you" or "them"
    text: str
    final: bool
    # Seconds into this speaker's WAV where the words start. Deepgram counts the
    # audio it has been sent, and the WAV is fed the same chunks, so this stays
    # true to the file even if the socket drops and reconnects mid-meeting.
    offset: float | None = None


async def stream_speaker(
    speaker: str,
    source: str,
    queue: asyncio.Queue[Utterance],
    wav: wave.Wave_write,
) -> None:
    """Spawn parec on `source`, tee PCM into `wav` and Deepgram, push results to queue."""
    api_key = os.environ["DEEPGRAM_API_KEY"]
    parec = await spawn_parec(source)
    # A reconnect starts Deepgram's clock at zero again, but the WAV keeps
    # growing; everything it reports has to be measured from what is already
    # written or the second half of a meeting seeks to the first half.
    base = _wav_seconds(wav)

    try:
        async with websockets.connect(
            DEEPGRAM_URL,
            additional_headers={"Authorization": f"Token {api_key}"},
            max_size=None,
        ) as ws:

            async def feed() -> None:
                assert parec.stdout is not None
                while True:
                    chunk = await parec.stdout.read(CHUNK_BYTES)
                    if not chunk:
                        return
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
                    ))
            finally:
                feeder.cancel()
                pinger.cancel()
    finally:
        await stop_parec(parec)


def _wav_seconds(wav: wave.Wave_write) -> float:
    """How much audio is already in the file."""
    try:
        return wav.tell() / SAMPLE_RATE
    except (OSError, AttributeError, ValueError):
        return 0.0


PRERECORDED_URL = "https://api.deepgram.com/v1/listen"


async def transcribe_file(
    path: Path, started_at: float, speaker: str, timeout_s: float = 600.0
) -> list[dict]:
    """Batch-transcribe a finished WAV into transcript records.

    Used for the microphone side, which the daemon records but does not stream:
    the prerecorded API is cheaper per minute than streaming and more accurate,
    since it sees the whole file at once. `ts` is anchored to `started_at` using
    each utterance's *end*, matching the live path — where a line's timestamp is
    when Deepgram finalized it, not when the speaker began — while `offset`
    keeps the exact position in the file, for playback.
    """
    import httpx

    api_key = os.environ["DEEPGRAM_API_KEY"]
    params = {
        "model": "nova-3",
        "smart_format": "true",
        "utterances": "true",
        "punctuate": "true",
    }
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
        records.append({
            "ts": started_at + float(u.get("end", 0.0)),
            "speaker": speaker,
            "text": text,
            # Exact, because the whole file was timed at once.
            "offset": float(u.get("start", 0.0)),
        })
    return records
