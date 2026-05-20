"""Deepgram streaming and transcript-level helpers."""
from __future__ import annotations

import asyncio
import json
import os
import re
import wave
from dataclasses import dataclass

import websockets

from .audio import CHUNK_BYTES, SAMPLE_RATE

DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    f"?model=nova-3&encoding=linear16&sample_rate={SAMPLE_RATE}&channels=1"
    "&smart_format=true&interim_results=true&endpointing=300"
)

AUTO_ASK_COOLDOWN_S = 20.0
AUTO_ASK_MIN_WORDS = 4

_INTERROGATIVE_RE = re.compile(
    r"^\s*(what|how|why|when|where|who|which|"
    r"can|could|would|should|will|do|does|did|are|is|was|were|have|has|had|"
    r"tell me|any thoughts|thoughts on)\b",
    re.IGNORECASE,
)


def is_question(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if t.endswith("?"):
        return True
    if len(t.split()) < AUTO_ASK_MIN_WORDS:
        return False
    return bool(_INTERROGATIVE_RE.match(t))


@dataclass
class Utterance:
    speaker: str  # "you" or "them"
    text: str
    final: bool


async def stream_speaker(
    speaker: str,
    source: str,
    queue: asyncio.Queue[Utterance],
    wav: wave.Wave_write,
) -> None:
    """Spawn parec on `source`, tee PCM into `wav` and Deepgram, push results to queue."""
    api_key = os.environ["DEEPGRAM_API_KEY"]
    parec = await asyncio.create_subprocess_exec(
        "parec",
        f"--device={source}",
        "--format=s16le",
        f"--rate={SAMPLE_RATE}",
        "--channels=1",
        "--raw",
        "--latency-msec=20",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

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

            feeder = asyncio.create_task(feed())
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
                    await queue.put(Utterance(
                        speaker=speaker,
                        text=text,
                        final=bool(data.get("is_final")),
                    ))
            finally:
                feeder.cancel()
    finally:
        parec.terminate()
        try:
            await asyncio.wait_for(parec.wait(), timeout=2)
        except asyncio.TimeoutError:
            parec.kill()
