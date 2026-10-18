"""Which model answers a question about a meeting.

The order of precedence and the list of choices are checked here. The
control in the Ask box needs a display, so tests/gtk_ask_model.py runs it
on a private Broadway one, with no pointer and no screen; that file is
skipped without gtk4-broadwayd.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from steno import ASK_MODEL_ENV, ask_model, resolve_ask_model
from steno.gui.askmodel import choice_rows, row_in_force
from tests.test_sidebar_menu import ROOT, SCRUBBED, start_broadway


class TestWhichModelAnswers(unittest.TestCase):
    def test_the_environment_beats_the_pick_and_the_pick_beats_the_default(self):
        self.assertEqual(resolve_ask_model("env", "pick", "default"), "env")
        self.assertEqual(resolve_ask_model("", "pick", "default"), "pick")
        self.assertEqual(resolve_ask_model("", "", "default"), "default")

    def test_a_blank_is_not_a_choice(self):
        self.assertEqual(resolve_ask_model("  ", " ", "default"), "default")
        self.assertEqual(resolve_ask_model(" env ", "pick", "default"), "env")

    def test_it_reads_the_environment_and_falls_back_to_the_summary_model(self):
        with mock.patch.dict(os.environ, {
            ASK_MODEL_ENV: "", "STENO_MODEL": "summary-model",
        }):
            self.assertEqual(ask_model(), "summary-model")
            self.assertEqual(ask_model("picked"), "picked")
            os.environ[ASK_MODEL_ENV] = "from-env"
            self.assertEqual(ask_model("picked"), "from-env")


class TestChoices(unittest.TestCase):
    def test_the_default_comes_first_then_presets_then_the_providers_list(self):
        rows = choice_rows("", "flash", ["flash", "pro"], ["alpha", "pro", "zed"])
        self.assertEqual(rows, [
            ("", "flash"), ("pro", "pro"), ("alpha", "alpha"), ("zed", "zed"),
        ])

    def test_a_model_is_one_row_however_many_places_name_it(self):
        """The default is also a preset and in the provider's list."""
        rows = choice_rows("flash", "flash", ["flash", "pro"], ["flash", "pro"])
        self.assertEqual([label for _value, label in rows], ["flash", "pro"])

    def test_the_default_row_saves_nothing_so_it_follows_the_default(self):
        rows = choice_rows("", "flash", ["flash", "pro"], [])
        self.assertEqual(rows[0][0], "")
        self.assertEqual([v for v, _ in rows if v], ["pro"])

    def test_a_pick_nothing_lists_is_still_shown(self):
        rows = choice_rows("mine", "flash", ["flash"], [])
        self.assertEqual(rows[1], ("mine", "mine"))
        self.assertEqual([v for v, _ in rows].count("mine"), 1)

    def test_with_no_list_the_presets_are_what_there_is(self):
        self.assertEqual(
            [v for v, _ in choice_rows("", "flash", ["flash"], [])],
            [""],
        )
        self.assertEqual(
            [v for v, _ in choice_rows("", "flash", ["flash", "pro"], [])],
            ["", "pro"],
        )

    def test_a_provider_with_no_default_still_has_a_default_row(self):
        self.assertEqual(choice_rows("", "", [], []), [("", "Default")])

    def test_the_tick_is_on_the_one_row_that_answers_as_the_pick_does(self):
        self.assertEqual(row_in_force("", "flash"), "")
        self.assertEqual(row_in_force("pro", "flash"), "pro")
        # Pinned to what the default is: the same model, on the same row.
        self.assertEqual(row_in_force("flash", "flash"), "")
        self.assertEqual(row_in_force("", ""), "")


class TestOnBroadway(unittest.TestCase):
    def test_the_ask_box_in_a_window_on_broadway(self):
        with tempfile.TemporaryDirectory() as runtime:
            server, number = start_broadway(runtime)
            if server is None:
                self.skipTest("no gtk4-broadwayd to open windows on")
            env = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
            env.update(
                XDG_RUNTIME_DIR=runtime,
                XDG_DATA_HOME=runtime,
                XDG_STATE_HOME=runtime,
                GDK_BACKEND="broadway",
                BROADWAY_DISPLAY=f":{number}",
                GSETTINGS_BACKEND="memory",
                GTK_A11Y="none",
                NO_AT_BRIDGE="1",
            )
            try:
                done = subprocess.run(
                    [sys.executable, "-W", "ignore::DeprecationWarning",
                     "-m", "unittest", "-v", "tests.gtk_ask_model"],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            finally:
                server.terminate()
                server.wait()
        self.assertEqual(done.returncode, 0, done.stderr[-4000:])


if __name__ == "__main__":
    unittest.main()
