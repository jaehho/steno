"""The seam between the engine's asyncio loop and GTK's main loop.

GTK and asyncio each want to own the thread they run on, so the engine gets its
own. Everything crossing back — every transcript line, every status change —
goes through `GLib.idle_add`, which is the one documented thread-safe way into
GTK. Nothing in `steno` outside this package imports `gi`, and nothing
in this file touches a widget: the callback it hands out runs on the GTK thread,
and what that callback does is the window's business.
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from gi.repository import GLib

from ..events import Event
from ..session import Engine


class EngineBridge:
    """Owns the engine, its thread, and its loop.

    `on_event` is invoked on the GTK main thread, one event per call.
    """

    def __init__(
        self,
        on_event: Callable[[Event], None],
        **engine_kwargs: Any,
    ) -> None:
        self._on_event = on_event
        self._engine_kwargs = engine_kwargs
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._ready = threading.Event()
        self.engine: Engine | None = None

    # ------------------------------------------------------------------ startup

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="steno-engine", daemon=True
        )
        self._thread.start()
        # The window asks the engine to do things as soon as it is shown, so
        # don't hand back a bridge whose engine doesn't exist yet.
        self._ready.wait(timeout=5)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        engine = Engine(emit=self._emit, **self._engine_kwargs)
        self.engine = engine
        # Cancelling this task is how the loop is asked to end. Stopping the
        # loop directly would abandon `run_until_complete` mid-flight and raise.
        self._task = loop.create_task(engine.run())
        self._ready.set()
        try:
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                _cancel_all(loop)
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    def _emit(self, event: Event) -> None:
        """Called on the engine thread; hop to GTK before anyone sees it."""
        GLib.idle_add(self._deliver, event, priority=GLib.PRIORITY_DEFAULT)

    def _deliver(self, event: Event) -> bool:
        self._on_event(event)
        return GLib.SOURCE_REMOVE

    # ----------------------------------------------------------------- commands

    def submit(self, coro) -> Future | None:
        """Run a coroutine on the engine loop from the GTK thread."""
        if self._loop is None or not self._loop.is_running():
            return None
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self, timeout: float = 30.0) -> None:
        """Stop the engine, giving an in-flight meeting time to finalize.

        A meeting that is ending has a batch transcription and a summary still
        to run, and losing those loses the entire point of having recorded —
        so this waits rather than killing the loop.
        """
        loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            return
        engine = self.engine
        if engine is not None and engine.current is not None:
            engine.stop_now()
            deadline = timeout
            while engine.current is not None and deadline > 0:
                thread.join(0.1)
                deadline -= 0.1
        task = self._task
        if task is not None:
            loop.call_soon_threadsafe(task.cancel)
        thread.join(timeout=5)
        self._thread = None


def _cancel_all(loop: asyncio.AbstractEventLoop) -> None:
    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
