"""Meeting detection: which apps hold an open microphone stream.

An app that opens a PulseAudio *recording* stream is in a call — Zoom, Discord,
Slack, and browser tabs on Meet all do; YouTube and Spotify never do. That makes
source-outputs a far cleaner signal than voice activity detection, and it costs
essentially nothing to watch.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

# Our own parec children tag themselves with `--client-name` so we never detect
# ourselves and latch on forever.
SELF_CLIENT_NAME = "steno"

# Browsers cover Meet/Zoom-web/Whereby and anything else over WebRTC.
DEFAULT_ALLOW = (
    "zoom", "teams", "teams-for-linux", "slack", "discord", "webex", "skype",
    "chromium", "chrome", "google-chrome", "brave", "firefox", "librewolf",
    "element", "signal-desktop", "telegram-desktop", "ferdium", "obs",
)

START_HOLD_S = 20.0   # continuous capture before a meeting counts as started
END_GRACE_S = 90.0    # silence after the last stream closes before it ends
POLL_INTERVAL_S = 5.0  # safety net; `pactl subscribe` drives the fast path

_INDEX_RE = re.compile(r"^Source Output #(\d+)")
_PROP_RE = re.compile(r'^\s*([\w.]+)\s*=\s*"(.*)"\s*$')
_CORKED_RE = re.compile(r"^\s*Corked:\s*(yes|no)\s*$")


@dataclass(frozen=True)
class RecordingStream:
    index: int
    app_name: str
    binary: str
    pid: int
    corked: bool

    @property
    def label(self) -> str:
        return self.app_name or self.binary or f"#{self.index}"


async def _pactl(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "pactl", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace")


def parse_source_outputs(text: str) -> list[RecordingStream]:
    """Parse `pactl list source-outputs`. Unknown/missing fields degrade to blanks."""
    streams: list[RecordingStream] = []
    index: int | None = None
    corked = False
    props: dict[str, str] = {}

    def flush() -> None:
        if index is None:
            return
        try:
            pid = int(props.get("application.process.id", "0"))
        except ValueError:
            pid = 0
        streams.append(RecordingStream(
            index=index,
            app_name=props.get("application.name", ""),
            binary=props.get("application.process.binary", ""),
            pid=pid,
            corked=corked,
        ))

    for line in text.splitlines():
        m = _INDEX_RE.match(line)
        if m:
            flush()
            index = int(m.group(1))
            corked = False
            props = {}
            continue
        if index is None:
            continue
        m = _CORKED_RE.match(line)
        if m:
            corked = m.group(1) == "yes"
            continue
        m = _PROP_RE.match(line)
        if m:
            props[m.group(1)] = m.group(2)
    flush()
    return streams


async def list_recording_streams() -> list[RecordingStream]:
    return parse_source_outputs(await _pactl("list", "source-outputs"))


def _matches(stream: RecordingStream, names: tuple[str, ...]) -> bool:
    haystack = (stream.binary.lower(), stream.app_name.lower())
    return any(n.lower() in h for n in names for h in haystack if h)


def qualifying(
    streams: list[RecordingStream],
    allow: tuple[str, ...] | None = DEFAULT_ALLOW,
    deny: tuple[str, ...] = (),
    self_pids: frozenset[int] = frozenset(),
) -> list[RecordingStream]:
    """Streams that indicate a live meeting.

    `allow=None` means "any app that isn't denied" — more forgiving of apps we
    have never seen, at the cost of false positives from anything that grabs the
    mic for its own reasons.
    """
    out = []
    for s in streams:
        if s.corked:
            continue
        if s.app_name == SELF_CLIENT_NAME or s.pid in self_pids:
            continue
        if deny and _matches(s, deny):
            continue
        if allow is not None and not _matches(s, allow):
            continue
        out.append(s)
    return out


@dataclass(frozen=True)
class MeetingEvent:
    kind: str  # "start" | "end"
    app: str


async def _subscribe_wakeups(queue: asyncio.Queue[None]) -> None:
    """Push a token whenever pactl reports a source-output change."""
    while True:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "pactl", "subscribe",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert proc.stdout is not None
            async for raw in proc.stdout:
                if b"source-output" in raw:
                    with contextlib.suppress(asyncio.QueueFull):
                        queue.put_nowait(None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the poll fallback keeps us correct
            await asyncio.sleep(2)
        finally:
            if proc is not None and proc.returncode is None:
                proc.terminate()
                with contextlib.suppress(TimeoutError, ProcessLookupError):
                    await asyncio.wait_for(proc.wait(), timeout=2)


async def watch_meetings(
    allow: tuple[str, ...] | None = DEFAULT_ALLOW,
    deny: tuple[str, ...] = (),
    start_hold_s: float = START_HOLD_S,
    end_grace_s: float = END_GRACE_S,
    poll_interval_s: float = POLL_INTERVAL_S,
    self_pids: frozenset[int] = frozenset(),
    is_paused: Callable[[], bool] | None = None,
) -> AsyncIterator[MeetingEvent]:
    """Yield start/end events as meeting apps open and close the microphone.

    `start_hold_s` debounces push-to-talk and notification blips; `end_grace_s`
    keeps one session across a reconnect or a device switch mid-call.
    `is_paused` is an optional zero-arg callable; while it returns True no start
    is emitted (a running meeting is still allowed to end).
    """
    queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
    waker = asyncio.create_task(_subscribe_wakeups(queue))

    in_meeting = False
    present_since: float | None = None
    absent_since: float | None = None
    current_app = ""

    try:
        while True:
            now = asyncio.get_running_loop().time()
            live = qualifying(await list_recording_streams(), allow, deny, self_pids)

            if live:
                absent_since = None
                if present_since is None:
                    present_since = now
                started = not in_meeting and (now - present_since) >= start_hold_s
                if started and (is_paused is None or not is_paused()):
                    current_app = live[0].label
                    in_meeting = True
                    yield MeetingEvent("start", current_app)
            else:
                present_since = None
                if in_meeting:
                    if absent_since is None:
                        absent_since = now
                    elif (now - absent_since) >= end_grace_s:
                        in_meeting = False
                        absent_since = None
                        yield MeetingEvent("end", current_app)
                        current_app = ""

            # Wake exactly when the pending hold/grace elapses, so a start or end
            # isn't deferred to the next poll tick.
            timeout = poll_interval_s
            if present_since is not None and not in_meeting:
                timeout = min(timeout, max(0.2, start_hold_s - (now - present_since)))
            elif absent_since is not None and in_meeting:
                timeout = min(timeout, max(0.2, end_grace_s - (now - absent_since)))
            try:
                await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                pass
    finally:
        waker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waker
