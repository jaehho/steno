"""A debrief: one voice recounting a meeting, transcribed here, summarized
as the meeting it describes."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from steno import claude, cli, debrief, realign, summarize, transcribe
from steno.cost import prerecorded_cost, stream_cost
from steno.summarize import (
    DEBRIEF_PROMPT,
    SYSTEM_PROMPT,
    estimated_cost,
    summarize_session,
    system_prompt_for,
)
from tests.test_localasr import TEST_MODEL, model_cached, speak, write_wav
from tests.test_summarize import FakeModel

STARTED_AT = 1_700_000_000.0

DEBRIEF_REPLY = """\
# Budget Planning Meeting

## Action items
**You**
- Write the draft [L0]

**Them**
- Sam sends the budget by Friday [L0]
"""


class TestSession(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_a_debrief_is_a_manual_session_of_its_own_kind(self):
        session = debrief.create_session(self.root, STARTED_AT)
        meta = json.loads((session / "meta.json").read_text())
        self.assertEqual(meta["kind"], "debrief")
        self.assertTrue(meta["manual"])
        self.assertTrue(meta["mic_pending"])
        # Not a room to hold for everyone's consent: it is one voice.
        self.assertIs(meta["room"], False)
        self.assertEqual(session.name, debrief.session_name(STARTED_AT))

    def test_it_never_writes_into_an_existing_session(self):
        debrief.create_session(self.root, STARTED_AT)
        with self.assertRaises(FileExistsError):
            debrief.create_session(self.root, STARTED_AT)

    def test_transcribing_writes_the_lines_and_says_which_engine(self):
        session = debrief.create_session(self.root, STARTED_AT)
        write_wav(session / "you.wav", b"\0\0" * 1600)
        rows = [{"ts": STARTED_AT + 2, "speaker": "you", "text": "hi",
                 "offset": 0.5}]
        fake = mock.AsyncMock(return_value=rows)
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(debrief, "transcribe_file", fake):
            os.environ.pop("STENO_ASR", None)
            lines = asyncio.run(debrief.transcribe(session, STARTED_AT))
        self.assertEqual(lines, 1)
        # Local unless STENO_ASR says otherwise.
        call = fake.await_args
        assert call is not None
        self.assertEqual(call.kwargs["backend"], "local")
        self.assertEqual(
            json.loads((session / "transcript.jsonl").read_text()), rows[0]
        )
        meta = json.loads((session / "meta.json").read_text())
        self.assertNotIn("mic_pending", meta)
        self.assertEqual(meta["asr"], "local:large-v3-turbo")


class TestRecord(unittest.TestCase):
    """The mic alone, until told to stop or out of time; no audio device."""

    def record(self, stop_after: float | None, max_s: float) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        dest = tmp / "you.wav"

        async def fake_record_only(source, wav):
            self.assertEqual(source, "mic-source")
            while True:
                wav.writeframes(b"\0\0" * 160)
                await asyncio.sleep(0.01)

        async def run():
            stop = asyncio.Event()
            if stop_after is not None:
                asyncio.get_running_loop().call_later(stop_after, stop.set)
            return await debrief.record(dest, stop, max_s)

        with mock.patch.object(debrief, "capture_mic",
                               mock.AsyncMock(return_value="mic-source")), \
             mock.patch.object(debrief, "record_only", fake_record_only):
            self.assertEqual(asyncio.run(run()), "mic-source")
        return dest

    def test_it_stops_when_told_and_leaves_a_readable_wav(self):
        dest = self.record(stop_after=0.1, max_s=30.0)
        self.assertGreater(debrief.wav_seconds(dest), 0.0)

    def test_a_forgotten_debrief_stops_at_its_limit(self):
        dest = self.record(stop_after=None, max_s=0.1)
        self.assertGreater(debrief.wav_seconds(dest), 0.0)


class TestStopWhenTold(unittest.TestCase):
    """Enter or the end of input stops a recording; nothing is left
    watching stdin afterwards. SIGTERM is TestRecordingProcess's."""

    def watch(self, stream, act) -> tuple[bool, bool]:
        async def run():
            stop = asyncio.Event()
            with debrief.stop_when_told(stop, stream) as enter_stops:
                act()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=2.0)
            loop = asyncio.get_running_loop()
            # Nothing is still watching the stream once it is done.
            if enter_stops:
                self.assertFalse(loop.remove_reader(stream.fileno()))
            return enter_stops, stop.is_set()

        return asyncio.run(run())

    def pipe(self):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "r")
        self.addCleanup(reader.close)

        def close_writer():
            with contextlib.suppress(OSError):  # a test may have closed it
                os.close(write_fd)

        self.addCleanup(close_writer)
        return reader, write_fd

    def test_enter_stops_it(self):
        reader, write_fd = self.pipe()
        self.assertEqual(
            self.watch(reader, lambda: os.write(write_fd, b"\n")),
            (True, True))

    def test_words_without_enter_do_not(self):
        reader, write_fd = self.pipe()
        self.assertEqual(
            self.watch(reader, lambda: os.write(write_fd, b"still talk")),
            (True, False))

    def test_the_end_of_input_stops_it(self):
        reader, write_fd = self.pipe()
        self.assertEqual(self.watch(reader, lambda: os.close(write_fd)),
                         (True, True))

    def test_a_file_cannot_stop_it(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "in").write_text("\n")
        with (tmp / "in").open() as stream:
            self.assertEqual(self.watch(stream, lambda: None), (False, False))

    def test_no_stdin_cannot_stop_it(self):
        self.assertEqual(self.watch(None, lambda: None), (False, False))


# `steno debrief`'s recording, in a process of its own with a fake mic and
# a stdin nobody types into: how it ends, and that the process then exits.
RECORD_CHILD = """
import asyncio, sys
from pathlib import Path
from unittest import mock
from steno import cli, debrief

async def fake_record_only(source, wav):
    while True:
        wav.writeframes(b"\\0\\0" * 160)
        await asyncio.sleep(0.01)

with mock.patch.object(debrief, "capture_mic",
                       mock.AsyncMock(return_value="fake-mic")), \\
     mock.patch.object(debrief, "record_only", fake_record_only):
    session, _ = cli._record_debrief(Path(sys.argv[1]), float(sys.argv[2]))
print("seconds", round(debrief.wav_seconds(session / debrief.TRACK), 1),
      flush=True)
"""


class TestRecordingProcess(unittest.TestCase):
    """The reader that waited for Enter in a thread kept a timed-out or
    interrupted debrief from transcribing for five minutes, and the
    process from exiting until Enter was pressed."""

    def run_child(self, max_s: float, send=None) -> tuple[str, float]:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        began = time.monotonic()
        child = subprocess.Popen(
            [sys.executable, "-c", RECORD_CHILD, str(root), str(max_s)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.addCleanup(child.kill)
        for pipe in (child.stdin, child.stdout, child.stderr):
            if pipe is not None:
                self.addCleanup(pipe.close)
        assert child.stdout is not None
        # Stdin stays open and empty: nobody presses Enter.
        ready, _, _ = select.select([child.stdout], [], [], 20)
        self.assertTrue(ready, "the debrief never started recording")
        self.assertIn("Debrief:", child.stdout.readline())
        if send is not None:
            time.sleep(0.3)
            child.send_signal(send)
        # Not communicate(): it closes stdin, which is an Enter of sorts.
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("the debrief process did not exit")
        took = time.monotonic() - began
        assert child.stderr is not None
        out, err = child.stdout.read(), child.stderr.read()
        self.assertEqual(child.returncode, 0, err)
        self.assertIn("recorded from", out)
        return out, took

    def test_the_time_limit_ends_it_and_the_process_exits(self):
        out, took = self.run_child(max_s=0.5)
        self.assertIn("seconds", out)
        self.assertLess(took, 25)

    def test_ctrl_c_ends_it_and_keeps_the_recording(self):
        out, _ = self.run_child(max_s=600, send=signal.SIGINT)
        self.assertIn("seconds", out)
        self.assertNotIn("seconds 0.0", out)

    def test_sigterm_ends_it_and_keeps_the_recording(self):
        out, _ = self.run_child(max_s=600, send=signal.SIGTERM)
        self.assertIn("recorded from fake-mic", out)
        self.assertNotIn("seconds 0.0", out)


class TestPrompt(unittest.TestCase):
    def test_a_call_keeps_the_prompt_it_had(self):
        self.assertEqual(system_prompt_for({}), SYSTEM_PROMPT)
        self.assertTrue(SYSTEM_PROMPT.startswith(
            "You summarize meeting transcripts. 'you' is the person wearing"
        ))
        self.assertIn("'them' is everyone else on the call.", SYSTEM_PROMPT)

    def test_a_debrief_is_told_whose_promises_are_whose(self):
        prompt = system_prompt_for({"kind": "debrief"})
        self.assertEqual(prompt, DEBRIEF_PROMPT)
        self.assertNotIn("everyone else on the call", prompt)
        self.assertIn("recounting from memory", prompt)
        self.assertIn("`**Them**`", prompt)
        # The rules after the frame are the call's, word for word.
        rules = SYSTEM_PROMPT.split("\n\n", 1)[1]
        self.assertTrue(prompt.endswith(rules))

    def test_summarizing_a_debrief_sends_the_debrief_prompt(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        session = debrief.create_session(root, STARTED_AT)
        (session / "transcript.jsonl").write_text(json.dumps({
            "ts": STARTED_AT + 5, "speaker": "you", "offset": 0.0,
            "text": "Sam said he would send the budget by Friday",
        }) + "\n")
        fake = FakeModel(DEBRIEF_REPLY)
        with mock.patch.object(claude, "run_structured", fake):
            summary = asyncio.run(summarize_session(
                session, "m", None, Path(tempfile.mkdtemp())
            ))
        self.assertTrue(fake.calls[0][1]["system"].startswith(DEBRIEF_PROMPT))
        self.assertEqual(summary.title, "Budget Planning Meeting")


class TestCost(unittest.TestCase):
    def test_a_debrief_done_here_costs_nothing(self):
        meta = {"kind": "debrief", "asr": "local:large-v3-turbo"}
        self.assertEqual(estimated_cost(meta, 5.0), 0.0)

    def test_a_call_costs_its_stream_and_its_batch(self):
        self.assertAlmostEqual(
            estimated_cost({}, 10.0), stream_cost(10.0) + prerecorded_cost(10.0)
        )

    def test_a_call_whose_side_was_done_here_costs_its_stream(self):
        meta = {"asr": "local:large-v3-turbo"}
        self.assertAlmostEqual(estimated_cost(meta, 10.0), stream_cost(10.0))


class TestRealignHere(unittest.TestCase):
    def test_a_local_plan_costs_nothing(self):
        plan = realign.Plan(Path("/x/a"), 30.0, False, per_minute=0.0)
        self.assertEqual(plan.cost, 0.0)
        self.assertIn("~$0.00", realign.format_plan([plan]).splitlines()[-1])

    def test_realign_runs_without_a_deepgram_key_when_local(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        session = root / "20260101-090000"
        session.mkdir()
        write_wav(session / "you.wav", b"\0\0" * 16000)
        (session / "transcript.jsonl").write_text(
            json.dumps({"ts": 1.0, "speaker": "you", "text": "old"}) + "\n")
        args = argparse.Namespace(session=str(session), all=False, force=False,
                                  dry_run=False, yes=True, dir=root)
        env = {k: v for k, v in os.environ.items() if k != "DEEPGRAM_API_KEY"}
        env["STENO_ASR"] = "local"
        fake = mock.AsyncMock(return_value=[
            {"ts": 3.0, "speaker": "you", "text": "new", "offset": 1.0}])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(cli, "load_dotenv"), \
             mock.patch.object(transcribe, "transcribe_file_local", fake), \
             redirect_stdout(out), redirect_stderr(err):
            cli.cmd_realign(args)
        self.assertIn("on this machine", out.getvalue())
        self.assertIn("~$0.00", out.getvalue())
        meta = json.loads((session / "meta.json").read_text())
        self.assertEqual(meta["asr"], "local:large-v3-turbo")
        self.assertTrue((session / "transcript.before-realign.jsonl").is_file())


@unittest.skipUnless(model_cached(TEST_MODEL) and shutil.which("espeak-ng"),
                     f"needs faster-whisper, the cached {TEST_MODEL}, espeak-ng")
class TestDebriefEndToEnd(unittest.TestCase):
    """A synthetic voice memo in, a debrief session with a summary out;
    a real local model, a stand-in for the summarizer."""

    def test_an_imported_memo_becomes_a_summarized_debrief(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        memo = speak("I just left the planning meeting. Sam said he would "
                     "send the budget by Friday. I said I would write the "
                     "draft.", tmp / "memo.wav")
        root = tmp / "sessions"
        args = argparse.Namespace(source=memo, max=60.0, no_summary=False,
                                  dir=root, brief=None)
        fake = FakeModel(DEBRIEF_REPLY)
        env = {"HF_HUB_OFFLINE": "1", "STENO_LOCAL_MODEL": TEST_MODEL,
               "STENO_LOCAL_DEVICE": "cpu", "STENO_LOCAL_THREADS": "2"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(cli, "load_dotenv"), \
             mock.patch.object(cli, "check_ready", return_value=None), \
             mock.patch.object(cli, "model_default", return_value="m"), \
             mock.patch.object(cli, "load_brief", return_value=None), \
             mock.patch.object(cli, "_notes_after"), \
             mock.patch.object(summarize, "projects_root",
                               return_value=tmp / "no-projects"), \
             mock.patch.object(claude, "run_structured", fake), \
             redirect_stdout(out):
            cli.cmd_debrief(args)
        (session,) = [p for p in root.iterdir() if p.is_dir()]
        meta = json.loads((session / "meta.json").read_text())
        self.assertEqual(meta["kind"], "debrief")
        self.assertEqual(meta["asr"], f"local:{TEST_MODEL}")
        self.assertEqual(meta["cost_usd"], 0.0)
        self.assertEqual(meta["title"], "Budget Planning Meeting")
        lines = [json.loads(x) for x in
                 (session / "transcript.jsonl").read_text().splitlines()]
        self.assertTrue(lines)
        self.assertTrue(all(r["speaker"] == "you" for r in lines))
        heard = " ".join(r["text"] for r in lines).lower()
        self.assertIn("friday", heard)
        prompt, kwargs = fake.calls[0]
        self.assertTrue(kwargs["system"].startswith(DEBRIEF_PROMPT))
        self.assertIn("you:", prompt)
        self.assertFalse((root / f".import-{os.getpid()}.wav").exists())


if __name__ == "__main__":
    unittest.main()
