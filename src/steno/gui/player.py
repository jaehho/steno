"""Playing back a recorded meeting.

GStreamer directly rather than `Gtk.MediaFile`: GTK's media backend is a
separate, frequently-absent package on Linux, and a playback feature that
silently does nothing on a normal desktop is worse than none.

The pipeline mixes every track it is given onto one timeline:

    filesrc -> wavparse -> audioconvert -> volume ┐
                                                  ├-> audiomixer -> sink
    filesrc -> wavparse -> audioconvert -> volume ┘

A meeting was one conversation and is played back as one, rather than as two
recordings of the same room that have to be listened to in turn. The two files
were opened within a frame of each other and written from the same graph clock,
so mixing them needs no alignment — the offsets that place a transcript line in
one track place it in the other.

Each side keeps its own `volume` element, so one can be turned down to hear
past it without rebuilding anything. The pipeline itself is built on first
play, so the window costs nothing to open for someone who never presses it.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")

from gi.repository import GLib, Gst

TICK_MS = 200

_initialized = False


def _ensure_gst() -> bool:
    global _initialized
    if not _initialized:
        try:
            # Names the PulseAudio stream. Without it the stream is called
            # `python3`, so its saved volume is shared with every other Python
            # program on the machine — and inherited from them.
            GLib.set_application_name("Steno")
            GLib.set_prgname("steno")
            Gst.init(None)
            _initialized = True
        except Exception:  # noqa: BLE001 — no GStreamer: playback is off, not fatal
            return False
    return True


def _make(kind: str, pipeline=None):
    element = Gst.ElementFactory.make(kind, None)
    if element is not None and pipeline is not None:
        pipeline.add(element)
    return element


class Player:
    """A set of tracks played as one, seekable, reporting position on a timer.

    `on_tick(position, duration, playing)` runs on the GTK thread — GStreamer's
    bus watch and our timer both live on the main loop, so nothing here needs
    the thread care the engine bridge does.
    """

    def __init__(self, on_tick: Callable[[float, float, bool], None]) -> None:
        self.on_tick = on_tick
        self.tracks: dict[str, Path] = {}
        self._pipeline = None
        self._volumes: dict[str, object] = {}
        self._sink = None
        self._timer: int | None = None
        self._pending_seek: float | None = None
        self._duration = 0.0
        self._levels: dict[str, float] = {}

    # ------------------------------------------------------------------ control

    def available(self) -> bool:
        return _ensure_gst()

    def play(self, tracks: Mapping[str, Path], seconds: float = 0.0) -> None:
        """Play `tracks` together from `seconds`, rebuilding if the set changed."""
        wanted = {
            name: path for name, path in tracks.items() if Path(path).is_file()
        }
        if not wanted or not _ensure_gst():
            return
        if wanted != self.tracks:
            self._build(wanted)
        pipeline = self._pipeline
        if pipeline is None:
            return
        self._unmute()
        if self._pending_seek is None:
            # Already prerolled: seek now. Otherwise the bus does it, since a
            # pipeline that has not prerolled cannot seek yet.
            pipeline.set_state(Gst.State.PLAYING)
            self._seek(seconds)
        else:
            self._pending_seek = seconds
            pipeline.set_state(Gst.State.PLAYING)
        self._start_timer()

    def set_levels(self, levels: Mapping[str, float]) -> None:
        """Set each side's volume, 0.0 to 1.0. Remembered across rebuilds."""
        self._levels = dict(levels)
        for name, element in self._volumes.items():
            element.set_property("volume", float(self._levels.get(name, 1.0)))

    def toggle(self) -> None:
        pipeline = self._pipeline
        if pipeline is None or not self.tracks:
            return
        if self.playing:
            pipeline.set_state(Gst.State.PAUSED)
        else:
            # Pressing play on a finished meeting should replay it, not sit at
            # the end doing nothing.
            duration = self.duration
            if duration and self.position >= duration - 0.5:
                self._seek(0.0)
            self._unmute()
            pipeline.set_state(Gst.State.PLAYING)
            self._start_timer()
        self._tick()

    def stop(self) -> None:
        """Release the audio device. Called whenever the window goes away."""
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
        self._pipeline = None
        self._volumes = {}
        self._sink = None
        self.tracks = {}
        self._duration = 0.0
        self._pending_seek = None
        self._stop_timer()

    def nudge(self, seconds: float) -> None:
        self._seek(max(0.0, self.position + seconds))

    def seek(self, seconds: float) -> None:
        self._seek(seconds)

    # ---------------------------------------------------------------- pipeline

    def _build(self, tracks: dict[str, Path]) -> None:
        """Assemble a mixer with one branch per track.

        Built element by element rather than through `parse_launch`, so a
        session directory with a quote or a backslash in its path is a path and
        not a syntax error.
        """
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
        self._pipeline = None
        self._volumes = {}
        self.tracks = {}

        pipeline = Gst.Pipeline.new("steno-player")
        mixer = _make("audiomixer", pipeline)
        convert = _make("audioconvert", pipeline)
        resample = _make("audioresample", pipeline)
        # Explicitly the Pulse sink where there is one: it carries a per-stream
        # volume that the audio server remembers, and `autoaudiosink` hides that
        # behind a bin we would have to reach through to set it.
        sink = _make("pulsesink", pipeline) or _make("autoaudiosink", pipeline)
        if None in (mixer, convert, resample, sink):
            return
        if not (mixer.link(convert) and convert.link(resample)
                and resample.link(sink)):
            return

        for name, path in tracks.items():
            source = _make("filesrc", pipeline)
            parser = _make("wavparse", pipeline)
            branch_convert = _make("audioconvert", pipeline)
            # Sum in float. Two 16-bit tracks that each peak near half scale can
            # land on the same syllable, and integer mixing would clip it.
            floats = _make("capsfilter", pipeline)
            volume = _make("volume", pipeline)
            if None in (source, parser, branch_convert, floats, volume):
                return
            source.set_property("location", str(path))
            floats.set_property("caps", Gst.Caps.from_string("audio/x-raw,format=F32LE"))
            if not (source.link(parser) and parser.link(branch_convert)
                    and branch_convert.link(floats) and floats.link(volume)
                    and volume.link(mixer)):
                return
            self._volumes[name] = volume

        bus = pipeline.get_bus()
        if bus is not None:
            bus.add_signal_watch()
            bus.connect("message::eos", self._on_eos)
            bus.connect("message::error", self._on_error)
            bus.connect("message::async-done", self._on_async_done)

        self._pipeline = pipeline
        self._sink = sink
        self.tracks = dict(tracks)
        self._duration = 0.0
        self._pending_seek = 0.0
        self.set_levels(self._levels or {name: 1.0 for name in tracks})

    def _unmute(self) -> None:
        """Play at full volume, every time.

        The audio server remembers a volume per application and restores it on
        the next stream, so a track once turned down — or muted by anything
        holding the same stream name — stays silent forever after. There is no
        master volume control in this window to explain that with, and "I
        clicked a line and heard nothing" is indistinguishable from a broken
        feature. The per-track levels are ours and are left alone.
        """
        if self._sink is None:
            return
        for prop, value in (("mute", False), ("volume", 1.0)):
            try:
                self._sink.set_property(prop, value)
            except (TypeError, AttributeError):
                pass

    def _seek(self, seconds: float) -> None:
        pipeline = self._pipeline
        if pipeline is None:
            return
        pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            int(max(0.0, seconds) * Gst.SECOND),
        )

    # -------------------------------------------------------------------- state

    @property
    def path(self) -> Path | None:
        """Something is loaded. Kept for callers that only ask that much."""
        return next(iter(self.tracks.values()), None)

    @property
    def playing(self) -> bool:
        if self._pipeline is None:
            return False
        return self._pipeline.get_state(0)[1] == Gst.State.PLAYING

    @property
    def position(self) -> float:
        if self._pipeline is None:
            return 0.0
        ok, pos = self._pipeline.query_position(Gst.Format.TIME)
        return pos / Gst.SECOND if ok and pos >= 0 else 0.0

    @property
    def duration(self) -> float:
        if self._pipeline is None:
            return 0.0
        if self._duration <= 0:
            ok, dur = self._pipeline.query_duration(Gst.Format.TIME)
            if ok and dur > 0:
                self._duration = dur / Gst.SECOND
        return self._duration

    # -------------------------------------------------------------------- pumps

    def _start_timer(self) -> None:
        if self._timer is None:
            self._timer = GLib.timeout_add(TICK_MS, self._tick_repeat)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None

    def _tick_repeat(self) -> bool:
        self._tick()
        return GLib.SOURCE_CONTINUE

    def _tick(self) -> None:
        self.on_tick(self.position, self.duration, self.playing)

    # ----------------------------------------------------------------- bus hooks

    def _on_async_done(self, _bus, _msg) -> None:
        if self._pending_seek is not None:
            seconds, self._pending_seek = self._pending_seek, None
            if seconds > 0:
                self._seek(seconds)

    def _on_eos(self, _bus, _msg) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.PAUSED)
        self._stop_timer()
        self._tick()

    def _on_error(self, _bus, msg) -> None:
        error, _debug = msg.parse_error()
        print(f"[steno] playback: {error.message}", flush=True)
        self.stop()
        self._tick()
