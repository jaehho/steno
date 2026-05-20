"""Pulseaudio/pipewire-pulse capture helpers."""
from __future__ import annotations

import asyncio
import wave
from pathlib import Path

SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # 100ms @ 16kHz s16le mono


async def _pactl(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "pactl", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode().strip()


async def default_mic() -> str:
    return await _pactl("get-default-source")


async def default_sink_monitor() -> str:
    sink = await _pactl("get-default-sink")
    return f"{sink}.monitor"


def open_wav(path: Path) -> wave.Wave_write:
    w = wave.open(str(path), "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SAMPLE_RATE)
    return w
