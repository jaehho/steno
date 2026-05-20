"""Textual TUI: dual transcript panes + Claude advisor."""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from pathlib import Path

from anthropic import AsyncAnthropic
from anthropic.types import TextBlockParam
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, RichLog, Static

from . import data_dir
from .audio import default_mic, default_sink_monitor, open_wav
from .brief import (
    BRIEF_FILENAME,
    brief_age_days,
    estimate_tokens,
    format_status,
    format_token_count,
    load_brief,
)
from .transcribe import (
    AUTO_ASK_COOLDOWN_S,
    Utterance,
    is_question,
    stream_speaker,
)

SYSTEM_PROMPT = (
    "You are a real-time meeting copilot. Given the recent transcript "
    "('you' is the user wearing the mic; 'them' is everyone else), do ONE: "
    "(a) if a question was just asked or the user seems stuck, give a tight "
    "answer or nudge; (b) otherwise list new action items and open threads. "
    "Be terse, bullets ok, target <80 words."
)


class CopilotApp(App):
    CSS = """
    Screen { layout: vertical; }
    #panes { height: 1fr; }
    #transcripts { width: 60%; }
    #advisor-col { width: 40%; }
    .speaker { height: 1fr; }
    .speaker > RichLog { height: 1fr; }
    .speaker > Static { height: auto; max-height: 3; color: grey; padding: 0 2; }
    #advisor-col > RichLog { height: 1fr; }
    #advisor-col > Static { height: auto; max-height: 10; color: cyan; padding: 0 2; }
    RichLog { border: round grey; padding: 0 1; }
    """
    BINDINGS = [
        Binding("ctrl+g", "ask", "Ask Claude"),
        Binding("ctrl+l", "clear_advisor", "Clear advisor"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, brief_path: Path | None = None) -> None:
        super().__init__()
        self.transcript: deque[Utterance] = deque(maxlen=400)
        self.claude = AsyncAnthropic()
        self.model_manual = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")
        self.model_auto = os.environ.get("CLAUDE_MODEL_AUTO", "claude-sonnet-4-6")
        self.queue: asyncio.Queue[Utterance] = asyncio.Queue()
        self.session_dir: Path | None = None
        self.transcript_jsonl = None
        self.transcript_txt = None
        self._asking = False
        self._last_ask_ts = 0.0
        resolved_brief = brief_path if brief_path is not None else Path.cwd() / BRIEF_FILENAME
        self.brief_path = resolved_brief
        self.brief = load_brief(resolved_brief)
        self.brief_age = brief_age_days(resolved_brief) if self.brief is not None else None
        self.title = "meeting copilot"
        self.sub_title = format_status(self.brief, self.brief_age)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="panes"):
            with Vertical(id="transcripts"):
                with Vertical(classes="speaker"):
                    yield RichLog(id="you", wrap=True, markup=True)
                    yield Static("", id="you-interim", classes="interim")
                with Vertical(classes="speaker"):
                    yield RichLog(id="them", wrap=True, markup=True)
                    yield Static("", id="them-interim", classes="interim")
            with Vertical(id="advisor-col"):
                yield RichLog(id="advisor", wrap=True, markup=True)
                yield Static("", id="advisor-stream", classes="interim")
        yield Footer()

    async def on_mount(self) -> None:
        you = self.query_one("#you", RichLog)
        them = self.query_one("#them", RichLog)
        adv = self.query_one("#advisor", RichLog)
        you.border_title = "You (mic)"
        them.border_title = "Them (system audio)"
        adv.border_title = "Advisor — Ctrl+G · auto on questions"

        self.session_dir = data_dir() / "sessions" / time.strftime("%Y%m%d-%H%M%S")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_jsonl = (self.session_dir / "transcript.jsonl").open("a")
        self.transcript_txt = (self.session_dir / "transcript.txt").open("a")

        mic = await default_mic()
        sink_mon = await default_sink_monitor()
        adv.write(
            f"[dim]mic: {mic}\nsink monitor: {sink_mon}\n"
            f"model: {self.model_manual} (Ctrl+G) · {self.model_auto} (auto) · 1M context beta\n"
            f"session: {self.session_dir}[/]"
        )
        if self.brief is not None:
            tok = format_token_count(estimate_tokens(self.brief))
            age = "today" if self.brief_age == 0 else f"{self.brief_age}d old"
            adv.write(
                f"[dim]brief: {self.brief_path} · {len(self.brief)} chars · {tok} · {age}[/]"
            )
        else:
            adv.write(f"[dim]brief: none at {self.brief_path}[/]")

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
        you_interim = self.query_one("#you-interim", Static)
        them_interim = self.query_one("#them-interim", Static)
        while True:
            u = await self.queue.get()
            interim_w = you_interim if u.speaker == "you" else them_interim
            log_w = you_log if u.speaker == "you" else them_log
            if not u.final:
                interim_w.update(u.text)
                continue
            interim_w.update("")
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
            log_w.write(u.text)
            self.transcript.append(u)

            if (
                u.speaker == "them"
                and is_question(u.text)
                and not self._asking
                and (time.monotonic() - self._last_ask_ts) > AUTO_ASK_COOLDOWN_S
            ):
                asyncio.create_task(self.action_ask(auto=True))

    def _recent_lines(self, max_items: int = 60) -> str:
        items = list(self.transcript)[-max_items:]
        return "\n".join(f"{u.speaker}: {u.text}" for u in items)

    async def action_ask(self, auto: bool = False) -> None:
        if self._asking:
            return
        self._asking = True
        self._last_ask_ts = time.monotonic()
        adv = self.query_one("#advisor", RichLog)
        stream_w = self.query_one("#advisor-stream", Static)
        ctx = self._recent_lines()
        if not ctx:
            adv.write("[dim](no transcript yet)[/]")
            self._asking = False
            return
        label = ">> advisor (auto)" if auto else ">> advisor"
        adv.write(f"\n[bold cyan]{label}[/]")
        system: str | list[TextBlockParam]
        if self.brief:
            system = [
                TextBlockParam(type="text", text=SYSTEM_PROMPT),
                TextBlockParam(
                    type="text",
                    text=f"# Standing context\n\n{self.brief}",
                    cache_control={"type": "ephemeral"},
                ),
            ]
        else:
            system = SYSTEM_PROMPT
        model = self.model_auto if auto else self.model_manual
        try:
            buf = ""
            async with self.claude.beta.messages.stream(
                model=model,
                max_tokens=500,
                system=system,
                messages=[{"role": "user", "content": ctx}],
                betas=["context-1m-2025-08-07"],
            ) as stream:
                async for delta in stream.text_stream:
                    buf += delta
                    stream_w.update(buf)
            stream_w.update("")
            adv.write(buf)
        except Exception as e:
            stream_w.update("")
            adv.write(f"[red]advisor error: {e!r}[/]")
        finally:
            self._asking = False

    def action_clear_advisor(self) -> None:
        self.query_one("#advisor", RichLog).clear()
        self.query_one("#advisor-stream", Static).update("")
