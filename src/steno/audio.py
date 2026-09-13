"""Pulseaudio/pipewire-pulse capture helpers."""
from __future__ import annotations

import asyncio
import wave
from pathlib import Path

SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # 100ms @ 16kHz s16le mono

# Tags our own capture streams so meeting detection never sees itself.
CLIENT_NAME = "steno"


async def spawn_parec(source: str) -> asyncio.subprocess.Process:
    """parec on `source`, emitting raw s16le mono PCM on stdout."""
    return await asyncio.create_subprocess_exec(
        "parec",
        f"--device={source}",
        f"--client-name={CLIENT_NAME}",
        "--format=s16le",
        f"--rate={SAMPLE_RATE}",
        "--channels=1",
        "--raw",
        "--latency-msec=20",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def stop_parec(proc: asyncio.subprocess.Process) -> None:
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except TimeoutError:
        proc.kill()


async def record_only(source: str, wav: wave.Wave_write) -> None:
    """Capture `source` straight to `wav` with no transcription."""
    proc = await spawn_parec(source)
    try:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(CHUNK_BYTES)
            if not chunk:
                return
            wav.writeframes(chunk)
    finally:
        await stop_parec(proc)


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


async def capture_mic() -> str:
    """The source the near side is recorded from: the default microphone.

    With the echo canceller installed (dotfiles, `audio` package), WirePlumber
    routes a recording of the microphone through it, so the far side coming out
    of the speakers has already been subtracted and this track is only you.
    """
    return await default_mic()


async def default_sink_monitor() -> str:
    sink = await _pactl("get-default-sink")
    return f"{sink}.monitor"


def open_wav(path: Path) -> wave.Wave_write:
    w = wave.open(str(path), "wb")  # noqa: SIM115 — caller owns the handle
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SAMPLE_RATE)
    return w
