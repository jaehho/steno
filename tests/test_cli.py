"""CLI output helpers."""
from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from steno import cli
from steno.cli import _parse_duration, _replace_line, _split_csv


class TestReplaceLine(unittest.TestCase):
    def _rendered(self, text: str, previous: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            _replace_line(text, previous)
        return buf.getvalue().rstrip("\n")

    def test_short_replacement_covers_the_longer_line(self):
        """A bare \\r left the tail of the old line visible — a title once
        picked up the end of the model name it was overwriting."""
        previous = "  20260717-095917: summarizing with claude-opus-4-7..."
        out = self._rendered("  20260717-095917: Audio Check", previous)
        self.assertGreaterEqual(len(out) - 1, len(previous))
        self.assertNotIn("-4-7", out)

    def test_longer_replacement_is_not_padded(self):
        out = self._rendered("a much longer line than before", "short")
        self.assertEqual(out, "\ra much longer line than before")

    def test_starts_with_carriage_return(self):
        self.assertTrue(self._rendered("x", "y").startswith("\r"))

    def test_no_carriage_return_when_nothing_to_overwrite(self):
        """Into a pipe there is no progress line, so no cursor games either."""
        self.assertEqual(self._rendered("done", ""), "done")


class TestParseDuration(unittest.TestCase):
    def test_units(self):
        self.assertEqual(_parse_duration("90s"), 90)
        self.assertEqual(_parse_duration("45m"), 2700)
        self.assertEqual(_parse_duration("2h"), 7200)

    def test_bare_number_is_minutes(self):
        self.assertEqual(_parse_duration("30"), 1800)

    def test_bad_input_exits(self):
        with self.assertRaises(SystemExit):
            _parse_duration("soon")


class TestSplitCsv(unittest.TestCase):
    def test_splits_and_strips(self):
        self.assertEqual(_split_csv(" zoom , discord "), ("zoom", "discord"))

    def test_empty(self):
        self.assertEqual(_split_csv(None), ())
        self.assertEqual(_split_csv(" , "), ())


class TestMainDispatch(unittest.TestCase):
    """What README shows must work: `steno --allow zoom` is the window."""

    def run_main(self, *argv: str):
        seen = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = {"XDG_CONFIG_HOME": tmp.name, "XDG_DATA_HOME": tmp.name}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(sys, "argv", ["steno", *argv]), \
             mock.patch.object(cli, "cmd_gui", seen.append):
            cli.main()
        return seen[0]

    def test_bare_flags_are_the_windows(self):
        args = self.run_main("--allow", "zoom,my-app")
        self.assertEqual(args.allow, "zoom,my-app")

    def test_bare_any_app(self):
        self.assertTrue(self.run_main("--any-app").any_app)

    def test_bare_steno_is_the_window(self):
        self.assertEqual(self.run_main().cmd, "gui")

    def test_top_level_brief_survives(self):
        args = self.run_main("--brief", "/x/brief.md", "--any-app")
        self.assertEqual(args.brief, Path("/x/brief.md"))

    def test_unknown_flag_after_a_subcommand_is_an_error(self):
        with mock.patch.object(sys, "argv", ["steno", "status", "--bogus"]), \
             redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main()


class TestPause(unittest.TestCase):
    def pause(self, duration: str):
        with mock.patch.object(cli, "set_pause") as set_pause, \
             mock.patch.object(cli, "activate") as activate, \
             redirect_stdout(io.StringIO()):
            cli.cmd_pause(argparse.Namespace(off=False, duration=duration))
        return set_pause.call_args.args[0], activate.call_args.args[1]

    def test_seconds_round_up_to_a_minute_not_zero(self):
        """Zero minutes is `resume` to the running app."""
        self.assertEqual(self.pause("30s"), (60, [1]))

    def test_ninety_seconds_is_two_minutes(self):
        self.assertEqual(self.pause("90s"), (120, [2]))

    def test_the_file_and_the_app_agree(self):
        seconds, minutes = self.pause("45m")
        self.assertEqual((seconds, minutes), (2700, [45]))


class TestDeleteSafety(unittest.TestCase):
    def test_the_live_recording_is_not_deleted(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        live = Path(tmp.name) / "20261010-120000"
        live.mkdir()
        (live / "transcript.jsonl").write_text("{}\n")
        args = argparse.Namespace(session=None, dir=Path(tmp.name), yes=True, permanent=False)
        err = io.StringIO()
        with mock.patch.object(cli, "read_state", return_value={"status": "recording"}), \
             mock.patch.object(cli, "delete_session") as delete, \
             redirect_stderr(err), self.assertRaises(SystemExit):
            cli.cmd_delete(args)
        delete.assert_not_called()
        self.assertIn("being recorded", err.getvalue())

    def test_a_finished_meeting_goes_and_takes_its_asst_tasks(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        done = Path(tmp.name) / "20261010-120000"
        done.mkdir()
        (done / "transcript.jsonl").write_text("{}\n")
        args = argparse.Namespace(session=None, dir=Path(tmp.name), yes=True, permanent=False)
        with mock.patch.object(cli, "read_state", return_value={"status": "idle"}), \
             mock.patch.object(cli, "forget_asst", return_value=2) as forget, \
             mock.patch.object(cli, "delete_session", return_value=("trashed", "/t")), \
             redirect_stdout(io.StringIO()):
            cli.cmd_delete(args)
        forget.assert_called_once()
