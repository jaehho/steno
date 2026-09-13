"""The engine: detect a meeting, record it, transcribe it, reduce it to notes.

Owned in-process by the application, but with no dependency on it — this module
imports no GUI toolkit and emits `events` rather than drawing anything, so the
whole pipeline runs and is tested headlessly.

The microphone is recorded but never streamed live. Reading back what *they*
said is the live use case; the near side is cheaper and more accurate to
transcribe in one pass once the file is closed.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from . import DEFAULT_MODEL, claude, config_dir, data_dir
from .audio import capture_mic, default_sink_monitor, open_wav, record_only
from .brief import BRIEF_FILENAME, load_brief
from .detect import DEFAULT_ALLOW, END_GRACE_S, START_HOLD_S, watch_meetings
from .events import (
    AdviceDelta,
    AdviceDone,
    EngineError,
    Event,
    Finalizing,
    Heard,
    MeetingEnded,
    MeetingStarted,
    StatusChanged,
    SummaryReady,
)
from .summarize import META_FILENAME, load_records, summarize_session
from .transcribe import Utterance, stream_speaker, transcribe_file

PAUSE_FILENAME = "paused-until"

ADVISOR_PROMPT = (
    "You are a meeting assistant, consulted mid-conversation by the person "
    "wearing the mic ('you'); 'them' is everyone else. You are being asked "
    "*now*, while they are in the room, so answer the thing most likely to be "
    "useful in the next thirty seconds: if a question is on the table, answer "
    "it; if they seem stuck, give the nudge; otherwise surface the open threads "
    "and anything they agreed to. Lead with the answer — no preamble, no "
    "restating the question. Bullets are fine. Under 80 words."
)

REVIEW_PROMPT = (
    "You are answering a question about a meeting that has already ended, "
    "asked by the person who wore the mic ('you'); 'them' is everyone else. "
    "Answer from the transcript: lead with the answer, quote or paraphrase what "
    "was actually said when that settles it, and say plainly when the meeting "
    "did not cover it. Bullets are fine. Under 150 words."
)


def pause_path() -> Path:
    return config_dir() / PAUSE_FILENAME


def paused_until() -> float | None:
    """Unix time the pause expires, or None if not paused."""
    try:
        until = float(pause_path().read_text().strip())
    except (OSError, ValueError):
        return None
    if until <= time.time():
        with contextlib.suppress(OSError):
            pause_path().unlink()
        return None
    return until


def set_pause(seconds: float | None) -> None:
    """Pause automatic starts for `seconds`, or clear the pause with None."""
    if seconds is None:
        with contextlib.suppress(OSError):
            pause_path().unlink()
        return
    pause_path().parent.mkdir(parents=True, exist_ok=True)
    pause_path().write_text(f"{time.time() + seconds}\n")


@dataclass
class Recording:
    """One in-flight meeting: its directory, open files, and capture tasks."""

    session_dir: Path
    started_at: float
    app: str
    manual: bool = False
    tasks: list[asyncio.Task] = field(default_factory=list)
    handles: list = field(default_factory=list)
    transcript_jsonl: IO[str] | None = None

    def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for h in self.handles:
            with contextlib.suppress(Exception):
                h.close()


class Engine:
    """Watches for meetings and runs one at a time.

    `emit` is called from the engine's own event loop thread; a UI is expected
    to marshal onto its own thread rather than touching widgets here.
    """

    def __init__(
        self,
        emit: Callable[[Event], None],
        allow: tuple[str, ...] | None = DEFAULT_ALLOW,
        deny: tuple[str, ...] = (),
        brief_path: Path | None = None,
        model: str | None = None,
        summarize: bool = True,
        root: Path | None = None,
        start_hold_s: float = START_HOLD_S,
        end_grace_s: float = END_GRACE_S,
    ) -> None:
        self.emit = emit
        self.allow = allow
        self.deny = deny
        self.model = model or os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)
        self.summarize = summarize
        self.root = root or (data_dir() / "sessions")
        self.brief_path = brief_path or (config_dir() / BRIEF_FILENAME)
        self.start_hold_s = start_hold_s
        self.end_grace_s = end_grace_s

        self.current: Recording | None = None
        self.transcript: list[dict] = []
        self.queue: asyncio.Queue[Utterance] = asyncio.Queue()
        self._commands: asyncio.Queue[str] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._asking = False

    # ---------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        """Watch for meetings until cancelled."""
        self._loop = asyncio.get_running_loop()
        self._status("paused" if paused_until() else "idle")
        watcher = asyncio.create_task(self._watch())
        commands = asyncio.create_task(self._serve_commands())
        try:
            await asyncio.gather(watcher, commands)
        finally:
            watcher.cancel()
            commands.cancel()
            if self.current is not None:
                await self._end()

    async def _watch(self) -> None:
        async for event in watch_meetings(
            allow=self.allow,
            deny=self.deny,
            start_hold_s=self.start_hold_s,
            end_grace_s=self.end_grace_s,
            is_paused=lambda: paused_until() is not None,
        ):
            if event.kind == "start":
                await self._start(event.app)
            elif self.current is not None and not self.current.manual:
                # A manually started recording outlives the app that triggered
                # detection: the user asked for it, so only they end it.
                await self._end()

    async def _serve_commands(self) -> None:
        """Serve `start_now()` / `stop_now()` from the engine's own loop."""
        while True:
            cmd = await self._commands.get()
            if cmd == "start" and self.current is None:
                await self._start("manual", manual=True)
            elif cmd == "stop" and self.current is not None:
                await self._end()

    def start_now(self) -> None:
        """Ask the engine to begin recording. Safe to call from another thread."""
        self._command("start")

    def stop_now(self) -> None:
        """Ask the engine to end the current recording. Thread-safe."""
        self._command("stop")

    def _command(self, cmd: str) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._commands.put_nowait, cmd)

    async def _start(self, app: str, manual: bool = False) -> None:
        if self.current is not None:
            return
        started_at = time.time()
        session_dir = self.root / time.strftime("%Y%m%d-%H%M%S", time.localtime(started_at))
        session_dir.mkdir(parents=True, exist_ok=True)
        rec = Recording(
            session_dir=session_dir, started_at=started_at, app=app, manual=manual
        )
        self.current = rec
        self.transcript = []

        rec.transcript_jsonl = (session_dir / "transcript.jsonl").open("a")
        _rewrite_meta(session_dir, started_at, app, manual)

        try:
            mic = await capture_mic()
            sink_mon = await default_sink_monitor()
            you_wav = open_wav(session_dir / "you.wav")
            them_wav = open_wav(session_dir / "them.wav")
        except Exception as e:  # noqa: BLE001 — surface it rather than half-record
            self.current = None
            self.emit(EngineError("capture", f"could not open audio: {e}", fatal=True))
            self._status("idle")
            return
        rec.handles = [you_wav, them_wav]

        # Stamped here, immediately before capture starts, rather than when we
        # decided to record: two `pactl` subprocesses sit in between, and this is
        # the number every transcript line is mapped back onto the audio with.
        rec.started_at = started_at = time.time()
        _rewrite_meta(session_dir, started_at, app, manual)

        rec.tasks = [
            asyncio.create_task(self._record_mic(mic, you_wav)),
            asyncio.create_task(self._stream_them(sink_mon, them_wav)),
            asyncio.create_task(self._pump()),
        ]
        self.emit(MeetingStarted(session_dir, app, started_at, manual))
        self._status("recording", app)

    async def _end(self) -> None:
        rec = self.current
        if rec is None:
            return
        self.current = None
        rec.stop()
        await asyncio.sleep(0.2)  # let cancelled writers unwind before we read
        if rec.transcript_jsonl is not None:
            with contextlib.suppress(Exception):
                rec.transcript_jsonl.close()

        duration = time.time() - rec.started_at
        self.emit(MeetingEnded(rec.session_dir, duration))
        self._status("finalizing", rec.session_dir.name)
        await self._finish(rec)
        self._status("paused" if paused_until() else "idle")

    # ------------------------------------------------------------------ capture

    async def _record_mic(self, source: str, wav) -> None:
        """Record the mic to disk, reopening parec if the device hiccups."""
        while True:
            try:
                await record_only(source, wav)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a dead capture must not end the meeting
                self.emit(EngineError("mic", f"capture on {source} failed: {e}"))
            await asyncio.sleep(2)

    async def _stream_them(self, source: str, wav) -> None:
        while True:
            try:
                await stream_speaker("them", source, self.queue, wav)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — retry across network blips
                self.emit(EngineError("deepgram", f"stream error: {e}; retrying"))
                await asyncio.sleep(2)

    async def _pump(self) -> None:
        """Persist finals, and pass everything up so the live view can show it."""
        while True:
            u = await self.queue.get()
            rec = self.current
            if rec is None:
                continue
            ts = time.time()
            self.emit(Heard(u.speaker, u.text, u.final, ts))
            if not u.final or rec.transcript_jsonl is None:
                continue
            row = {"ts": ts, "speaker": u.speaker, "text": u.text}
            if u.offset is not None:
                row["offset"] = round(u.offset, 3)
            self.transcript.append(row)
            rec.transcript_jsonl.write(json.dumps(row) + "\n")
            rec.transcript_jsonl.flush()

    # ------------------------------------------------------------- finalization

    async def _finish(self, rec: Recording) -> None:
        """Batch-transcribe the mic, merge both sides, then summarize."""
        you_wav = rec.session_dir / "you.wav"
        mine: list[dict] = []
        if you_wav.is_file():
            self.emit(Finalizing(rec.session_dir, "transcribing your side"))
            try:
                mine = await transcribe_file(you_wav, rec.started_at, "you")
            except Exception as e:  # noqa: BLE001 — keep the far side regardless
                self.emit(EngineError("transcribe", f"mic transcription failed: {e}"))

        records = sorted(load_records(rec.session_dir) + mine, key=lambda r: r["ts"])
        if not records:
            self.emit(EngineError("summary", "nothing was transcribed; no summary"))
            return
        write_transcripts(rec.session_dir, records)

        if not self.summarize:
            return
        self.emit(Finalizing(rec.session_dir, "writing the summary"))
        brief = load_brief(self.brief_path)
        try:
            summary = await summarize_session(rec.session_dir, self.model, brief)
        except Exception as e:  # noqa: BLE001 — the transcript is already safe on disk
            self.emit(EngineError("summary", f"summary failed: {e}"))
            return
        self.emit(SummaryReady(rec.session_dir, summary.title, summary.body))

    # --------------------------------------------------------------- the advisor

    async def ask(
        self, question: str = "", max_lines: int = 80, session_dir: Path | None = None
    ) -> None:
        """Answer a question about a meeting, streaming as it arrives.

        With no `session_dir` that is the meeting in progress, where only the
        recent stretch matters. A past meeting is asked about as a whole.
        """
        if self._asking:
            return
        self._asking = True
        try:
            lines = (
                load_records(session_dir) if session_dir is not None
                else self.transcript[-max_lines:]
            )
            if not lines:
                self.emit(AdviceDone("Nothing has been transcribed yet."))
                return
            convo = "\n".join(f"{r['speaker']}: {r['text']}" for r in lines)
            content = f"{convo}\n\n---\nThe user asks: {question}" if question else convo

            brief = load_brief(self.brief_path)
            system = ADVISOR_PROMPT if session_dir is None else REVIEW_PROMPT
            if brief:
                system += f"\n\n# Standing context\n\n{brief}"
            answer = await claude.stream_text(
                content,
                system=system,
                model=self.model,
                on_delta=lambda d: self.emit(AdviceDelta(d)),
            )
            self.emit(AdviceDone(answer))
        except Exception as e:  # noqa: BLE001 — a failed question must not end the meeting
            self.emit(EngineError("advisor", str(e)))
            self.emit(AdviceDone(""))
        finally:
            self._asking = False

    # ---------------------------------------------------------------- internals

    def _status(self, status: str, detail: str = "") -> None:
        self.emit(StatusChanged(status, detail))


def _rewrite_meta(session_dir: Path, started_at: float, app: str, manual: bool) -> None:
    with contextlib.suppress(OSError):
        (session_dir / META_FILENAME).write_text(json.dumps({
            "started_at": started_at, "app": app, "manual": manual,
        }, indent=2) + "\n")


def write_transcripts(session_dir: Path, records: list[dict]) -> None:
    """Rewrite both transcript files from the merged, time-ordered records."""
    with (session_dir / "transcript.jsonl").open("w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    with (session_dir / "transcript.txt").open("w") as fh:
        for r in records:
            stamp = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
            fh.write(f"[{stamp}] {r['speaker']}: {r['text']}\n")
