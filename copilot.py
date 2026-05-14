"""Live meeting copilot.

Captures default mic ("you") and default sink monitor ("them") via parec,
streams both to Deepgram, displays a 3-pane TUI, and on Ctrl+G sends the
rolling transcript to Claude for a tight advisory nudge.

Each session writes to sessions/<timestamp>/:
  you.wav, them.wav, transcript.jsonl, transcript.txt
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import websockets
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, RichLog

load_dotenv()

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # 100ms @ 16kHz s16le mono

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


async def stream_speaker(
    speaker: str,
    source: str,
    queue: asyncio.Queue[Utterance],
    wav: wave.Wave_write,
) -> None:
    """Spawn parec on `source`, tee PCM into `wav` and Deepgram, push results to queue."""
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
            additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
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


class CopilotApp(App):
    CSS = """
    Screen { layout: vertical; }
    #panes { height: 1fr; }
    #transcripts { width: 60%; }
    #advisor { width: 40%; }
    RichLog { border: round grey; padding: 0 1; }
    """
    BINDINGS = [
        Binding("ctrl+g", "ask", "Ask Claude"),
        Binding("ctrl+l", "clear_advisor", "Clear advisor"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.transcript: deque[Utterance] = deque(maxlen=400)
        self.claude = AsyncAnthropic()
        self.queue: asyncio.Queue[Utterance] = asyncio.Queue()
        self.session_dir: Path | None = None
        self.transcript_jsonl = None
        self.transcript_txt = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="panes"):
            with Vertical(id="transcripts"):
                yield RichLog(id="you", wrap=True, markup=True)
                yield RichLog(id="them", wrap=True, markup=True)
            yield RichLog(id="advisor", wrap=True, markup=True)
        yield Footer()

    async def on_mount(self) -> None:
        you = self.query_one("#you", RichLog)
        them = self.query_one("#them", RichLog)
        adv = self.query_one("#advisor", RichLog)
        you.border_title = "You (mic)"
        them.border_title = "Them (system audio)"
        adv.border_title = "Advisor — Ctrl+G to ask"

        self.session_dir = Path("sessions") / time.strftime("%Y%m%d-%H%M%S")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_jsonl = (self.session_dir / "transcript.jsonl").open("a")
        self.transcript_txt = (self.session_dir / "transcript.txt").open("a")

        mic = await default_mic()
        sink_mon = await default_sink_monitor()
        adv.write(
            f"[dim]mic: {mic}\nsink monitor: {sink_mon}\n"
            f"model: {MODEL}\nsession: {self.session_dir}[/]"
        )

        asyncio.create_task(self._run_speaker("you", mic))
        asyncio.create_task(self._run_speaker("them", sink_mon))
        asyncio.create_task(self._pump())

    async def on_unmount(self) -> None:
        for fh in (self.transcript_jsonl, self.transcript_txt):
            if fh is not None:
                try:
                    fh.flush()
                    fh.close()
                except Exception:
                    pass

    async def _run_speaker(self, speaker: str, source: str) -> None:
        adv = self.query_one("#advisor", RichLog)
        assert self.session_dir is not None
        wav = open_wav(self.session_dir / f"{speaker}.wav")
        try:
            while True:
                try:
                    await stream_speaker(speaker, source, self.queue, wav)
                except Exception as e:
                    adv.write(f"[red]{speaker} stream error: {e!r}; retrying in 2s[/]")
                    await asyncio.sleep(2)
        finally:
            wav.close()

    async def _pump(self) -> None:
        you_log = self.query_one("#you", RichLog)
        them_log = self.query_one("#them", RichLog)
        while True:
            u = await self.queue.get()
            if not u.final:
                continue
            ts = time.time()
            if self.transcript_jsonl is not None:
                self.transcript_jsonl.write(json.dumps({
                    "ts": ts, "speaker": u.speaker, "text": u.text,
                }) + "\n")
                self.transcript_jsonl.flush()
            if self.transcript_txt is not None:
                self.transcript_txt.write(
                    f"[{time.strftime('%H:%M:%S', time.localtime(ts))}] "
                    f"{u.speaker}: {u.text}\n"
                )
                self.transcript_txt.flush()
            (you_log if u.speaker == "you" else them_log).write(u.text)
            self.transcript.append(u)

    def _recent_lines(self, max_items: int = 60) -> str:
        items = list(self.transcript)[-max_items:]
        return "\n".join(f"{u.speaker}: {u.text}" for u in items)

    async def action_ask(self) -> None:
        adv = self.query_one("#advisor", RichLog)
        ctx = self._recent_lines()
        if not ctx:
            adv.write("[dim](no transcript yet)[/]")
            return
        adv.write("\n[bold cyan]>> advisor[/]")
        system = (
            "You are a real-time meeting copilot. Given the recent transcript "
            "('you' is the user wearing the mic; 'them' is everyone else), do ONE: "
            "(a) if a question was just asked or the user seems stuck, give a tight "
            "answer or nudge; (b) otherwise list new action items and open threads. "
            "Be terse, bullets ok, target <80 words."
        )
        try:
            async with self.claude.messages.stream(
                model=MODEL,
                max_tokens=500,
                system=system,
                messages=[{"role": "user", "content": ctx}],
            ) as stream:
                buf = ""
                async for delta in stream.text_stream:
                    buf += delta
                adv.write(buf)
        except Exception as e:
            adv.write(f"[red]advisor error: {e!r}[/]")

    def action_clear_advisor(self) -> None:
        self.query_one("#advisor", RichLog).clear()


def _preflight() -> None:
    missing_env = [k for k in ("DEEPGRAM_API_KEY", "ANTHROPIC_API_KEY") if not os.environ.get(k)]
    if missing_env:
        print(f"Missing env: {', '.join(missing_env)}. Copy .env.example to .env and fill in.", file=sys.stderr)
        sys.exit(1)
    missing_tools = [t for t in ("pactl", "parec") if not shutil.which(t)]
    if missing_tools:
        print(f"Missing tools: {', '.join(missing_tools)}. Install pipewire-pulse (or pulseaudio).", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    _preflight()
    CopilotApp().run()


if __name__ == "__main__":
    main()
