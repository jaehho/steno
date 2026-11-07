"""What `steno bar` prints, pinned for the programs that read it.

`contract/` at the top of the repo holds `bar.jsonl`, one line per
state as `steno bar --once` prints it, and `consumers.json`: who calls
what and which fields they read. A consumer builds its fake from these
lines (dotfiles' `docs/app-contract.md`, "The owner pins what consumers
read"), so changing a field one of them reads is changing what it was
promised: add the new field, move every consumer, then drop the old.

The tooltip and text are free to change; the fields a consumer reads
and the set of fields are not.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from steno import session, state
from steno.events import StatusChanged

CONTRACT = Path(__file__).resolve().parents[1] / "contract"

# What each line of bar.jsonl was printed from, in order: (status,
# detail, since) written by the app, or None for no app running.
PRINTED_FROM = (
    ("recording", state.MANUAL, 1000.0),
    ("recording", "Zoom", 1000.0),
    ("finalizing", "20261012-100000", 1000.0),
    ("paused", "", 5000.0),
    ("idle", "", 1000.0),
    None,
)
NOW = 1185.0


def sample_lines() -> list[dict]:
    text = (CONTRACT / "bar.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line]


def consumers() -> dict:
    return json.loads((CONTRACT / "consumers.json").read_text())


def read_fields() -> set[str]:
    return {
        field
        for c in consumers().values()
        for fields in c["reads"].values()
        for field in fields
    }


class TestBarSample(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def printed(self, written) -> dict:
        """What `steno bar --once` prints with `written` on disk."""
        with contextlib.suppress(FileNotFoundError):
            state.state_path().unlink()
        if written is not None:
            status, detail, since = written
            state.write_state(status, detail, since=since)
        return state.bar_json(state.read_state(), now=NOW)

    def test_every_line_is_what_steno_bar_prints(self):
        lines = sample_lines()
        self.assertEqual(len(lines), len(PRINTED_FROM))
        for line, written in zip(lines, PRINTED_FROM, strict=True):
            with self.subTest(written):
                got = self.printed(written)
                self.assertEqual(
                    sorted(got), sorted(line),
                    "a field was added or dropped; update bar.jsonl",
                )
                for key, value in got.items():
                    self.assertIs(type(value), type(line[key]), key)
                for key in read_fields():
                    self.assertEqual(got[key], line[key], key)

    def test_the_sample_shows_every_state(self):
        """A status renamed in the app shows here as a missing class."""
        classes = {line["class"] for line in sample_lines()}
        self.assertEqual(classes, set(state.ICONS))

    def test_every_consumer_finds_the_fields_it_reads(self):
        for who, c in consumers().items():
            for name, fields in c["reads"].items():
                self.assertEqual(name, "bar.jsonl", who)
                for line in sample_lines():
                    for field in fields:
                        self.assertIn(field, line, f"{who} reads {field}")

    def test_only_a_recording_you_started_is_manual(self):
        manual = [line["manual"] for line in sample_lines()]
        self.assertEqual(manual, [True] + [False] * (len(manual) - 1))


class TestManualReachesTheBar(unittest.TestCase):
    """The engine publishes a start you asked for under `state.MANUAL`,
    and a call it detected under the call app's name."""

    def started(self, begin) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        events: list = []
        engine = session.Engine(
            emit=events.append, root=Path(tmp.name),
            brief_path=Path(tmp.name) / "brief.md", model="m",
        )

        async def source():
            return "src"

        async def idle(*_args):
            return None

        wav = SimpleNamespace(close=lambda: None)
        with mock.patch.object(session, "capture_mic", source), \
             mock.patch.object(session, "default_sink_monitor", source), \
             mock.patch.object(session, "open_wav", lambda _p: wav), \
             mock.patch.object(engine, "_load_keyterms", lambda _a="": []), \
             mock.patch.object(engine, "_record_mic", idle), \
             mock.patch.object(engine, "_stream_them", idle), \
             mock.patch.object(engine, "_pump", idle):
            asyncio.run(begin(engine))
        self.addCleanup(engine.current.transcript_jsonl.close)
        (changed,) = [e for e in events if isinstance(e, StatusChanged)]
        published = {"status": changed.status, "detail": changed.detail}
        return state.bar_json(published)

    def test_a_start_you_asked_for(self):
        out = self.started(lambda e: e._on_command("start"))
        self.assertEqual(out["class"], "recording")
        self.assertIs(out["manual"], True)

    def test_a_call_the_detector_saw(self):
        call = SimpleNamespace(kind="start", app="ZOOM VoiceEngine")
        out = self.started(lambda e: e._on_detect(call))
        self.assertEqual(out["class"], "recording")
        self.assertIs(out["manual"], False)


if __name__ == "__main__":
    unittest.main()
