"""A debrief: one voice recounting a meeting, transcribed here, summarized
as the meeting it describes."""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import tempfile
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
