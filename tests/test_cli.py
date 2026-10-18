"""CLI output helpers."""
from __future__ import annotations

import argparse
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

from steno import asstlink, cli
from steno.cli import _parse_duration, _replace_line, _split_csv
from steno.notes import Todo, save_todos
from steno.realign import Plan
from steno.summarize import is_archived, load_meta, set_archived


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
        """`steno publish --all` takes its sessions from here too."""
        everything = [self.new, self.away, self.old]
        with redirect_stdout(io.StringIO()) as out:
            kept = cli._leave_out_archived(everything)
        self.assertEqual(kept, [self.new, self.old])
        self.assertIn("1 archived meeting left out", out.getvalue())


class TestNamedCommandsStillReachIt(ArchiveCase):
    def test_export_by_name_writes_the_meeting(self):
        args = argparse.Namespace(
            session=self.away.name, output=None, dir=self.root
        )
        self.run_cmd(cli.cmd_export, args)
        self.assertIsNone(self.code)
        self.assertIn("# Standup", self.out)

    def test_todos_still_lists_what_you_owe_from_it(self):
        """Putting a meeting away does not keep a promise made in it."""
        save_todos(self.away, [Todo(text="Send the report", owner="you")])
        with mock.patch.object(asstlink, "available", lambda: False):
            self.run_cmd(cli.cmd_todos, argparse.Namespace(
                dir=self.root, all=False
            ))
        self.assertIn("Standup", self.out)
        self.assertIn("[ ] Send the report", self.out)
