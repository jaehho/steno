"""Browse past sessions: list, transcript with click-to-seek audio playback."""
from __future__ import annotations

import bisect
import json
import shutil
import threading
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd
import soundfile as sf
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, Static

from . import data_dir

SAMPLE_RATE = 16000


@dataclass
class Session:
    dir: Path
    start: datetime
    duration_s: float
    line_count: int
    has_audio: bool


def discover_sessions(root: Path) -> list[Session]:
    out: list[Session] = []
    if not root.is_dir():
        return out
    for p in sorted(root.iterdir(), reverse=True):
        if not p.is_dir():
            continue
        try:
            start = datetime.strptime(p.name, "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        you_dur = _wav_duration(p / "you.wav")
        them_dur = _wav_duration(p / "them.wav")
        duration = max(you_dur, them_dur)
        lines = _count_lines(p / "transcript.jsonl") or _count_lines(p / "transcript.txt")
        out.append(Session(
            dir=p, start=start, duration_s=duration,
            line_count=lines, has_audio=duration > 0,
        ))
    return out


def _wav_duration(path: Path) -> float:
    if not path.is_file():
        return 0.0
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, OSError):
        return 0.0


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def format_duration(s: float) -> str:
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{sec:02d}s"


def format_clock(s: float) -> str:
    s = max(0, int(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


class StereoMixer:
    """Plays you.wav (left) + them.wav (right) with frame-accurate seek."""

    def __init__(self, you: Path | None, them: Path | None) -> None:
        self._you = sf.SoundFile(str(you)) if you and you.is_file() else None
        self._them = sf.SoundFile(str(them)) if them and them.is_file() else None
        first = self._you or self._them
        self.sample_rate = first.samplerate if first else SAMPLE_RATE
        self._frame = 0
        self._max_frames = max(
            self._you.frames if self._you else 0,
            self._them.frames if self._them else 0,
        )
        self._stream: sd.OutputStream | None = None
        self._lock = threading.Lock()

    @property
    def duration(self) -> float:
        return self._max_frames / self.sample_rate if self._max_frames else 0.0

    @property
    def position(self) -> float:
        with self._lock:
            return self._frame / self.sample_rate

    @property
    def playing(self) -> bool:
        return self._stream is not None and self._stream.active

    def _read_chunk(self, handle: sf.SoundFile | None, start: int, n: int) -> Any:
        if handle is None or start >= handle.frames:
            return np.zeros(n, dtype=np.float32)
        handle.seek(start)
        avail = handle.frames - start
        take = min(n, avail)
        data = handle.read(take, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data[:, 0]
        if take < n:
            data = np.concatenate([data, np.zeros(n - take, dtype=np.float32)])
        return data

    def _callback(self, outdata: Any, frames: int, _time: Any, _status: Any) -> None:
        with self._lock:
            start = self._frame
            self._frame += frames
        outdata[:, 0] = self._read_chunk(self._you, start, frames)
        outdata[:, 1] = self._read_chunk(self._them, start, frames)
        if start + frames >= self._max_frames:
            raise sd.CallbackStop

    def play(self) -> None:
        if self.playing or self._max_frames == 0:
            return
        if self._frame >= self._max_frames:
            self._frame = 0
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=2,
            dtype="float32",
            blocksize=1024,
            callback=self._callback,
        )
        self._stream.start()

    def pause(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def toggle(self) -> None:
        if self.playing:
            self.pause()
        else:
            self.play()

    def seek(self, seconds: float) -> None:
        target = max(0.0, min(seconds, self.duration))
        with self._lock:
            self._frame = int(target * self.sample_rate)

    def close(self) -> None:
        self.pause()
        for h in (self._you, self._them):
            if h is not None:
                try:
                    h.close()
                except Exception:
                    pass


class TranscriptLineClicked(Message):
    def __init__(self, idx: int) -> None:
        super().__init__()
        self.idx = idx


class TranscriptLine(Static):
    """One clickable transcript line; clicking seeks the mixer."""

    def __init__(self, line_idx: int, time_str: str, speaker: str, text: str) -> None:
        color = "yellow" if speaker == "you" else "green"
        super().__init__(
            f"[dim]{time_str}[/]  [{color}]{speaker:<4}[/]  {text}",
            classes="line",
        )
        self.line_idx = line_idx

    def on_click(self) -> None:
        self.post_message(TranscriptLineClicked(self.line_idx))


class ConfirmDelete(ModalScreen[bool]):
    """Yes/no confirmation for deleting a session directory."""

    DEFAULT_CSS = """
    ConfirmDelete { align: center middle; }
    #dialog {
        width: 60; height: auto; padding: 1 2;
        background: $surface; border: thick $error;
    }
    #buttons { height: auto; align: center middle; margin-top: 1; }
    #buttons Button { margin: 0 1; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, session_name: str) -> None:
        super().__init__()
        self.session_name = session_name

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(
                f"Delete session [bold]{self.session_name}[/]?\n"
                "This removes the directory, transcripts, and audio."
            )
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Delete", id="confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class HistoryApp(App):
    CSS = """
    Screen { layout: vertical; }
    #panes { height: 1fr; }
    #sessions { width: 36; border-right: solid grey; }
    #transcript { padding: 0 1; }
    .line { padding: 0 0; }
    .line:hover { background: $boost; }
    .line.current { background: $primary 35%; text-style: bold; }
    .line.nav { background: $accent 40%; }
    .line.current.nav { background: $primary 55%; text-style: bold; }
    #status { height: 1; padding: 0 1; background: $surface; color: $text; }
    DataTable { height: 1fr; }
    """
    BINDINGS = [
        Binding("space", "toggle_play", "Play/pause"),
        Binding("enter", "select_line", "Seek", show=False),
        Binding("c", "center_playback", "Center"),
        Binding("left", "seek_back", "←/→ ±5s"),
        Binding("right", "seek_fwd", "+5s", show=False),
        Binding("h", "seek_back", "-5s", show=False),
        Binding("l", "seek_fwd", "+5s", show=False),
        Binding("j", "nav_down", "Down", show=False),
        Binding("k", "nav_up", "Up", show=False),
        Binding("g", "nav_top", "Top", show=False),
        Binding("G", "nav_bottom", "Bottom", show=False),
        Binding("ctrl+d", "page_down", "Page down", show=False),
        Binding("ctrl+u", "page_up", "Page up", show=False),
        Binding("d", "delete", "Delete"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        self.root = root or (data_dir() / "sessions")
        self.sessions: list[Session] = []
        self.current: Session | None = None
        self.mixer: StereoMixer | None = None
        self._lines: list[TranscriptLine] = []
        self._offsets: list[float] = []
        self._highlight_idx: int | None = None
        self._nav_idx: int | None = None
        self.title = "meeting copilot — history"

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="panes"):
            yield DataTable(id="sessions", cursor_type="row", zebra_stripes=True)
            yield VerticalScroll(id="transcript")
        yield Static("(no session loaded)", id="status")
        yield Footer()

    def on_mount(self) -> None:
        st = self.query_one("#sessions", DataTable)
        st.add_columns("Date", "Duration", "Lines")
        self._populate_sessions()
        st.focus()
        if self.sessions:
            self._load_session(self.sessions[0])
        else:
            self._set_status(f"no sessions in {self.root}")
        self.set_interval(0.1, self._tick)

    def _populate_sessions(self) -> None:
        st = self.query_one("#sessions", DataTable)
        st.clear()
        self.sessions = discover_sessions(self.root)
        for s in self.sessions:
            st.add_row(
                s.start.strftime("%Y-%m-%d %H:%M"),
                format_duration(s.duration_s) if s.has_audio else "—",
                str(s.line_count),
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row = event.cursor_row
        if row is not None and 0 <= row < len(self.sessions):
            self._load_session(self.sessions[row])

    def on_transcript_line_clicked(self, event: TranscriptLineClicked) -> None:
        self._seek_to_line(event.idx)

    def _seek_to_line(self, idx: int) -> None:
        """Seek to the START of line `idx`'s speech (≈ previous line's offset).
        Stored offsets are *finalization* timestamps (≥300ms after speech ends
        + network), so they point at the end of the utterance, not the start.
        """
        if self.mixer is None or not (0 <= idx < len(self._offsets)):
            return
        start = self._offsets[idx - 1] if idx > 0 else 0.0
        self.mixer.seek(start)
        if not self.mixer.playing:
            self.mixer.play()
        self._clear_nav()
        self._refresh_status()

    def _clear_nav(self) -> None:
        if self._nav_idx is not None and self._nav_idx < len(self._lines):
            self._lines[self._nav_idx].remove_class("nav")
        self._nav_idx = None

    def _load_session(self, s: Session) -> None:
        self.current = s
        if self.mixer is not None:
            self.mixer.close()
            self.mixer = None
        if s.has_audio:
            try:
                self.mixer = StereoMixer(s.dir / "you.wav", s.dir / "them.wav")
            except Exception as e:
                self.mixer = None
                self._set_status(f"audio load failed: {e}")
        scroll = self.query_one("#transcript", VerticalScroll)
        scroll.remove_children()
        self._lines = []
        self._offsets = []
        self._highlight_idx = None
        self._nav_idx = None
        jsonl = s.dir / "transcript.jsonl"
        session_t0 = s.start.timestamp()
        if jsonl.is_file():
            for raw in jsonl.read_text().splitlines():
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("ts")
                if ts is None:
                    continue
                offset = max(0.0, float(ts) - session_t0)
                line = TranscriptLine(
                    len(self._lines), format_clock(offset),
                    rec.get("speaker", "?"), rec.get("text", ""),
                )
                self._lines.append(line)
                self._offsets.append(offset)
        if self._lines:
            scroll.mount(*self._lines)
        else:
            scroll.mount(Static("[dim](no transcript)[/]"))
        scroll.scroll_home(animate=False)
        self._refresh_status()

    def action_toggle_play(self) -> None:
        if self.mixer is not None:
            self.mixer.toggle()
            self._refresh_status()

    def action_seek_back(self) -> None:
        if self.mixer is not None:
            self.mixer.seek(max(0.0, self.mixer.position - 5))
            self._refresh_status()

    def action_seek_fwd(self) -> None:
        if self.mixer is not None:
            self.mixer.seek(self.mixer.position + 5)
            self._refresh_status()

    @work
    async def action_delete(self) -> None:
        st = self.query_one("#sessions", DataTable)
        row = st.cursor_row
        if row is None or not (0 <= row < len(self.sessions)):
            return
        target = self.sessions[row]
        confirmed = await self.push_screen_wait(ConfirmDelete(target.dir.name))
        if not confirmed:
            return
        if self.current is target and self.mixer is not None:
            self.mixer.close()
            self.mixer = None
        try:
            shutil.rmtree(target.dir)
        except OSError as e:
            self._set_status(f"delete failed: {e}")
            return
        was_current = self.current is target
        self._populate_sessions()
        if not self.sessions:
            self.current = None
            scroll = self.query_one("#transcript", VerticalScroll)
            scroll.remove_children()
            self._lines = []
            self._offsets = []
            self._highlight_idx = None
            self._set_status(f"deleted {target.dir.name} · no sessions left")
            return
        new_row = min(row, len(self.sessions) - 1)
        st.move_cursor(row=new_row)
        if was_current:
            self._load_session(self.sessions[new_row])
        self._set_status(f"deleted {target.dir.name}")

    def _tick(self) -> None:
        if self.mixer is not None and self.mixer.playing:
            self._refresh_status()

    def _scroll(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    def _on_main_screen(self) -> bool:
        return len(self.screen_stack) <= 1

    def _focus_is_transcript(self) -> bool:
        f = self.focused
        return f is not None and f.id == "transcript"

    def action_nav_down(self) -> None:
        if not self._on_main_screen():
            return
        focused = self.focused
        if isinstance(focused, DataTable):
            focused.action_cursor_down()
        elif self._focus_is_transcript():
            self._move_nav(1)
        else:
            self._scroll().scroll_down(animate=False)

    def action_nav_up(self) -> None:
        if not self._on_main_screen():
            return
        focused = self.focused
        if isinstance(focused, DataTable):
            focused.action_cursor_up()
        elif self._focus_is_transcript():
            self._move_nav(-1)
        else:
            self._scroll().scroll_up(animate=False)

    def action_nav_top(self) -> None:
        if not self._on_main_screen():
            return
        focused = self.focused
        if isinstance(focused, DataTable):
            if focused.row_count:
                focused.move_cursor(row=0)
        elif self._focus_is_transcript() and self._lines:
            self._set_nav(0)
        else:
            self._scroll().scroll_home(animate=False)

    def action_nav_bottom(self) -> None:
        if not self._on_main_screen():
            return
        focused = self.focused
        if isinstance(focused, DataTable):
            if focused.row_count:
                focused.move_cursor(row=focused.row_count - 1)
        elif self._focus_is_transcript() and self._lines:
            self._set_nav(len(self._lines) - 1)
        else:
            self._scroll().scroll_end(animate=False)

    def action_page_down(self) -> None:
        if not self._on_main_screen():
            return
        focused = self.focused
        if isinstance(focused, DataTable):
            focused.action_page_down()
        elif self._focus_is_transcript() and self._lines:
            self._move_nav(10)
        else:
            self._scroll().scroll_page_down(animate=False)

    def action_page_up(self) -> None:
        if not self._on_main_screen():
            return
        focused = self.focused
        if isinstance(focused, DataTable):
            focused.action_page_up()
        elif self._focus_is_transcript() and self._lines:
            self._move_nav(-10)
        else:
            self._scroll().scroll_page_up(animate=False)

    def action_select_line(self) -> None:
        if not self._on_main_screen() or not self._focus_is_transcript():
            return
        if self._nav_idx is None:
            return
        self._seek_to_line(self._nav_idx)

    def action_center_playback(self) -> None:
        if self._highlight_idx is None:
            return
        self._clear_nav()
        self._lines[self._highlight_idx].scroll_visible(animate=False)

    def _move_nav(self, delta: int) -> None:
        if not self._lines:
            return
        if self._nav_idx is None:
            base = self._highlight_idx if self._highlight_idx is not None else 0
        else:
            base = self._nav_idx
        self._set_nav(max(0, min(len(self._lines) - 1, base + delta)))

    def _set_nav(self, idx: int) -> None:
        if not (0 <= idx < len(self._lines)):
            return
        if self._nav_idx is not None and self._nav_idx < len(self._lines):
            self._lines[self._nav_idx].remove_class("nav")
        self._nav_idx = idx
        line = self._lines[idx]
        line.add_class("nav")
        line.scroll_visible(animate=False)

    def _refresh_status(self) -> None:
        if self.mixer is None:
            if self.current is not None:
                self._set_status(f"{self.current.dir.name} · no audio")
            self._update_highlight(None)
            return
        glyph = "▶" if self.mixer.playing else "⏸"
        self._set_status(
            f"{glyph}  {format_clock(self.mixer.position)} / {format_clock(self.mixer.duration)}"
        )
        self._update_highlight(self.mixer.position)

    def _update_highlight(self, pos: float | None) -> None:
        # Each line's offset is when Deepgram *finalized* the utterance — ≥300ms
        # after the speaker stopped, plus network. So the line currently being
        # spoken is the next one to finalize: the first offset > pos.
        if pos is None or not self._offsets:
            idx: int | None = None
        else:
            i = bisect.bisect_right(self._offsets, pos)
            idx = i if i < len(self._offsets) else len(self._offsets) - 1
        if idx == self._highlight_idx:
            return
        if self._highlight_idx is not None and self._highlight_idx < len(self._lines):
            self._lines[self._highlight_idx].remove_class("current")
        if idx is not None:
            line = self._lines[idx]
            line.add_class("current")
            # Auto-scroll to playback only when the nav cursor hasn't drifted off.
            if self._nav_idx is None or self._nav_idx == idx:
                line.scroll_visible(animate=False)
        self._highlight_idx = idx

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def on_unmount(self) -> None:
        if self.mixer is not None:
            self.mixer.close()
