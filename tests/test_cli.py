"""CLI output helpers."""
from __future__ import annotations

import argparse
import errno
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from steno import asstlink, cli, projectnotes
from steno.cli import _parse_duration, _replace_line, _split_csv
from steno.notes import Todo, load_todos, save_todos, sync_todos
from steno.realign import Plan
from steno.summarize import (
    Summary,
    is_archived,
    load_meta,
    set_archived,
    update_meta,
)
from tests.fakeasst import FakeAsst


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

    def test_a_flag_value_that_names_a_command_is_still_a_value(self):
        """`--allow pause` is the app called pause, not the pause command."""
        args = self.run_main("--allow", "pause")
        self.assertEqual(args.allow, "pause")

    def test_brief_path_named_like_a_command(self):
        args = self.run_main("--brief", "show", "--any-app")
        self.assertEqual(args.brief, Path("show"))

    def test_the_retired_commands_say_where_they_went(self):
        """One line and a failure, whatever follows the word: it used to
        fall through to the window's parser and complain about --allow."""
        for argv, said in (
            (["publish"], "steno publish was renamed: "
                          "use steno project-notes\n"),
            (["publish", "--all"], "steno publish was renamed: "
                                   "use steno project-notes\n"),
            (["unpublish", "20260928-143000"],
             "steno unpublish was renamed: "
             "use steno project-notes --remove NAME\n"),
        ):
            with self.subTest(argv=argv), \
                 mock.patch.object(sys, "argv", ["steno", *argv]), \
                 mock.patch.object(cli, "cmd_gui") as window, \
                 redirect_stderr(io.StringIO()) as err, \
                 self.assertRaises(SystemExit) as raised:
                cli.main()
            self.assertEqual(err.getvalue(), said)
            self.assertNotEqual(raised.exception.code, 0)
            window.assert_not_called()

    def test_an_app_called_publish_is_still_an_app(self):
        args = self.run_main("--allow", "publish")
        self.assertEqual(args.allow, "publish")

    def test_unknown_flag_after_a_subcommand_is_an_error(self):
        with mock.patch.object(sys, "argv", ["steno", "status", "--bogus"]), \
             redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main()


class TestPreflight(unittest.TestCase):
    """With no terminal, a refusal to start has to reach the desktop."""

    def run_preflight(self, tty: bool):
        calls = []

        class Err(io.StringIO):
            def isatty(self):
                return tty

        def record(argv, **_kw):
            calls.append(argv)

        with mock.patch.dict(os.environ, {"DEEPGRAM_API_KEY": ""}), \
             mock.patch.object(cli.shutil, "which", return_value="/usr/bin/notify-send"), \
             mock.patch.object(cli.subprocess, "run", side_effect=record), \
             redirect_stderr(Err()), self.assertRaises(SystemExit):
            cli._preflight()
        return calls

    def test_no_terminal_notifies(self):
        calls = self.run_preflight(tty=False)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "notify-send")
        self.assertIn("DEEPGRAM_API_KEY", calls[0][-1])

    def test_a_terminal_only_prints(self):
        self.assertEqual(self.run_preflight(tty=True), [])


class TestToggle(unittest.TestCase):
    """A record key that did nothing has to say so, with no terminal too."""

    def toggle(self, reached: bool, tty: bool):
        calls = []

        class Err(io.StringIO):
            def isatty(self):
                return tty

        def record(argv, **_kw):
            calls.append(argv)

        code = None
        with mock.patch.object(cli, "toggle_record", return_value=reached), \
             mock.patch.object(cli.shutil, "which", return_value="/usr/bin/notify-send"), \
             mock.patch.object(cli.subprocess, "run", side_effect=record), \
             redirect_stderr(Err()):
            try:
                cli.cmd_toggle(argparse.Namespace())
            except SystemExit as exit_:
                code = exit_.code
        return code, calls

    def test_no_listener_and_no_terminal_notifies(self):
        code, calls = self.toggle(reached=False, tty=False)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "notify-send")
        self.assertIn("Could not start", calls[0][-1])

    def test_no_listener_on_a_terminal_only_prints(self):
        self.assertEqual(self.toggle(reached=False, tty=True), (1, []))

    def test_a_toggle_that_landed_says_nothing(self):
        self.assertEqual(self.toggle(reached=True, tty=False), (None, []))


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
        with mock.patch.object(cli, "load_dotenv"), \
             mock.patch.object(cli, "read_state", return_value={"status": "recording"}), \
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
        with mock.patch.object(cli, "load_dotenv"), \
             mock.patch.object(cli, "read_state", return_value={"status": "idle"}), \
             mock.patch.object(cli, "forget_asst", return_value=2) as forget, \
             mock.patch.object(cli, "delete_session", return_value=("trashed", "/t")), \
             redirect_stdout(io.StringIO()):
            cli.cmd_delete(args)
        forget.assert_called_once()


class TestSummarizeUncited(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # Two from before citations, one with them, one that was asked
        # and came back without, one with no summary at all.
        self.old = self.session("20260801-090000", "- x\n", title="Kickoff",
                                duration_min=42.0)
        self.newer = self.session("20260901-090000", "- y\n")
        self.session("20260902-090000", "- z <!--cite 5-->\n")
        self.session("20260903-090000", "- w\n",
                     cites={"items": 1, "cited": 0})
        self.session("20260904-090000", None)

    def session(self, name: str, body: str | None, **meta) -> Path:
        folder = self.root / name
        folder.mkdir()
        (folder / "transcript.jsonl").write_text("{}\n")
        (folder / "meta.json").write_text(json.dumps(meta))
        if body is not None:
            (folder / "summary.md").write_text(f"# T\n\n## TL;DR\n{body}")
        return folder

    def run_cmd(self, yes=False, session=None, all_=False, redone=True):
        args = argparse.Namespace(
            session=session, all=all_, force=False, uncited=True, yes=yes,
            dir=self.root,
        )
        out, err = io.StringIO(), io.StringIO()
        self.code = None
        with mock.patch.object(cli, "load_dotenv"), \
             mock.patch.object(cli, "check_ready", return_value=None), \
             mock.patch.object(cli, "model_default", return_value="m"), \
             mock.patch.object(cli, "load_brief", return_value=None), \
             mock.patch.object(cli, "_summarize_one",
                               return_value=redone) as one, \
             redirect_stdout(out), redirect_stderr(err):
            try:
                cli.cmd_summarize(args)
            except SystemExit as e:
                self.code = e.code
        self.one, self.out, self.err = one, out.getvalue(), err.getvalue()

    def test_without_yes_it_lists_and_prices_and_does_nothing(self):
        self.run_cmd()
        self.one.assert_not_called()
        self.assertIn("2 summaries without citations", self.out)
        self.assertIn("20260801-090000  Kickoff  (42 min)", self.out)
        self.assertIn("about $0.02", self.out)
        self.assertIn("--yes", self.out)
        for left_alone in ("20260902", "20260903", "20260904"):
            self.assertNotIn(left_alone, self.out)

    def test_newest_comes_first(self):
        self.run_cmd()
        self.assertLess(self.out.index("20260901"), self.out.index("20260801"))

    def test_with_yes_it_redoes_those_and_only_those(self):
        self.run_cmd(yes=True)
        self.assertEqual([c.args[0] for c in self.one.call_args_list],
                         [self.newer, self.old])
        self.assertIn("2/2 summarized", self.out)
        self.assertIsNone(self.code)

    def test_a_failure_is_an_error_exit(self):
        self.run_cmd(yes=True, redone=False)
        self.assertEqual(self.code, 1)

    def test_nothing_to_redo_says_so(self):
        for folder in (self.old, self.newer):
            (folder / "summary.md").write_text("# T\n\n- x <!--cite 1-->\n")
        self.run_cmd(yes=True)
        self.one.assert_not_called()
        self.assertIn("already has citations", self.out)

    def test_it_chooses_its_own_sessions(self):
        for given in ({"session": "20260801-090000"}, {"all_": True}):
            with self.subTest(**given):
                self.run_cmd(yes=True, **given)
                self.one.assert_not_called()
                self.assertEqual(self.code, 1)
                self.assertIn("chooses the sessions itself", self.err)


class TestExportCommand(unittest.TestCase):
    """`steno export` leaves the app: a citation becomes a stamp."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        folder = self.root / "20260801-090000"
        folder.mkdir()
        rows = [{"ts": 1000.0 + 30 * n, "speaker": "them", "text": f"row {n}"}
                for n in range(4)]
        (folder / "transcript.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows)
        )
        (folder / "meta.json").write_text(
            json.dumps({"started_at": 1000.0, "title": "Kickoff"})
        )
        (folder / "summary.md").write_text(
            "# Kickoff\n\n## Decisions\n- Ship in March <!--cite 30,90-->\n"
        )

    def export(self, output: Path | None = None) -> tuple[str, str]:
        args = argparse.Namespace(session=None, output=output, dir=self.root)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_export(args)
        return out.getvalue(), (output.read_text() if output else "")

    def test_printed_it_has_stamps_and_no_marker(self):
        printed, _ = self.export()
        self.assertIn("- Ship in March [00:30, 01:30]\n", printed)
        self.assertIn("[00:30] them: row 1", printed)
        self.assertNotIn("cite", printed)

    def test_saved_it_is_the_same_page(self):
        printed, _ = self.export()
        target = self.root / "out.md"
        said, saved = self.export(target)
        self.assertEqual(said, f"wrote {target}\n")
        self.assertEqual(saved, printed)


class TestTodosCommand(unittest.TestCase):
    def test_listing_todos_files_nothing(self):
        """`steno todos` reads asst. It used to file every meeting's
        unfiled items on the way past (#19)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        meeting = Path(tmp.name) / "20260801-090000"
        meeting.mkdir()
        (meeting / "transcript.jsonl").write_text("{}\n")
        (meeting / "meta.json").write_text("{}")
        save_todos(meeting, [Todo(text="Send the report", owner="you")])
        calls: list[list[str]] = []

        def fake_asst(args: list[str]) -> str:
            calls.append(args)
            return "[]"

        out = io.StringIO()
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "_run", side_effect=fake_asst), \
             redirect_stdout(out):
            cli.cmd_todos(argparse.Namespace(dir=Path(tmp.name), all=False))
        self.assertIn("Send the report", out.getvalue())
        self.assertIn(["ls"], calls)  # it did ask asst
        self.assertEqual([a for a in calls if a[0] == "add"], [])


def _meeting(root: Path, name: str, summary: str | None = "- x\n",
             **meta) -> Path:
    """A finished meeting: a transcript, a meta.json, maybe a summary."""
    folder = root / name
    folder.mkdir()
    (folder / "transcript.jsonl").write_text(
        json.dumps({"ts": 1000.0, "speaker": "them", "text": "hi"}) + "\n"
    )
    (folder / "meta.json").write_text(json.dumps(meta))
    if summary is not None:
        (folder / "summary.md").write_text(f"# T\n\n## TL;DR\n{summary}")
    return folder


class ArchiveCase(unittest.TestCase):
    """Three meetings, the middle one archived, in a tmp sessions dir."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.old = _meeting(self.root, "20260801-090000", title="Kickoff")
        self.away = _meeting(self.root, "20260901-090000", summary=None,
                             title="Standup")
        self.new = _meeting(self.root, "20261001-090000", summary=None)
        set_archived(self.away, True)

    def run_cmd(self, command, args, state="idle"):
        out, err = io.StringIO(), io.StringIO()
        self.code = None
        with mock.patch.object(cli, "load_dotenv"), \
             mock.patch.object(cli, "check_ready", return_value=None), \
             mock.patch.object(cli, "model_default", return_value="m"), \
             mock.patch.object(cli, "load_brief", return_value=None), \
             mock.patch.object(cli, "read_state",
                               return_value={"status": state}), \
             redirect_stdout(out), redirect_stderr(err):
            try:
                command(args)
            except SystemExit as e:
                self.code = e.code
        self.out, self.err = out.getvalue(), err.getvalue()


class TestArchiveCommands(ArchiveCase):
    def archive(self, *names, unarchive=False):
        args = argparse.Namespace(session=list(names), dir=self.root)
        command = cli.cmd_unarchive if unarchive else cli.cmd_archive
        self.run_cmd(command, args)

    def test_it_puts_a_meeting_away_and_names_it(self):
        self.archive(self.old.name)
        self.assertTrue(is_archived(self.old))
        self.assertIn(f"archived: Kickoff  ({self.old.name})", self.out)
        self.assertIsNone(self.code)

    def test_several_names_in_one_go(self):
        self.archive(self.old.name, self.new.name)
        self.assertTrue(is_archived(self.old))
        self.assertTrue(is_archived(self.new))

    def test_unarchive_brings_it_back_with_its_meta(self):
        self.archive(self.away.name, unarchive=True)
        self.assertFalse(is_archived(self.away))
        self.assertEqual(load_meta(self.away), {"title": "Standup"})
        self.assertIn(f"unarchived: Standup  ({self.away.name})", self.out)

    def test_doing_it_again_says_so_and_is_not_an_error(self):
        self.archive(self.away.name)
        self.assertIn("already archived: Standup", self.out)
        self.assertIsNone(self.code)
        self.archive(self.old.name, unarchive=True)
        self.assertIn("already unarchived: Kickoff", self.out)
        self.assertIsNone(self.code)

    def test_an_unknown_name_fails_but_the_others_still_go(self):
        self.archive("nope", self.old.name)
        self.assertTrue(is_archived(self.old))
        self.assertIn("no such session: nope", self.err)
        self.assertEqual(self.code, 1)

    def test_a_directory_that_is_not_a_meeting_is_left_alone(self):
        """A path resolves to any directory, and archiving writes into it."""
        stray = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, stray)
        self.archive(str(stray))
        self.assertFalse((stray / "meta.json").exists())
        self.assertIn("not a meeting", self.err)
        self.assertEqual(self.code, 1)

    def test_the_meeting_being_recorded_is_not_put_away(self):
        args = argparse.Namespace(
            session=[self.new.name, self.old.name], dir=self.root
        )
        self.run_cmd(cli.cmd_archive, args, state="recording")
        self.assertFalse(is_archived(self.new))
        self.assertIn("is being recorded; stop it first", self.err)
        self.assertTrue(is_archived(self.old))
        self.assertEqual(self.code, 1)

    def test_both_take_several_names_from_the_command_line(self):
        for command in ("archive", "unarchive"):
            argv = ["steno", command, "a", "b"]
            with self.subTest(command), \
                 mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(cli, "migrate_legacy", return_value=[]), \
                 mock.patch.object(cli, f"cmd_{command}") as run:
                cli.main()
            self.assertEqual(run.call_args.args[0].session, ["a", "b"])


class TestStatusCountsArchived(ArchiveCase):
    def status(self):
        data = self.root.parent / "xdg"
        sessions = data / "steno" / "sessions"
        sessions.parent.mkdir(parents=True, exist_ok=True)
        if not sessions.exists():
            sessions.symlink_to(self.root, target_is_directory=True)
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data)}), \
             mock.patch.object(cli, "paused_until", return_value=None):
            self.run_cmd(cli.cmd_status, argparse.Namespace())
        shutil.rmtree(data)
        return self.out

    def test_it_says_how_many_are_put_away(self):
        out = self.status()
        self.assertIn("sessions: 3 in", out)
        self.assertIn("  1 archived", out)

    def test_an_archived_meeting_is_not_one_to_catch_up(self):
        """`summarize --all` would not summarize it."""
        self.assertIn("  1 without a summary", self.status())

    def test_nothing_archived_adds_no_line(self):
        set_archived(self.away, False)
        out = self.status()
        self.assertNotIn("archived", out)
        self.assertIn("  2 without a summary", out)


class TestBulkCommandsSkipArchived(ArchiveCase):
    def summarize(self, **kw):
        args = argparse.Namespace(**{
            "session": None, "all": False, "force": False,
            "uncited": False, "yes": False, "dir": self.root, **kw,
        })
        with mock.patch.object(cli, "_summarize_one",
                               return_value=True) as one:
            self.run_cmd(cli.cmd_summarize, args)
        self.one = one
        return [c.args[0].name for c in one.call_args_list]

    def test_summarize_all_leaves_it_out_and_says_so(self):
        self.assertEqual(self.summarize(all=True), [self.new.name,
                                                    self.old.name])
        self.assertIn("1 archived meeting left out", self.out)
        self.assertIn("`steno unarchive NAME`", self.out)

    def test_summarize_all_says_nothing_when_nothing_is_archived(self):
        set_archived(self.away, False)
        self.assertEqual(len(self.summarize(all=True)), 3)
        self.assertNotIn("archived", self.out)

    def test_summarize_all_forced_leaves_it_out_too(self):
        self.assertNotIn(self.away.name, self.summarize(all=True, force=True))

    def test_naming_it_summarizes_it(self):
        self.assertEqual(self.summarize(session=self.away.name),
                         [self.away.name])
        self.assertNotIn("left out", self.out)

    def test_uncited_neither_lists_nor_prices_it(self):
        _meeting(self.root, "20260915-090000", summary="- y\n")
        gone = _meeting(self.root, "20261101-090000", summary="- z\n")
        set_archived(gone, True)
        self.summarize(uncited=True)
        self.one.assert_not_called()
        self.assertIn("2 summaries without citations", self.out)
        self.assertNotIn("20261101", self.out)
        self.assertIn("1 archived meeting left out", self.out)

    def test_uncited_with_only_archived_ones_has_nothing_to_do(self):
        set_archived(self.old, True)
        self.summarize(uncited=True, yes=True)
        self.one.assert_not_called()
        self.assertIn("every summary already has citations", self.out)

    def test_realign_all_leaves_it_out(self):
        seen = []

        def plan(session_dir):
            seen.append(session_dir.name)
            return Plan(session_dir, 1.0, False)

        args = argparse.Namespace(
            session=None, all=True, force=True, dry_run=True, yes=False,
            dir=self.root,
        )
        with mock.patch.dict(os.environ, {"DEEPGRAM_API_KEY": "k"}), \
             mock.patch.object(cli, "load_dotenv"), \
             mock.patch.object(cli, "has_audio", return_value=True), \
             mock.patch.object(cli, "plan", side_effect=plan), \
             mock.patch.object(cli, "format_plan", return_value="plans"):
            self.run_cmd(cli.cmd_realign, args)
        self.assertEqual(seen, [self.new.name, self.old.name])
        self.assertIn("1 archived meeting left out", self.out)

    def test_clean_echo_all_leaves_it_out(self):
        args = argparse.Namespace(
            session=None, all=True, force=False, no_suppress=False,
            dir=self.root,
        )
        self.run_cmd(cli.cmd_clean_echo, args)
        self.assertIn(f"{self.old.name}: no audio", self.out)
        self.assertNotIn(self.away.name, self.out)

    def test_the_seam_a_later_bulk_command_goes_through(self):
        """`steno project-notes --all` takes its sessions from here too."""
        everything = [self.new, self.away, self.old]
        with redirect_stdout(io.StringIO()) as out:
            kept = cli._leave_out_archived(everything)
        self.assertEqual(kept, [self.new, self.old])
        self.assertIn("1 archived meeting left out", out.getvalue())


class TestArchiveClosesTodos(ArchiveCase):
    """`steno archive` takes the meeting's open tasks out of asst, and
    `steno unarchive` files them again. The asst here is
    tests/fakeasst.py."""

    def setUp(self):
        super().setUp()
        self.asst = FakeAsst().start()
        self.addCleanup(self.asst.stop)
        sync_todos(self.old, "## Action items\n**You**\n"
                             "- Send the report\n- Draft the table\n")

    def archive(self, unarchive=False):
        args = argparse.Namespace(session=[self.old.name], dir=self.root)
        self.run_cmd(cli.cmd_unarchive if unarchive else cli.cmd_archive, args)

    def test_it_says_what_it_took_out_of_asst(self):
        self.assertEqual(len(self.asst.tasks()), 2)
        self.archive()
        self.assertIn(f"archived: Kickoff  ({self.old.name})", self.out)
        self.assertIn("  removed 2 open tasks from asst", self.out)
        self.assertEqual(self.asst.tasks(), [])
        self.assertTrue(all(t.dropped for t in load_todos(self.old)))
        self.assertIsNone(self.code)

    def test_a_refused_removal_still_archives_and_exits_1(self):
        self.asst.down = True
        self.archive()
        self.assertTrue(is_archived(self.old))
        self.assertNotIn("removed", self.out)
        self.assertIn("could not remove 2 open tasks from asst", self.err)
        self.assertEqual(self.code, 1)
        self.assertFalse(any(t.dropped for t in load_todos(self.old)))

        # Archiving it again, with asst back, is the retry.
        self.asst.down = False
        self.archive()
        self.assertIn("already archived: Kickoff", self.out)
        self.assertIn("  removed 2 open tasks from asst", self.out)
        self.assertIsNone(self.code)
        self.assertEqual(self.asst.tasks(), [])

    def test_unarchive_files_them_again(self):
        self.archive()
        self.archive(unarchive=True)
        self.assertIn(f"unarchived: Kickoff  ({self.old.name})", self.out)
        self.assertIn("  filed 2 open tasks in asst again", self.out)
        self.assertEqual(len(self.asst.tasks()), 2)
        self.assertTrue(all(t.open for t in load_todos(self.old)))

        self.archive(unarchive=True)
        self.assertIn("already unarchived", self.out)
        self.assertNotIn("filed", self.out)
        self.assertEqual(len(self.asst.tasks()), 2)

    def test_unarchive_says_what_asst_would_not_take(self):
        self.archive()
        self.asst.down = True
        self.archive(unarchive=True)
        self.assertFalse(is_archived(self.old))
        self.assertIn("asst did not take 2 open tasks", self.err)
        self.assertEqual(self.code, 1)

    def test_archiving_and_unarchiving_again_files_what_asst_refused(self):
        """What the message above tells you to do. Unarchiving twice
        does nothing: it reopens what archiving closed, and no more."""
        self.archive()
        self.asst.down = True
        self.archive(unarchive=True)
        self.assertIn("Archive and unarchive it again to retry", self.err)
        self.asst.down = False
        self.archive(unarchive=True)
        self.assertIn("already unarchived", self.out)
        self.assertEqual(self.asst.tasks(), [])

        self.archive()
        self.archive(unarchive=True)
        self.assertIn("  filed 2 open tasks in asst again", self.out)
        self.assertEqual(len(self.asst.tasks()), 2)
        self.assertIsNone(self.code)

    def test_unarchive_does_not_retry_a_removal_asst_refused(self):
        """It leaves those rows open and their tasks where they are;
        archiving again is what removes them (above)."""
        self.asst.down = True
        self.archive()
        self.asst.down = False
        kept = self.asst.tasks()
        self.assertEqual(len(kept), 2)

        self.archive(unarchive=True)
        self.assertEqual(self.asst.tasks(), kept)
        self.assertNotIn("removed", self.out)
        self.assertNotIn("filed", self.out)
        self.assertTrue(all(t.open for t in load_todos(self.old)))
        self.assertIsNone(self.code)


class TestNamedCommandsStillReachIt(ArchiveCase):
    def test_export_by_name_writes_the_meeting(self):
        args = argparse.Namespace(
            session=self.away.name, output=None, dir=self.root
        )
        self.run_cmd(cli.cmd_export, args)
        self.assertIsNone(self.code)
        self.assertIn("# Standup", self.out)

    def test_todos_leaves_it_out_and_says_so(self):
        """Its to-dos were closed when it was put away."""
        save_todos(self.away, [Todo(text="Send the report", owner="you")])
        with mock.patch.object(asstlink, "available", lambda: False):
            self.run_cmd(cli.cmd_todos, argparse.Namespace(
                dir=self.root, all=False
            ))
        self.assertNotIn("Standup", self.out)
        self.assertNotIn("Send the report", self.out)
        self.assertIn("1 archived meeting left out", self.out)

    def test_summarizing_it_counts_none_of_its_closed_to_dos(self):
        save_todos(self.away, [
            Todo(text="Send the report", owner="you", dropped=True),
        ])
        body = "## Action items\n**You**\n- Send the report\n"

        async def fake(session_dir, _model, _brief):
            (session_dir / "summary.md").write_text(f"# Standup\n\n{body}")
            return Summary("Standup", body)

        out = io.StringIO()
        with mock.patch.object(cli, "summarize_session", fake), \
             redirect_stdout(out):
            cli._summarize_one(self.away, "m", None)
        self.assertIn("summary.md", out.getvalue())
        self.assertNotIn("todo", out.getvalue())
        self.assertTrue(load_todos(self.away)[0].dropped)

    def test_todos_never_lists_a_row_closed_with_a_meeting(self):
        """Not by default, and not with --all, which adds the ticked."""
        save_todos(self.new, [
            Todo(text="Send the report", owner="you", dropped=True),
            Todo(text="Book the room", owner="you", done=True),
            Todo(text="Draft the table", owner="you"),
        ])
        with mock.patch.object(asstlink, "available", lambda: False):
            self.run_cmd(cli.cmd_todos, argparse.Namespace(
                dir=self.root, all=False
            ))
            plain = self.out
            self.run_cmd(cli.cmd_todos, argparse.Namespace(
                dir=self.root, all=True
            ))
        self.assertIn("[ ] Draft the table", plain)
        self.assertNotIn("Book the room", plain)
        self.assertNotIn("Send the report", plain)
        self.assertIn("[ ] Draft the table", self.out)
        self.assertIn("[x] Book the room", self.out)
        self.assertNotIn("Send the report", self.out)

    def test_todos_lists_it_again_once_it_is_back(self):
        save_todos(self.away, [Todo(text="Send the report", owner="you")])
        self.run_cmd(cli.cmd_archive, argparse.Namespace(
            session=[self.away.name], dir=self.root
        ))
        self.run_cmd(cli.cmd_unarchive, argparse.Namespace(
            session=[self.away.name], dir=self.root
        ))
        with mock.patch.object(asstlink, "available", lambda: False):
            self.run_cmd(cli.cmd_todos, argparse.Namespace(
                dir=self.root, all=False
            ))
        self.assertIn("[ ] Send the report", self.out)


def _tree(root: Path) -> dict[str, tuple]:
    """Every path under `root` with its bytes and time, to see that a run
    wrote nothing."""
    return {
        str(p.relative_to(root)):
            (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else ()
        for p in sorted(root.rglob("*"))
    }


class ProjectNotesCase(unittest.TestCase):
    """Meetings in a tmp sessions dir and projects dir; the projects
    root is the tmp one, never ~/projects."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.root = base / "sessions"
        self.root.mkdir()
        self.projects = base / "projects"
        for name in ("demo", "other"):
            (self.projects / name).mkdir(parents=True)
        env = mock.patch.dict(
            os.environ, {"STENO_PROJECTS_DIR": str(self.projects)}
        )
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(projectnotes.SWITCH_ENV, None)

        self.old = _meeting(self.root, "20260801-090000", title="Kickoff",
                            project="demo")
        self.new = _meeting(self.root, "20261001-090000", title="Standup",
                            project="demo")
        self.elsewhere = _meeting(self.root, "20260901-090000",
                                  title="Review", project="other")
        self.loose = _meeting(self.root, "20260915-090000", title="Chat")
        self.away = _meeting(self.root, "20260820-090000", title="Retro",
                             project="demo")
        set_archived(self.away, True)
        self.state = "idle"

    def meetings(self, project: str = "demo") -> Path:
        return self.projects / project / "notes" / "meetings"

    def run_cmd(self, command, **given):
        args = argparse.Namespace(**{
            "session": [], "all": False, "project": None,
            "dry_run": False, "force": False, "remove": False,
            "dir": self.root, **given,
        })
        out, err = io.StringIO(), io.StringIO()
        self.code = None
        with mock.patch.object(cli, "load_dotenv"), \
             mock.patch.object(cli, "read_state",
                               return_value={"status": self.state}), \
             redirect_stdout(out), redirect_stderr(err):
            try:
                command(args)
            except SystemExit as e:
                self.code = e.code
        self.out, self.err = out.getvalue(), err.getvalue()

    def notes(self, *names, **given):
        """`steno project-notes NAME...` with the options given."""
        self.run_cmd(cli.cmd_project_notes, session=list(names), **given)


class TestWritingProjectNotes(ProjectNotesCase):
    def test_a_named_meeting_goes_into_its_project(self):
        self.notes(self.old.name)
        self.assertIsNone(self.code)
        self.assertEqual(
            sorted(p.name for p in self.meetings().iterdir()),
            [".gitignore", "2026-08-01-0900-kickoff.md",
             "2026-08-01-0900-kickoff.transcript.md", "README.md"],
        )
        self.assertIn("20260801-090000  Kickoff  [demo]", self.out)
        self.assertIn("create  demo/notes/meetings/2026-08-01-0900-kickoff.md",
                      self.out)
        self.assertFalse(self.meetings("other").exists())

    def test_without_a_name_it_is_the_latest_meeting(self):
        self.notes()
        self.assertIn("20261001-090000  Standup", self.out)

    def test_a_dry_run_lists_what_it_would_do_and_writes_nothing(self):
        before = _tree(self.projects)
        meta = (self.old / "meta.json").read_text()
        self.notes(self.old.name, dry_run=True)
        self.assertEqual(_tree(self.projects), before)
        self.assertEqual((self.old / "meta.json").read_text(), meta)
        self.assertIn("dry run, nothing written: 4 to create", self.out)
        dry = self.out
        self.notes(self.old.name)
        for line in dry.splitlines():
            if line.startswith("  create"):
                self.assertIn(line, self.out)

    def test_again_it_writes_nothing_and_says_it_is_up_to_date(self):
        self.notes(self.old.name)
        before = _tree(self.projects)
        self.notes(self.old.name)
        self.assertEqual(_tree(self.projects), before)
        self.assertIn("up to date", self.out)
        self.assertIsNone(self.code)

    def test_all_goes_through_the_filed_meetings_but_the_archived(self):
        self.notes(all=True)
        self.assertEqual(
            sorted(p.name for p in self.meetings().glob("*-*.md")
                   if "transcript" not in p.name),
            ["2026-08-01-0900-kickoff.md", "2026-10-01-0900-standup.md"],
        )
        self.assertEqual(len(list(self.meetings("other").glob("*-*.md"))), 2)
        self.assertIn("1 archived meeting left out", self.out)
        self.assertNotIn("Retro", self.out)
        self.assertNotIn("Chat", self.out)
        self.assertIn("3 meetings looked at", self.out)
        self.assertIsNone(self.code)

    def test_all_dry_run_shows_the_lot_before_anything_is_written(self):
        before = _tree(self.projects)
        self.notes(all=True, dry_run=True)
        self.assertEqual(_tree(self.projects), before)
        self.assertIn("create", self.out)
        self.assertIn("dry run, nothing written", self.out)

    def test_a_dry_run_over_all_says_what_the_real_run_then_does(self):
        """A project's index and .gitignore are made by its first meeting
        and only added to by the rest: told once, in both runs."""
        self.notes(all=True, dry_run=True)
        dry = self.out
        self.notes(all=True)
        self.assertEqual(dry.splitlines()[:-1], self.out.splitlines()[:-1])
        self.assertIn("10 to create", dry)
        for told in (".gitignore", "README.md"):
            self.assertEqual(self.out.count(f"demo/notes/meetings/{told}"), 1)
        self.assertIn("10 created", self.out)

    def test_a_project_limits_all_to_that_project(self):
        self.notes(all=True, project="other")
        self.assertFalse(self.meetings().exists())
        self.assertTrue(self.meetings("other").is_dir())
        self.assertIn("1 meetings looked at", self.out)

    def test_a_project_the_named_meeting_is_not_in_does_nothing(self):
        self.notes(self.old.name, project="other")
        self.assertEqual(self.code, 1)
        self.assertIn("filed under demo, not other", self.err)
        self.assertFalse(self.meetings().exists())

    def test_a_meeting_with_no_project_has_nowhere_to_go(self):
        self.notes(self.loose.name)
        self.assertEqual(self.code, 1)
        self.assertIn("not filed under a project", self.out)

    def test_a_name_and_all_together_are_refused(self):
        self.notes(self.old.name, all=True)
        self.assertEqual(self.code, 1)
        self.assertFalse(self.meetings().exists())

    def test_two_names_are_refused(self):
        self.notes(self.old.name, self.new.name)
        self.assertEqual(self.code, 1)
        self.assertIn("name one meeting", self.err)
        self.assertFalse(self.meetings().exists())

    def test_switched_off_it_refuses_and_force_does_it_by_hand(self):
        os.environ[projectnotes.SWITCH_ENV] = "0"
        self.notes(self.old.name)
        self.assertEqual(self.code, 1)
        self.assertIn("--force", self.err)
        self.assertFalse(self.meetings().exists())
        self.notes(self.old.name, force=True)
        self.assertIsNone(self.code)
        self.assertTrue(self.meetings().is_dir())

    def test_the_meeting_being_recorded_is_not_written(self):
        self.state = "recording"
        self.notes(self.new.name)
        self.assertEqual(self.code, 1)
        self.assertIn("being recorded", self.err)
        self.assertFalse(self.meetings().exists())
        self.notes(all=True)
        self.assertNotIn("Standup", self.out)

    def test_a_page_that_is_not_steno_s_is_left_and_named(self):
        self.meetings().mkdir(parents=True)
        mine = self.meetings() / "2026-08-01-0900-kickoff.md"
        mine.write_text("my own page\n")
        self.notes(self.old.name)
        self.assertEqual(mine.read_text(), "my own page\n")
        self.assertIn("skip", self.out)
        self.assertIn("not written by steno", self.out)

    def test_a_file_in_the_way_of_each_meeting_is_said_once_and_not_logged(self):
        self.meetings().mkdir(parents=True)
        (self.meetings() / "README.md").write_text("my index\n")
        with self.assertNoLogs("steno.projectnotes", "WARNING"):
            self.notes(all=True)
        self.assertEqual(self.out.count("skip    demo/notes/meetings/README.md"),
                         1)
        self.assertEqual((self.meetings() / "README.md").read_text(),
                         "my index\n")

    def test_a_project_that_is_not_a_directory_is_said_not_made(self):
        shutil.rmtree(self.projects / "demo")
        self.notes(self.old.name)
        self.assertIn("no such project", self.out)
        self.assertFalse((self.projects / "demo").exists())


class TestRemovingProjectNotes(ProjectNotesCase):
    def test_it_takes_the_pages_and_their_index_line_away(self):
        self.notes(all=True)
        self.notes(self.old.name, remove=True)
        self.assertIsNone(self.code)
        self.assertEqual(
            [p.name for p in self.meetings().glob("*kickoff*")], []
        )
        self.assertIn("remove  demo/notes/meetings/2026-08-01-0900-kickoff.md",
                      self.out)
        index = (self.meetings() / "README.md").read_text()
        self.assertNotIn("Kickoff", index)
        self.assertIn("Standup", index)

    def test_it_works_on_an_archived_meeting_which_all_leaves_alone(self):
        self.notes(self.away.name)
        self.assertTrue(self.meetings().is_dir())
        self.notes(self.away.name, remove=True)
        self.assertFalse(self.meetings().exists())

    def test_a_dry_run_removes_nothing(self):
        self.notes(self.old.name)
        before = _tree(self.projects)
        self.notes(self.old.name, remove=True, dry_run=True)
        self.assertEqual(_tree(self.projects), before)
        self.assertIn("remove", self.out)

    def test_a_meeting_with_no_pages_in_a_project_says_so(self):
        self.notes(self.old.name, remove=True)
        self.assertIsNone(self.code)
        self.assertIn("no pages in a project", self.out)

    def test_an_unknown_name_fails_but_the_others_still_go(self):
        self.notes(self.old.name)
        self.notes("nope", self.old.name, remove=True)
        self.assertEqual(self.code, 1)
        self.assertFalse(self.meetings().exists())

    def test_it_needs_a_name(self):
        self.notes(remove=True)
        self.assertEqual(self.code, 1)
        self.assertIn("--remove needs", self.err)

    def test_the_options_that_choose_what_to_write_are_refused(self):
        """`--all` ignored would leave the folder as it was while the
        person thinks it was cleared."""
        self.notes(all=True)
        before = _tree(self.projects)
        for given in ({"all": True}, {"project": "demo"}, {"force": True}):
            with self.subTest(**given):
                self.notes(self.old.name, remove=True, **given)
                self.assertEqual(self.code, 1)
                self.assertIn("--remove takes meeting names only", self.err)
                self.assertEqual(_tree(self.projects), before)

    def test_it_takes_several_names_from_the_command_line(self):
        argv = ["steno", "project-notes", "--remove", "a", "b"]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(cli, "cmd_project_notes") as run:
            cli.main()
        args = run.call_args.args[0]
        self.assertEqual(args.session, ["a", "b"])
        self.assertTrue(args.remove)


class TestProjectNotesFollowOtherCommands(ProjectNotesCase):
    def summarize(self, session_dir: Path, project: str | None):
        """`steno summarize` with the model stood in for: what the real
        one leaves on disk is a summary and a title in meta.json."""
        async def fake(session_dir, _model, _brief):
            body = "## TL;DR\n- Ship it\n\n## Action items\n- Send it\n"
            (session_dir / "summary.md").write_text(f"# New\n\n{body}")
            update_meta(session_dir, title="New", project=project)
            return Summary("New", body, project)

        out = io.StringIO()
        with mock.patch.object(cli, "summarize_session", fake), \
             redirect_stdout(out), redirect_stderr(io.StringIO()):
            cli._summarize_one(session_dir, "m", None)
        return out.getvalue()

    def test_a_summary_with_no_project_goes_nowhere(self):
        out = self.summarize(self.loose, None)
        self.assertEqual(list(self.projects.rglob("notes")), [])
        self.assertNotIn("project notes", out)

    def test_a_summary_with_a_project_writes_the_pages_and_says_so(self):
        out = self.summarize(self.loose, "demo")
        self.assertIn("-> demo/notes/meetings: 4 files changed", out)
        page = self.meetings() / "2026-09-15-0900-new.md"
        self.assertIn("- [ ] Send it", page.read_text())

    def test_summarizing_again_replaces_the_pages_under_its_new_title(self):
        self.notes(self.old.name)
        self.summarize(self.old, "demo")
        self.assertEqual(
            sorted(p.name for p in self.meetings().glob("2026-08-*")),
            ["2026-08-01-0900-new.md", "2026-08-01-0900-new.transcript.md"],
        )
        self.assertIn("Ship it", (self.meetings() / "2026-08-01-0900-new.md")
                      .read_text())

    def test_realigning_a_meeting_in_a_project_writes_its_pages_again(self):
        self.notes(self.old.name)
        (self.old / "transcript.jsonl").write_text(json.dumps(
            {"ts": 1000.0, "speaker": "them", "text": "hello again"}) + "\n")
        self.realign(self.old)
        words = next(self.meetings().glob("*kickoff.transcript.md"))
        self.assertIn("hello again", words.read_text())

    def test_realigning_a_meeting_with_no_pages_writes_none(self):
        self.realign(self.old)
        self.assertFalse(self.meetings().exists())

    def realign(self, session_dir: Path) -> None:
        args = argparse.Namespace(
            session=session_dir.name, all=False, force=True, dry_run=False,
            yes=True, dir=self.root,
        )
        with mock.patch.dict(os.environ, {"DEEPGRAM_API_KEY": "k"}), \
             mock.patch.object(cli, "load_dotenv"), \
             mock.patch.object(cli, "plan",
                               return_value=Plan(session_dir, 1.0, False)), \
             mock.patch.object(cli, "format_plan", return_value="plans"), \
             mock.patch.object(cli, "realign_session",
                               mock.AsyncMock(return_value=1)), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            cli.cmd_realign(args)


class TestDeleteTakesTheCopyWithIt(ProjectNotesCase):
    def delete(self, outcome: str, session_dir: Path | None = None):
        session_dir = session_dir or self.old
        seen = {}

        def fake_delete(_folder, permanent=False):  # noqa: ARG001
            seen["pages"] = sorted(p.name for p in self.meetings().glob(
                "*kickoff*"))
            return (outcome, Path("/t") if outcome == "trashed" else None)

        with mock.patch.object(cli, "forget_asst", return_value=0), \
             mock.patch.object(cli, "delete_session", fake_delete):
            self.run_cmd(cli.cmd_delete, session=session_dir.name, yes=True,
                         permanent=False)
        return seen["pages"]

    def test_the_pages_are_gone_before_the_meeting_is(self):
        self.notes(self.old.name)
        self.assertEqual(self.delete("trashed"), [])
        self.assertIn("taken out of demo's notes:", self.out)
        self.assertIn("remove", self.out)
        self.assertIn("moved to trash", self.out)
        self.assertEqual(
            [p.name for p in self.meetings().glob("*kickoff*")], []
        )

    def test_a_meeting_that_could_not_be_deleted_is_put_back(self):
        self.notes(self.old.name)
        self.assertEqual(self.delete("failed"), [])
        self.assertEqual(self.code, 1)
        self.assertEqual(len(list(self.meetings().glob("*kickoff*"))), 2)

    def test_a_meeting_with_no_pages_in_a_project_costs_it_nothing(self):
        self.assertEqual(self.delete("trashed"), [])
        self.assertFalse(self.meetings().exists())
        self.assertNotIn("remove", self.out)

    def test_a_copy_that_cannot_be_taken_out_keeps_the_meeting(self):
        """The record of where the copy is lives in meta.json, which goes
        with the meeting: delete anyway and the transcript page is left
        in the project for good."""
        self.notes(self.old.name)
        before = (self.old / "meta.json").read_text()
        denied = PermissionError(errno.EACCES, "Permission denied")
        with mock.patch.object(cli, "forget_asst") as forget, \
             mock.patch.object(cli, "delete_session") as delete, \
             mock.patch.object(Path, "unlink", side_effect=denied):
            self.run_cmd(cli.cmd_delete, session=self.old.name, yes=True,
                         permanent=False)
        self.assertEqual(self.code, 1)
        forget.assert_not_called()  # its tasks stay filed too
        delete.assert_not_called()
        self.assertIn("Permission denied", self.err)
        self.assertIn("was not deleted", self.err)
        self.assertEqual((self.old / "meta.json").read_text(), before)
        self.assertEqual(len(list(self.meetings().glob("*kickoff*"))), 2)
        # Once the folder can be written, the same command finishes.
        self.assertEqual(self.delete("trashed"), [])
        self.assertEqual(list(self.meetings().glob("*kickoff*")), [])
