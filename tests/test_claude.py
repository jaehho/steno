"""The `claude -p` wrapper and the summary's project inference, with a fake `claude`."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steno import claude
from steno.summarize import list_projects, load_project, summarize_session


def _fake_claude(bindir: Path, stdout: str) -> None:
    """A `claude` that saves its argv, env and stdin next to itself, then prints `stdout`."""
    script = bindir / "claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "here = os.path.dirname(os.path.abspath(__file__))\n"
        "json.dump({'argv': sys.argv[1:], 'stdin': sys.stdin.read(),\n"
        "           'key': os.environ.get('ANTHROPIC_API_KEY'), 'cwd': os.getcwd()},\n"
        "          open(os.path.join(here, 'call.json'), 'w'))\n"
        f"sys.stdout.write({stdout!r})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _call(bindir: Path) -> dict:
    return json.loads((bindir / "call.json").read_text())


class TestArgv(unittest.TestCase):
    def test_without_a_root_there_are_no_tools(self):
        argv = claude.build_argv("claude", "m", "sys", "stream-json")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", argv)

    def test_a_root_allows_reads_inside_it_only(self):
        root = Path(tempfile.mkdtemp())
        argv = claude.build_argv("claude", "m", "sys", "json", root)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], f"Read(/{root.resolve()}/**)")

    def test_settings_and_mcp_are_off(self):
        argv = claude.build_argv("claude", "m", "sys", "json")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", argv)

    def test_the_api_key_never_reaches_claude(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-x"}):
            self.assertNotIn("ANTHROPIC_API_KEY", claude.child_env())


class TestParsing(unittest.TestCase):
    def test_structured_output(self):
        out = json.dumps({"is_error": False, "structured_output": {"a": 1}})
        self.assertEqual(claude.parse_structured(out), {"a": 1})

    def test_error_result_raises(self):
        out = json.dumps({"is_error": True, "result": "not logged in"})
        with self.assertRaisesRegex(claude.ClaudeError, "not logged in"):
            claude.parse_structured(out)

    def test_garbage_raises_with_stderr(self):
        with self.assertRaisesRegex(claude.ClaudeError, "boom"):
            claude.parse_structured("", "boom")

    def test_stream_lines(self):
        delta = {"type": "stream_event",
                 "event": {"type": "content_block_delta",
                           "delta": {"type": "text_delta", "text": "hi"}}}
        thinking = {"type": "stream_event",
                    "event": {"delta": {"type": "thinking_delta", "thinking": ""}}}
        self.assertEqual(claude.parse_stream_line(json.dumps(delta)), ("delta", "hi"))
        self.assertEqual(claude.parse_stream_line(json.dumps(thinking)), ("", ""))
        self.assertEqual(
            claude.parse_stream_line(json.dumps({"type": "result", "result": "hi there"})),
            ("result", "hi there"))


class TestAgainstAFakeClaude(unittest.TestCase):
    def setUp(self):
        self.bindir = Path(tempfile.mkdtemp())
        env = {"PATH": f"{self.bindir}:{os.environ['PATH']}", "ANTHROPIC_API_KEY": "sk-x"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stream_text_hands_over_deltas(self):
        lines = [
            {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "a"}}},
            {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "b"}}},
            {"type": "result", "result": "ab"},
        ]
        _fake_claude(self.bindir, "".join(json.dumps(x) + "\n" for x in lines))
        got: list[str] = []
        answer = asyncio.run(claude.stream_text("q", system="s", model="m", on_delta=got.append))
        self.assertEqual((got, answer), (["a", "b"], "ab"))
        call = _call(self.bindir)
        self.assertEqual(call["stdin"], "q")
        self.assertIsNone(call["key"])

    def test_summary_records_a_known_project_and_reads_from_the_root(self):
        projects = Path(tempfile.mkdtemp())
        (projects / "thesis").mkdir()
        (projects / "thesis" / "README.md").write_text("# thesis\n\nA beamformer on an FPGA.\n")
        session = Path(tempfile.mkdtemp())
        (session / "transcript.jsonl").write_text(
            json.dumps({"ts": 1.0, "speaker": "them", "text": "the beamformer"}) + "\n")
        result = {"is_error": False, "structured_output": {
            "summary": "# Beamformer Review\n\n## TL;DR\n- x", "project": "thesis"}}
        _fake_claude(self.bindir, json.dumps(result))

        summary = asyncio.run(summarize_session(session, "m", None, projects))

        self.assertEqual((summary.title, summary.project), ("Beamformer Review", "thesis"))
        self.assertEqual(load_project(session), "thesis")
        call = _call(self.bindir)
        self.assertEqual(Path(call["cwd"]).resolve(), projects.resolve())
        system = call["argv"][call["argv"].index("--system-prompt") + 1]
        self.assertIn("- thesis: A beamformer on an FPGA.", system)

    def test_an_invented_project_is_dropped(self):
        projects = Path(tempfile.mkdtemp())
        (projects / "thesis").mkdir()
        session = Path(tempfile.mkdtemp())
        (session / "transcript.jsonl").write_text(
            json.dumps({"ts": 1.0, "speaker": "you", "text": "hi"}) + "\n")
        _fake_claude(self.bindir, json.dumps({"structured_output": {
            "summary": "# Hi", "project": "../../etc"}}))
        summary = asyncio.run(summarize_session(session, "m", None, projects))
        self.assertIsNone(summary.project)


class TestProjects(unittest.TestCase):
    def test_describes_each_directory_by_its_readme(self):
        root = Path(tempfile.mkdtemp())
        (root / "a").mkdir()
        (root / "a" / "README.md").write_text("# a\n\n![badge](x)\nDoes a thing.\n")
        (root / "b").mkdir()
        (root / ".hidden").mkdir()
        (root / "file.txt").write_text("")
        self.assertEqual(list_projects(root), {"a": "Does a thing.", "b": ""})

    def test_missing_root(self):
        self.assertEqual(list_projects(Path("/nonexistent/steno")), {})


if __name__ == "__main__":
    unittest.main()
