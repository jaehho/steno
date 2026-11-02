"""`claude -p`, the default provider, against a fake `claude`."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steno import claude, claudecode


def _fake_claude(bindir: Path, stdout: str) -> None:
    """A `claude` that saves its argv, env and stdin next to itself, then
    prints `stdout`."""
    script = bindir / "claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "here = os.path.dirname(os.path.abspath(__file__))\n"
        "json.dump({'argv': sys.argv[1:], 'stdin': sys.stdin.read(),\n"
        "           'key': os.environ.get('ANTHROPIC_API_KEY'),\n"
        "           'base': os.environ.get('ANTHROPIC_BASE_URL'), 'cwd': os.getcwd()},\n"
        "          open(os.path.join(here, 'call.json'), 'w'))\n"
        f"sys.stdout.write({stdout!r})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _call(bindir: Path) -> dict:
    return json.loads((bindir / "call.json").read_text())


def _argv(**kw) -> list[str]:
    return claudecode.build_argv(
        "claude", model="m", effort=kw.pop("effort", "high"), system="sys",
        output_format=kw.pop("output_format", "json"), **kw,
    )


def _after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


class TestArgv(unittest.TestCase):
    def test_without_a_root_there_are_no_tools(self):
        argv = _argv(output_format="stream-json")
        self.assertEqual(_after(argv, "--tools"), "")
        self.assertNotIn("--allowedTools", argv)

    def test_a_root_allows_reads_inside_it_only(self):
        root = Path(tempfile.mkdtemp())
        argv = _argv(read_root=root)
        self.assertEqual(_after(argv, "--tools"), "Read,Grep,Glob")
        self.assertEqual(_after(argv, "--allowedTools"), f"Read(/{root.resolve()}/**)")

    def test_meeting_notes_are_denied_where_named_and_where_linked(self):
        root = Path(tempfile.mkdtemp()).resolve()
        (root / "lab" / "notes" / "meetings").mkdir(parents=True)
        (root / "ergo" / "docs" / "meetings").mkdir(parents=True)
        (root / "ergo" / "notes").symlink_to("docs")
        (root / "thesis").mkdir()
        argv = _argv(read_root=root)
        denied = argv[argv.index("--disallowedTools") + 1:]
        denied = denied[: next((i for i, a in enumerate(denied) if a.startswith("--")),
                               len(denied))]
        self.assertIn(f"Read(/{root}/lab/notes/meetings/**)", denied)
        self.assertIn(f"Read(/{root}/ergo/notes/meetings/**)", denied)
        self.assertIn(f"Read(/{root}/ergo/docs/meetings/**)", denied)
        self.assertFalse(any("thesis" in rule for rule in denied))

    def test_effort_goes_with_the_call_and_none_sends_nothing(self):
        self.assertEqual(_after(_argv(), "--effort"), "high")
        self.assertNotIn("--effort", _argv(effort=""))

    def test_settings_hooks_and_mcp_are_off(self):
        argv = _argv()
        self.assertEqual(_after(argv, "--setting-sources"), "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--no-session-persistence", argv)

    def test_no_key_or_other_host_reaches_claude(self):
        env = {"ANTHROPIC_API_KEY": "sk-x", "ANTHROPIC_BASE_URL": "https://x",
               "ANTHROPIC_AUTH_TOKEN": "t", "HOME": "/home/u"}
        self.assertEqual(claudecode.child_env(env), {"HOME": "/home/u"})


class TestParsing(unittest.TestCase):
    def test_structured_output(self):
        out = json.dumps({"is_error": False, "structured_output": {"a": 1}})
        self.assertEqual(claudecode.parse_structured(out), {"a": 1})

    def test_error_result_raises(self):
        out = json.dumps({"is_error": True, "result": "not logged in"})
        with self.assertRaisesRegex(claudecode.ClaudeCodeError, "not logged in"):
            claudecode.parse_structured(out)

    def test_stream_lines(self):
        delta = {"type": "stream_event",
                 "event": {"delta": {"type": "text_delta", "text": "hi"}}}
        thinking = {"type": "stream_event",
                    "event": {"delta": {"type": "thinking_delta", "thinking": ""}}}
        self.assertEqual(claudecode.parse_stream_line(json.dumps(delta)), ("delta", "hi"))
        self.assertEqual(claudecode.parse_stream_line(json.dumps(thinking)), ("", ""))


class TestTheDefaultProvider(unittest.TestCase):
    """steno's own calls, with a fake `claude` first on PATH."""

    def setUp(self):
        self.bindir = Path(tempfile.mkdtemp())
        env = {"PATH": f"{self.bindir}:{os.environ['PATH']}",
               "ANTHROPIC_API_KEY": "sk-x", "STENO_PROVIDER": "", "STENO_EFFORT": ""}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_is_claude_code_on_sonnet_at_high_effort(self):
        self.assertEqual(claude.provider(), "claude-code")
        self.assertEqual(claude.default_model(), "claude-sonnet-5-5")
        self.assertEqual(claude.effort(), "high")
        self.assertEqual(asyncio.run(claude.list_models()), claude.presets())

    def test_a_summary_call_goes_through_claude_p(self):
        result = {"is_error": False, "structured_output": {"a": "x"}}
        _fake_claude(self.bindir, json.dumps(result))
        out = asyncio.run(claude.run_structured(
            "the transcript", system="s", model="claude-sonnet-5-5",
            schema={"type": "object"}))
        self.assertEqual(out, {"a": "x"})
        call = _call(self.bindir)
        self.assertEqual(call["stdin"], "the transcript")
        self.assertIsNone(call["key"])
        self.assertEqual(_after(call["argv"], "--effort"), "high")
        self.assertEqual(_after(call["argv"], "--model"), "claude-sonnet-5-5")

    def test_an_answer_streams(self):
        lines = [
            {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "a"}}},
            {"type": "result", "result": "a"},
        ]
        _fake_claude(self.bindir, "".join(json.dumps(x) + "\n" for x in lines))
        got: list[str] = []
        answer = asyncio.run(claude.stream_text(
            "q", system="s", model="m", on_delta=got.append))
        self.assertEqual((got, answer), (["a"], "a"))

    def test_its_failure_is_a_claude_error(self):
        _fake_claude(self.bindir, json.dumps({"is_error": True, "result": "logged out"}))
        with self.assertRaisesRegex(claude.ClaudeError, "logged out"):
            asyncio.run(claude.run_structured(
                "t", system="s", model="m", schema={"type": "object"}))


if __name__ == "__main__":
    unittest.main()
