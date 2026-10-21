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
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from . import claude, config_dir, data_dir, model_default
from .audio import capture_mic, default_sink_monitor, open_wav, record_only
from .brief import BRIEF_FILENAME, brief_for, load_brief, proper_nouns
from .detect import DEFAULT_ALLOW, END_GRACE_S, START_HOLD_S, watch_meetings
from .events import (
    AdviceDelta,
    AdviceDone,
    EngineError,
    Event,
    Heard,
    MeetingEnded,
    MeetingStarted,
    NotesStatus,
    StatusChanged,
    SummaryReady,
)
from .notesjob import NotesPhase, NotesState
from .playback import session_start
from .redact import redact_text
from .summarize import (
    META_FILENAME,
    load_meta,
    load_records,
    summarize_session,
    update_meta,
)
from .transcribe import Utterance, stream_speaker, transcribe_file

PAUSE_FILENAME = "paused-until"
# One notes job (mic pass + summary) gets this long before it is cut off.
# The model call alone may take a quarter of an hour; the outer bound is
# what stops a wedged job from holding the meeting forever.
NOTES_TIMEOUT_S = 30 * 60

log = logging.getLogger("steno.session")

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
    # Taken at the start: a meeting that began during the last one's
    # finishing must not hand it the wrong app's names.
    keyterms: list[str] = field(default_factory=list)

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

    def __init__(  # noqa: PLR0913, PLR0917 - Engine's full config surface
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
        notes_timeout_s: float = NOTES_TIMEOUT_S,
    ) -> None:
        self.emit = emit
        self.allow = allow
        self.deny = deny
        self.model = model or model_default()
        self.summarize = summarize
        self.root = root or (data_dir() / "sessions")
        self.brief_path = brief_path or (config_dir() / BRIEF_FILENAME)
        self.start_hold_s = start_hold_s
        self.end_grace_s = end_grace_s
        self.notes_timeout_s = notes_timeout_s

        self.current: Recording | None = None
        # Post-meeting work in flight (batch transcription, a summary). `current`
        # is already None while it runs, so a quit that only looked at `current`
        # cancelled the very work that makes a recording worth keeping.
        self._finalizing = 0
        self._finishers: set[asyncio.Task] = set()
        # One live notes task per meeting; a second ask joins it.
        self._notes_tasks: dict[Path, asyncio.Task] = {}
        self.transcript: list[dict] = []
        self.queue: asyncio.Queue[Utterance] = asyncio.Queue()
        self._commands: asyncio.Queue[str] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._asking = False
        self._keyterms: list[str] = []
        # Someone is looking at the window. The mic is normally only recorded,
        # not streamed; while this is on its words are also shown live.
        self._audience = False
        self._you_display: asyncio.Task | None = None

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
            # A meeting that has just ended is still being turned into notes.
            if self._finishers:
                await asyncio.gather(*self._finishers, return_exceptions=True)

    async def _watch(self) -> None:
        while True:
            try:
                async for event in watch_meetings(
                    allow=self.allow,
                    deny=self.deny,
                    start_hold_s=self.start_hold_s,
                    end_grace_s=self.end_grace_s,
                    is_paused=lambda: paused_until() is not None,
                ):
                    await self._guarded(self._on_detect, event)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # The detector itself failed (a `pactl` that would not spawn).
                # Ending here would stop listening with the bar still saying so.
                self.emit(EngineError("engine", f"detection failed: {e}; retrying", fatal=True))
                await asyncio.sleep(5)
            else:
                return

    async def _on_detect(self, event) -> None:
        if event.kind == "start":
            await self._start(event.app)
        elif self.current is not None and not self.current.manual:
            # A manually started recording outlives the app that triggered
            # detection: the user asked for it, so only they end it.
            await self._end()

    async def _guarded(self, handler, *args) -> None:
        """Run one step of the watch or command loop; a failure is not the end.

        One full disk or unmounted data directory used to raise out of the
        loop, end the engine's thread, and leave the bar saying "listening"
        for good. Say what went wrong, and keep listening.
        """
        try:
            await handler(*args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.emit(EngineError("engine", f"{type(e).__name__}: {e}", fatal=True))
            self._settle()

    async def _serve_commands(self) -> None:
        """Serve `start_now()` / `stop_now()` / `set_audience()` from our loop."""
        while True:
            await self._guarded(self._on_command, await self._commands.get())

    async def _on_command(self, cmd: str) -> None:
        if cmd == "start" and self.current is None:
            await self._start("manual", manual=True)
        elif cmd == "stop" and self.current is not None:
            await self._end()
        elif cmd == "audience-on":
            self._audience = True
            self._start_you_display()
        elif cmd == "audience-off":
            self._audience = False
            self._stop_you_display()

    @property
    def busy(self) -> bool:
        """A meeting is being recorded or is still being turned into notes."""
        return self.current is not None or self._finalizing > 0

    def start_now(self) -> None:
        """Ask the engine to begin recording. Safe to call from another thread."""
        self._command("start")

    def stop_now(self) -> None:
        """Ask the engine to end the current recording. Thread-safe."""
        self._command("stop")

    def set_audience(self, watching: bool) -> None:
        """Someone opened or closed the window. Thread-safe.

        The far side is always streamed — that is the live use case. Your own
        words are only streamed while someone can see them, because a second
        Deepgram stream for a room nobody is looking at is pure spend.
        """
        self._command("audience-on" if watching else "audience-off")

    def _command(self, cmd: str) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._commands.put_nowait, cmd)

    async def _start(self, app: str, manual: bool = False) -> None:
        if self.current is not None:
            return
        started_at = time.time()
        session_dir = self.root / time.strftime("%Y%m%d-%H%M%S", time.localtime(started_at))
        created = not session_dir.exists()
        session_dir.mkdir(parents=True, exist_ok=True)
        rec = Recording(
            session_dir=session_dir, started_at=started_at, app=app, manual=manual
        )
        self.current = rec
        self.transcript = []

        you_wav = them_wav = None
        try:
            rec.transcript_jsonl = (session_dir / "transcript.jsonl").open("a")
            _rewrite_meta(session_dir, started_at, app, manual)
            mic = await capture_mic()
            sink_mon = await default_sink_monitor()
            you_wav = open_wav(session_dir / "you.wav")
            them_wav = open_wav(session_dir / "them.wav")
        except Exception as e:
            for handle in (you_wav, them_wav, rec.transcript_jsonl):
                if handle is not None:
                    with contextlib.suppress(Exception):
                        handle.close()
            self.current = None
            # Nothing was recorded: an empty session left behind would be
            # listed forever as a meeting that never got its summary.
            if created:
                shutil.rmtree(session_dir, ignore_errors=True)
            self.emit(EngineError("capture", f"could not open audio: {e}", fatal=True))
            self._settle()
            return
        rec.handles = [you_wav, them_wav]

        try:
            # Stamped here, immediately before capture starts, rather than when
            # we decided to record: two `pactl` subprocesses sit in between, and
            # this is the number every transcript line is mapped back onto the
            # audio with.
            rec.started_at = started_at = time.time()
            _rewrite_meta(session_dir, started_at, app, manual)

            # Names the brief cares about, so "Mimo" is not transcribed "me
            # moan". Extracted once per meeting: the brief is short, and a live
            # meeting must not re-read a file on every utterance.
            rec.keyterms = self._keyterms = self._load_keyterms(app)

            rec.tasks = [
                asyncio.create_task(self._record_mic(mic, you_wav)),
                asyncio.create_task(self._stream_them(sink_mon, them_wav)),
                asyncio.create_task(self._pump()),
            ]
            self.emit(MeetingStarted(session_dir, app, started_at, manual))
            self._status("recording", app)
            if self._audience:
                self._start_you_display()
        except Exception:
            # Left as it was, `current` would stay set with nothing capturing,
            # and every later detection would be ignored as "already recording".
            rec.stop()
            if rec.transcript_jsonl is not None:
                with contextlib.suppress(Exception):
                    rec.transcript_jsonl.close()
            self.current = None
            raise

    async def _end(self) -> None:
        """Stop capturing, then finish the meeting in the background.

        Returns as soon as the capture is closed. The batch transcription and
        the summary can take minutes, and the detector shares this task: a
        call that opens the mic while the last one is being summarized must
        still be caught from its first seconds.
        """
        rec = self.current
        if rec is None:
            return
        # Counted before `current` clears, so there is no instant at which a
        # meeting is neither recording nor being finished (a quit that looked
        # at `busy` in that gap cancelled the summary before it started).
        self._finalizing += 1
        try:
            self.current = None
            self._stop_you_display()
            rec.stop()
            await asyncio.sleep(0.2)  # let cancelled writers unwind before we read
            if rec.transcript_jsonl is not None:
                with contextlib.suppress(Exception):
                    rec.transcript_jsonl.close()

            duration = time.time() - rec.started_at
            self.emit(MeetingEnded(rec.session_dir, duration))
            self._status("finalizing", rec.session_dir.name)
            task = asyncio.create_task(
                self._run_notes_job(rec.session_dir, rec=rec, counted=True)
            )
        except BaseException:
            self._finalizing -= 1
            raise
        self._finishers.add(task)
        task.add_done_callback(self._finishers.discard)

    def _settle(self) -> None:
        """Back to listening, unless another meeting is recording or finishing."""
        if self.current is None and self._finalizing == 0:
            self._status("paused" if paused_until() else "idle")

    def _notes_running(
        self, session_dir: Path, phase: NotesPhase, *, requested: bool, joined: bool = False
    ) -> None:
        self.emit(NotesStatus(
            session_dir=session_dir,
            state=NotesState.RUNNING,
            phase=phase,
            requested=requested,
            joined=joined,
        ))

    async def summarize_now(self, session_dir: Path) -> None:
        """Write (or rewrite) one past meeting's summary, on request.

        For a meeting whose summary never came: the model host was down, the
        key was wrong, steno was quit mid-write. Same path as the end of a
        meeting, so the to-dos and the title follow. If your side was never
        transcribed (the app was quit or killed first) that pass is done too:
        a summary of half a conversation is the failure being repaired.

        A job already running on this meeting is joined, not started again.
        """
        await self._run_notes_job(session_dir, requested=True)

    async def _run_notes_job(
        self,
        session_dir: Path,
        *,
        rec: Recording | None = None,
        requested: bool = False,
        counted: bool = False,
    ) -> None:
        """One notes job for one meeting: start or join, then settle.

        The only place a job is started. If a task is already live on this
        meeting the caller is told so and nothing new begins — the page
        never says work is under way when nothing is.

        `counted` means the caller has already raised `_finalizing` (the
        end-of-meeting path does, before `current` clears) and this will
        lower it once.
        """
        running = self._notes_tasks.get(session_dir)
        if running is not None and not running.done():
            log.info("notes join %s", session_dir.name)
            self._notes_running(
                session_dir, NotesPhase.SUMMARIZING,
                requested=requested, joined=True,
            )
            return

        if not counted:
            self._finalizing += 1
        task = asyncio.current_task()
        if task is not None:
            self._notes_tasks[session_dir] = task
        log.info("notes start %s requested=%s", session_dir.name, requested)
        self._notes_running(
            session_dir, NotesPhase.TRANSCRIBING, requested=requested
        )
        try:
            await asyncio.wait_for(
                self._notes_pipeline(session_dir, rec, requested=requested),
                timeout=self.notes_timeout_s,
            )
        except TimeoutError:
            log.warning(
                "notes timeout %s after %.0fs",
                session_dir.name, self.notes_timeout_s,
            )
            self._fail_notes(
                session_dir,
                f"gave up after {int(self.notes_timeout_s // 60)} minutes",
            )
        except asyncio.CancelledError:
            log.info("notes cancelled %s", session_dir.name)
            self._fail_notes(session_dir, "cancelled")
            raise
        except Exception as e:
            log.exception("notes failed %s", session_dir.name)
            self._fail_notes(session_dir, str(e) or type(e).__name__)
        else:
            log.info("notes written %s", session_dir.name)
            self.emit(NotesStatus(
                session_dir=session_dir,
                state=NotesState.WRITTEN,
                requested=requested,
            ))
        finally:
            if self._notes_tasks.get(session_dir) is task:
                self._notes_tasks.pop(session_dir, None)
            self._finalizing -= 1
            self._settle()

    def _fail_notes(self, session_dir: Path, error: str) -> None:
        self.emit(NotesStatus(
            session_dir=session_dir, state=NotesState.FAILED, error=error
        ))
        self.emit(EngineError(
            "summary", f"summary failed: {error}", session_dir=session_dir
        ))

    async def _notes_pipeline(
        self, session_dir: Path, rec: Recording | None, *, requested: bool
    ) -> None:
        """Transcribe what is missing, merge, then write the summary.

        Raises on failure; `_run_notes_job` turns that into a failed job.
        The SummaryReady event is emitted here, once the file exists.
        """
        started_at = rec.started_at if rec is not None else session_start(
            session_dir, load_records(session_dir)
        )
        keyterms = rec.keyterms if rec is not None else self._load_keyterms("")
        you_wav = session_dir / "you.wav"
        # End of a meeting always merges your side; a later ask only does
        # that pass when the meeting is still marked missing it.
        need_mic = you_wav.is_file() and (
            rec is not None or bool(load_meta(session_dir).get("mic_pending"))
        )
        mine: list[dict] = []
        mic_done = True
        if need_mic:
            self._notes_running(
                session_dir, NotesPhase.TRANSCRIBING, requested=requested
            )
            mine, mic_done = await self._mic_pass(
                session_dir, started_at, keyterms
            )

        records = sorted(load_records(session_dir) + mine, key=lambda r: r["ts"])
        if not records:
            msg = "nothing was transcribed; no summary"
            raise ValueError(msg)
        write_transcripts(session_dir, records)
        if mic_done:
            # Only now, with your lines on disk: a kill before this point
            # leaves the meeting marked as missing your side.
            with contextlib.suppress(OSError):
                update_meta(session_dir, mic_pending=None)

        if not self.summarize:
            return

        self._notes_running(
            session_dir, NotesPhase.SUMMARIZING, requested=requested
        )
        brief = self._meeting_brief(rec.app if rec is not None else "")
        summary = await summarize_session(session_dir, self.model, brief)
        self.emit(SummaryReady(
            session_dir, summary.title, summary.body, requested=requested
        ))

    async def _mic_pass(
        self, session_dir: Path, started_at: float, keyterms: list[str]
    ) -> tuple[list[dict], bool]:
        """Batch-transcribe your side. (records, whether it worked)."""
        try:
            mine = await transcribe_file(
                session_dir / "you.wav", started_at, "you", keyterms=keyterms
            )
        except Exception as e:
            self.emit(EngineError(
                "transcribe", f"mic transcription failed: {e}", session_dir=session_dir
            ))
            return [], False
        return mine, True

    # ------------------------------------------------------------------ capture

    async def _record_mic(self, source: str, wav) -> None:
        """Record the mic to disk, reopening parec if the device hiccups."""
        while True:
            try:
                await record_only(source, wav)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.emit(EngineError("mic", f"capture on {source} failed: {e}"))
            await asyncio.sleep(2)

    async def _stream_them(self, source: str, wav) -> None:
        while True:
            try:
                await stream_speaker("them", source, self.queue, wav,
                                    keyterms=self._keyterms)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.emit(EngineError("deepgram", f"stream error: {e}; retrying"))
                await asyncio.sleep(2)

    async def _pump(self) -> None:
        """Persist finals, and pass everything up so the live view can show it."""
        save_failed = False
        while True:
            u = await self.queue.get()
            rec = self.current
            if rec is None:
                continue
            ts = time.time()
            self.emit(Heard(u.speaker, u.text, u.final, ts))
            if not u.final or not u.persist or rec.transcript_jsonl is None:
                continue
            row = {"ts": ts, "speaker": u.speaker, "text": u.text}
            if u.offset is not None:
                row["offset"] = round(u.offset, 3)
            if u.who:
                row["who"] = u.who
            self.transcript.append(row)
            try:
                rec.transcript_jsonl.write(json.dumps(row) + "\n")
                rec.transcript_jsonl.flush()
            except (OSError, ValueError) as e:
                # Disk full, or the file went away. The live view keeps
                # working; say once that the transcript on disk is not.
                if not save_failed:
                    save_failed = True
                    self.emit(EngineError(
                        "engine", f"could not save the transcript: {e}", fatal=True
                    ))

    # --------------------------------------------------------------- the advisor

    async def ask(
        self,
        question: str = "",
        max_lines: int = 80,
        session_dir: Path | None = None,
        model: str | None = None,
    ) -> None:
        """Answer a question about a meeting, streaming as it arrives.

        With no `session_dir` that is the meeting in progress, where only the
        recent stretch matters. A past meeting is asked about as a whole.
        `model` is the one the window chose for asking; without it the
        engine's own (the summary's) answers.
        """
        if self._asking:
            return
        self._asking = True
        asked = model or self.model
        try:
            lines = (
                load_records(session_dir) if session_dir is not None
                else self.transcript[-max_lines:]
            )
            if not lines:
                self.emit(AdviceDone("Nothing has been transcribed yet.", by_model=False))
                return
            convo = "\n".join(
                f"{r['speaker']}: {redact_text(str(r.get('text', '')))}"
                for r in lines
            )
            content = f"{convo}\n\n---\nThe user asks: {question}" if question else convo

            brief = self._meeting_brief(
                self.current.app if self.current is not None and session_dir is None
                else ""
            )
            system = ADVISOR_PROMPT if session_dir is None else REVIEW_PROMPT
            if brief:
                system += f"\n\n# Standing context\n\n{brief}"
            answer = await claude.stream_text(
                content,
                system=system,
                model=asked,
                on_delta=lambda d: self.emit(AdviceDelta(d)),
            )
            self.emit(AdviceDone(answer))
        except Exception as e:
            # Name the model: a mistyped id comes back as the provider's
            # words about it, which may not say which one it was.
            self.emit(EngineError("advisor", f"{asked} did not answer: {e}"))
            self.emit(AdviceDone("", failed=True))
        finally:
            self._asking = False

    # ---------------------------------------------------------------- internals

    def _load_keyterms(self, app: str = "") -> list[str]:
        """Proper nouns from this meeting's brief, for Deepgram's keyterm boost."""
        brief = load_brief(brief_for(self.brief_path, app))
        return proper_nouns(brief) if brief else []

    def _meeting_brief(self, app: str = "") -> str | None:
        """The standing brief, or the one written for this app."""
        return load_brief(brief_for(self.brief_path, app))

    def _start_you_display(self) -> None:
        """Stream the mic for the live view only. Idempotent.

        `you.wav` is already being written by `_record_mic`; this second parec
        is thrown away after Deepgram hears it. The batch pass at the end is
        still what lands in the transcript.
        """
        rec = self.current
        if rec is None or self._you_display is not None:
            return
        self._you_display = asyncio.create_task(self._stream_you_display())

    def _stop_you_display(self) -> None:
        task, self._you_display = self._you_display, None
        if task is not None:
            task.cancel()

    async def _stream_you_display(self) -> None:
        rec = self.current
        if rec is None:
            return
        try:
            source = await capture_mic()
            await stream_speaker(
                "you", source, self.queue,
                wav=None, keyterms=self._keyterms, persist=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.emit(EngineError("deepgram", f"mic live view failed: {e}"))
        finally:
            self._you_display = None

    def _status(self, status: str, detail: str = "") -> None:
        self.emit(StatusChanged(status, detail))


def _rewrite_meta(session_dir: Path, started_at: float, app: str, manual: bool) -> None:
    # `mic_pending` from the first second: a kill or crash mid-meeting leaves
    # your side untranscribed, and retention must not read that as finished.
    # It is cleared once your lines are merged into the transcript.
    with contextlib.suppress(OSError):
        (session_dir / META_FILENAME).write_text(json.dumps({
            "started_at": started_at, "app": app, "manual": manual,
            "mic_pending": True,
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
