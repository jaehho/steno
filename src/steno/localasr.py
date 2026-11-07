"""Batch transcription on this machine, with faster-whisper.

The prerecorded Deepgram pass uploads the whole WAV. This does the same
job here and returns the same rows, so the mic pass, `steno realign` and
a debrief can use either. Nothing it reads or writes leaves the machine:
the model comes from the Hugging Face cache and is never downloaded here
(`local_files_only`), so a missing model is an error, not a 1.6 GB
surprise on a phone hotspot.

On the CPU it runs int8 on four threads, four to ten times faster than
real time on this laptop, depending on what else is running. CUDA is faster again, but ctranslate2 wants the
CUDA 12 cuBLAS and cuDNN, and a system with only CUDA 13 has neither: the
`local-gpu` extra installs them into the venv, and `preload_cuda12` loads
them before the model so ctranslate2 finds them by name.

faster-whisper is an optional dependency (the `local` extra), imported
only when a local pass runs.
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import sys
import wave
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .audio import SAMPLE_RATE

log = logging.getLogger("steno.localasr")

MODEL_ENV = "STENO_LOCAL_MODEL"
DEVICE_ENV = "STENO_LOCAL_DEVICE"
LANGUAGE_ENV = "STENO_LOCAL_LANGUAGE"
THREADS_ENV = "STENO_LOCAL_THREADS"

# faster-whisper's alias for mobiuslabsgmbh/faster-whisper-large-v3-turbo.
DEFAULT_MODEL = "large-v3-turbo"
# Half the cores: a meeting is transcribed while the user goes on working.
DEFAULT_THREADS = 4
# Deepgram's nova-3 is English unless told otherwise; so is this. An
# empty STENO_LOCAL_LANGUAGE lets Whisper guess per file.
DEFAULT_LANGUAGE = "en"
DEVICES = ("auto", "cpu", "cuda")

# What the venv's nvidia-*-cu12 wheels hold, in load order: cuBLAS needs
# cuBLASLt beside it; cuDNN loads its own parts from its directory.
CUDA12_LIBRARIES = (
    ("cublas", "libcublasLt.so.12"),
    ("cublas", "libcublas.so.12"),
    ("cudnn", "libcudnn.so.9"),
)


class LocalAsrError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalConfig:
    """How a local pass runs: which model, where, in what language."""

    model: str = DEFAULT_MODEL
    device: str = "auto"
    language: str | None = DEFAULT_LANGUAGE
    threads: int = DEFAULT_THREADS

    @property
    def label(self) -> str:
        """What meta.json records as the engine behind the words."""
        return f"local:{self.model}"


def config_from_env(environ: Mapping[str, str]) -> LocalConfig:
    """The local pass as the environment sets it, defaults elsewhere."""
    device = environ.get(DEVICE_ENV, "").strip() or "auto"
    if device not in DEVICES:
        msg = f"{DEVICE_ENV}={device!r}; use one of {', '.join(DEVICES)}"
        raise ValueError(msg)
    language = environ.get(LANGUAGE_ENV, DEFAULT_LANGUAGE).strip() or None
    threads_text = environ.get(THREADS_ENV, "").strip()
    threads = int(threads_text) if threads_text else DEFAULT_THREADS
    return LocalConfig(
        model=environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL,
        device=device,
        language=language,
        threads=threads,
    )


class Segment(Protocol):
    start: float
    end: float
    text: str


def rows_from_segments(
    segments: Iterable[Segment], started_at: float, speaker: str
) -> list[dict]:
    """Transcript rows, the shape `transcribe_file` returns.

    `ts` is the segment's end on the wall clock, as the Deepgram paths
    stamp a line when it was finished; `offset` is where it starts in the
    file, for playback. Segments with no words are dropped.
    """
    rows = []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        rows.append({
            "ts": started_at + float(seg.end),
            "speaker": speaker,
            "text": text,
            "offset": float(seg.start),
        })
    return rows


def read_wav(path: Path):
    """A steno track as float32 samples in [-1, 1).

    Only the format steno records (16 kHz, mono, 16-bit): anything else
    is converted before it gets here, by the caller that imported it.
    """
    import numpy as np  # noqa: PLC0415 - the `local` extra brings it

    with wave.open(str(path), "rb") as wav:
        shape = (wav.getframerate(), wav.getnchannels(), wav.getsampwidth())
        if shape != (SAMPLE_RATE, 1, 2):
            msg = (f"{path.name} is {shape[0]} Hz, {shape[1]} channel(s), "
                   f"{8 * shape[2]}-bit; expected {SAMPLE_RATE} Hz mono 16-bit")
            raise ValueError(msg)
        pcm = wav.readframes(wav.getnframes())
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def _cuda12_dirs(search_path: Iterable[str]) -> dict[str, Path]:
    """`nvidia/<part>/lib` directories on `search_path`, by part."""
    found: dict[str, Path] = {}
    for entry in search_path:
        for part, _ in CUDA12_LIBRARIES:
            lib = Path(entry) / "nvidia" / part / "lib"
            if part not in found and lib.is_dir():
                found[part] = lib
    return found


def preload_cuda12(search_path: Iterable[str] | None = None) -> list[Path]:
    """Load the venv's CUDA 12 cuBLAS and cuDNN, if it has them.

    ctranslate2 opens `libcublas.so.12` by name when the first model goes
    to the GPU. A library already loaded under that name is the one it
    gets, and loading one by its full path needs no LD_LIBRARY_PATH,
    which a running process cannot change for itself. Returns what was
    loaded; empty when the wheels are not installed.
    """
    dirs = _cuda12_dirs(sys.path if search_path is None else search_path)
    loaded = []
    for part, name in CUDA12_LIBRARIES:
        lib = dirs.get(part)
        if lib is None or not (lib / name).is_file():
            continue
        try:
            ctypes.CDLL(str(lib / name), mode=ctypes.RTLD_GLOBAL)
        except OSError as e:
            log.warning("could not load %s: %s", lib / name, e)
            continue
        loaded.append(lib / name)
    return loaded


def cuda_usable() -> bool:
    """A GPU, and the CUDA 12 libraries ctranslate2 needs to use it."""
    import ctranslate2  # noqa: PLC0415 - the `local` extra brings it

    if ctranslate2.get_cuda_device_count() < 1:
        return False
    preload_cuda12()
    try:
        ctypes.CDLL("libcublas.so.12")
        ctypes.CDLL("libcudnn.so.9")
    except OSError:
        return False
    return True


def _load_model(config: LocalConfig, device: str):
    from faster_whisper import WhisperModel  # noqa: PLC0415 - optional extra
    from faster_whisper.utils import _MODELS  # noqa: PLC0415

    compute_type = "int8_float16" if device == "cuda" else "int8"
    try:
        return WhisperModel(
            config.model,
            device=device,
            compute_type=compute_type,
            cpu_threads=config.threads,
            local_files_only=True,
        )
    except FileNotFoundError as e:
        # huggingface_hub's LocalEntryNotFoundError: not in the cache, and
        # this never downloads.
        repo = _MODELS.get(config.model, config.model)
        msg = (f"{config.model} is not in the Hugging Face cache; download "
               f"it once: `uvx --from huggingface_hub hf download {repo}`")
        raise LocalAsrError(msg) from e


def _decode(model, audio, language: str | None) -> list:
    segments, _ = model.transcribe(
        audio,
        language=language,
        beam_size=5,
        vad_filter=True,
    )
    return list(segments)


def transcribe_wav(
    path: Path, started_at: float, speaker: str, config: LocalConfig
) -> list[dict]:
    """Transcribe one WAV here, blocking. Same rows as `transcribe_file`.

    `auto` uses the GPU when ctranslate2 can, and the CPU when it cannot
    or when the GPU pass fails (out of memory, a driver that went away):
    a slower transcript beats none.
    """
    audio = read_wav(path)
    if audio.size == 0:
        return []
    device = config.device
    if device == "auto":
        device = "cuda" if cuda_usable() else "cpu"
    elif device == "cuda":
        preload_cuda12()
    try:
        segments = _decode(_load_model(config, device), audio, config.language)
    except LocalAsrError:
        raise
    except RuntimeError as e:
        if config.device != "auto" or device == "cpu":
            raise
        log.warning("GPU pass failed (%s); doing it on the CPU", e)
        segments = _decode(_load_model(config, "cpu"), audio, config.language)
    return rows_from_segments(segments, started_at, speaker)


async def transcribe_file_local(
    path: Path,
    started_at: float,
    speaker: str,
    config: LocalConfig | None = None,
) -> list[dict]:
    """`transcribe_wav` off the event loop, configured from the environment."""
    chosen = config if config is not None else config_from_env(os.environ)
    return await asyncio.to_thread(
        transcribe_wav, path, started_at, speaker, chosen
    )
