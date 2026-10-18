"""GTK checks for choosing the model that answers the Ask box.

Run by test_ask_model.py, in a child process on a Broadway display and
never on a real one. The name keeps test discovery from importing this
file in the main process, where `import gi` would connect to the desktop.

No model is reached and nothing leaves this process: the window is given
a stand-in engine whose `ask` records what it was handed, and the
provider's model list is stood in for at `askmodel.fetch_models`.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk, Gtk

from steno import ASK_MODEL_ENV, claude
from steno.events import AdviceDelta, AdviceDone
from steno.gui import askmodel, prefs
from steno.gui.window import Window
from tests.gtk_meeting_scroll import wait_for
from tests.gtk_sidebar_menu import StubApp, settle

DEFAULT = "mimo-v2.6-flash"
PRESETS = [DEFAULT, "mimo-v2.6-pro[1m]"]
LISTED = ["alpha-1", "beta-2"]
STAMP = "20260801-090000"


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


def make_meeting(root: Path) -> None:
    folder = root / STAMP
    folder.mkdir(parents=True)
    line = {"speaker": "them", "text": "hello", "ts": 1.0}
    (folder / "transcript.jsonl").write_text(json.dumps(line) + "\n")
    (folder / "meta.json").write_text(json.dumps({"title": "Call"}))


class AskModel(unittest.TestCase):
    def setUp(self):
        self.raised: list[BaseException] = []
        self._hook = sys.excepthook
        sys.excepthook = lambda _kind, value, _tb: self.raised.append(value)

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "sessions"
        make_meeting(self.root)
        # The pick is saved under XDG_STATE_HOME, and the default is read
        # from the environment: both are this test's own.
        env = {"XDG_STATE_HOME": str(Path(self._tmp.name) / "state"),
               "STENO_PROVIDER": "mimo"}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        for name in (ASK_MODEL_ENV, "STENO_MODEL", "CLAUDE_MODEL"):
            os.environ.pop(name, None)

        # The provider's list: held back until released, so the menu can be
        # seen before it lands; or refusing to come.
        self.fetches = 0
        self.release = threading.Event()
        self.release.set()
        self.listing: list[str] | None = LISTED

        def fetch() -> list[str]:
            self.fetches += 1
            self.release.wait(5)
            if self.listing is None:
                msg = "no route"
                raise claude.ClaudeError(msg)
            return self.listing

        for patch in (
            mock.patch.object(askmodel, "fetch_models", fetch),
            mock.patch.object(askmodel, "MODELS", askmodel.ModelList()),
        ):
            patch.start()
            self.addCleanup(patch.stop)

        # The engine's `ask` records its arguments instead of calling out.
        self.sent: list[tuple] = []
        self.app = StubApp()
        self.app.register(None)
        self.app.engine = SimpleNamespace(
            current=None,
            ask=lambda question, session_dir=None, model=None: (
                "ask", question, session_dir, model),
        )
        self.app.bridge = SimpleNamespace(
            submit=lambda job: self.sent.append(job) or object())
        self.window = Window(self.app, self.root)
        self.window.present()
        settle()
        self.box = self.window.meeting.ask
        self.button = self.box.model_button

    def tearDown(self):
        sys.excepthook = self._hook
        self.release.set()
        self.window.destroy()
        settle()
        self._tmp.cleanup()
        self.assertEqual(self.raised, [])

    # ----------------------------------------------------------------- helpers

    def shown(self) -> str:
        return self.button.model_label.get_text()

    def open_menu(self) -> None:
        self.button.popup()
        settle()

    def row_widgets(self) -> list:
        found, i = [], 0
        while (row := self.button.choices.get_row_at_index(i)) is not None:
            found.append(row)
            i += 1
        return found

    def rows(self) -> list[tuple[str | None, str]]:
        return [(row.value, row.label) for row in self.row_widgets()]

    def marked(self) -> list[str | None]:
        """The rows that carry the tick for being in force."""
        return [
            row.value for row in self.row_widgets()
            if isinstance(row.get_child().get_last_child(), Gtk.Image)
        ]

    def tagged(self) -> list[str | None]:
        """The rows that say they follow the default."""
        found = []
        for row in self.row_widgets():
            part = row.get_child().get_first_child()
            while part is not None:
                if isinstance(part, Gtk.Label) and part.get_text() == "default":
                    found.append(row.value)
                part = part.get_next_sibling()
        return found

    def values(self) -> list[str | None]:
        return [value for value, _label in self.rows()]

    def choose(self, value: str | None) -> None:
        """Activate the row for `value`, as a click or Enter on it would."""
        i = self.values().index(value)
        row = self.button.choices.get_row_at_index(i)
        self.button.choices.emit("row-activated", row)
        settle()

    def ask(self, question: str = "what was decided?") -> None:
        self.box.entry.set_text(question)
        self.box.entry.emit("activate")
        settle()

    def answer(self, text: str = "Sign on Monday.") -> None:
        self.window.on_event(AdviceDelta(text))
        self.window.on_event(AdviceDone(text))
        settle()

    # ------------------------------------------------------------- the control

    def test_it_names_the_model_a_question_would_go_to(self):
        self.assertEqual(self.shown(), DEFAULT)
        self.assertEqual(self.button.model, DEFAULT)

    def test_a_pick_from_an_earlier_run_is_shown_by_a_new_window(self):
        prefs.save_ask_model("beta-2")
        self.assertEqual(askmodel.AskModelButton().model_label.get_text(), "beta-2")

    def test_the_environment_overrides_a_pick_and_the_menu_says_so(self):
        prefs.save_ask_model("beta-2")
        os.environ[ASK_MODEL_ENV] = "from-env"
        self.button.refresh()
        self.assertEqual(self.shown(), "from-env")
        self.assertIn(ASK_MODEL_ENV, self.button.get_tooltip_text())
        self.assertIn("from-env", self.button.get_tooltip_text())

        self.open_menu()
        self.assertTrue(self.button.note.get_visible())
        self.assertIn("from-env", self.button.note.get_text())
        self.assertFalse(self.button.choices_scroll.get_visible())
        self.assertEqual(self.fetches, 0)

    # --------------------------------------------------------------- the menu

    def test_the_default_the_presets_and_custom_are_there_before_the_list(self):
        self.release.clear()
        self.open_menu()
        self.assertEqual(
            self.rows(),
            [("", DEFAULT), (PRESETS[1], PRESETS[1]), (None, "Custom…")],
        )
        self.assertFalse(self.button.note.get_visible())

        self.release.set()
        self.assertTrue(wait_for(lambda: LISTED[0] in self.values()))
        self.assertEqual(
            self.values(), ["", PRESETS[1], *LISTED, None])

    def test_a_model_is_one_row_and_only_the_default_one_is_tagged(self):
        """The default is a preset too, and the provider lists it: it is
        still a single row, and says it is the one that follows."""
        self.listing = [*LISTED, DEFAULT]
        self.open_menu()
        self.assertTrue(wait_for(lambda: LISTED[0] in self.values()))
        self.assertEqual(self.values().count(DEFAULT), 0)
        self.assertEqual([label for _v, label in self.rows()].count(DEFAULT), 1)
        self.assertEqual(self.tagged(), [""])
        self.assertEqual(self.marked(), [""])

    def test_a_pick_of_the_default_model_is_that_row_not_a_second_one(self):
        prefs.save_ask_model(DEFAULT)
        self.button.refresh()
        self.open_menu()
        self.assertEqual(self.marked(), [""])
        self.assertEqual([label for _v, label in self.rows()].count(DEFAULT), 1)

    def test_the_list_is_fetched_once_per_run(self):
        self.open_menu()
        self.assertTrue(wait_for(lambda: LISTED[0] in self.values()))
        self.button.popover.popdown()
        settle()
        self.open_menu()
        self.assertEqual(self.fetches, 1)
        self.assertIn(LISTED[0], self.values())

    def test_a_list_that_will_not_come_is_not_mentioned_and_tried_again(self):
        self.listing = None
        self.open_menu()
        self.assertTrue(wait_for(lambda: self.fetches == 1))
        settle()
        self.assertEqual(self.values(), ["", PRESETS[1], None])
        self.assertFalse(self.button.note.get_visible())

        self.button.popover.popdown()
        settle()
        self.listing = LISTED
        self.open_menu()
        self.assertTrue(wait_for(lambda: LISTED[0] in self.values()))
        self.assertEqual(self.fetches, 2)

    def test_the_menu_stays_open_and_usable_while_the_list_is_on_its_way(self):
        self.release.clear()
        self.open_menu()
        self.assertTrue(self.button.popover.get_visible())
        self.choose(PRESETS[1])
        self.assertEqual(prefs.ask_model(), PRESETS[1])

    def test_picking_a_row_saves_it_shows_it_and_closes_the_menu(self):
        self.open_menu()
        self.assertTrue(wait_for(lambda: "beta-2" in self.values()))
        self.choose("beta-2")

        self.assertEqual(prefs.ask_model(), "beta-2")
        self.assertEqual(self.shown(), "beta-2")
        self.assertFalse(self.button.popover.get_visible())

    def test_the_row_in_force_is_marked_and_the_default_returns(self):
        prefs.save_ask_model("beta-2")
        self.button.refresh()
        self.open_menu()
        # A pick that the provider has not yet listed is still a row.
        self.assertIn("beta-2", self.values())
        self.assertEqual(self.marked(), ["beta-2"])
        self.choose("")

        self.assertEqual(prefs.ask_model(), "")
        self.assertEqual(self.shown(), DEFAULT)
        self.open_menu()
        self.assertEqual(self.marked(), [""])

    def test_a_model_can_be_typed_in(self):
        self.open_menu()
        self.assertFalse(self.button.custom_entry.get_visible())
        self.choose(None)
        self.assertTrue(self.button.custom_entry.get_visible())
        self.assertTrue(self.button.popover.get_visible())

        self.button.custom_entry.emit("activate")   # nothing typed yet
        self.assertEqual(prefs.ask_model(), "")
        self.assertTrue(self.button.popover.get_visible())

        self.button.custom_entry.set_text("  my/model  ")
        self.button.custom_entry.emit("activate")
        settle()
        self.assertEqual(prefs.ask_model(), "my/model")
        self.assertEqual(self.shown(), "my/model")
        self.assertFalse(self.button.popover.get_visible())

    def test_a_long_list_gets_a_filter(self):
        self.listing = [f"model-{n:02d}" for n in range(30)]
        self.open_menu()
        self.assertTrue(wait_for(lambda: "model-29" in self.values()))
        self.assertTrue(self.button.filter_entry.get_visible())

        # The entry waits a moment after typing before it says anything.
        self.button.filter_entry.set_text("model-2")
        self.assertTrue(wait_for(lambda: "model-05" not in self.visible()))
        self.assertIn("model-25", self.visible())
        self.assertIn(None, self.visible())   # "Custom…" is not filtered away

    def visible(self) -> list[str | None]:
        return [
            row.value for row in self.row_widgets() if row.get_child_visible()
        ]

    def test_a_short_list_does_not(self):
        self.open_menu()
        self.assertTrue(wait_for(lambda: LISTED[0] in self.values()))
        self.assertFalse(self.button.filter_entry.get_visible())

    # --------------------------------------------------------- asking and saying

    def test_without_a_pick_the_question_goes_to_the_summary_model(self):
        self.ask()
        self.assertEqual(self.sent, [("ask", "what was decided?",
                                      self.root / STAMP, DEFAULT)])

    def test_the_next_question_goes_to_the_pick(self):
        self.open_menu()
        self.assertTrue(wait_for(lambda: "beta-2" in self.values()))
        self.choose("beta-2")
        self.ask()
        self.assertEqual(self.sent[-1][3], "beta-2")

    def test_the_environment_model_answers_whatever_was_picked(self):
        prefs.save_ask_model("beta-2")
        os.environ[ASK_MODEL_ENV] = "from-env"
        self.ask()
        self.assertEqual(self.sent[-1][3], "from-env")

    def test_the_answer_says_which_model_gave_it(self):
        self.assertFalse(self.box.byline.get_visible())
        self.ask()
        self.assertFalse(self.box.byline.get_visible())
        self.answer()
        self.assertTrue(self.box.byline.get_visible())
        self.assertEqual(self.box.byline.get_text(), f"answered by {DEFAULT}")
        self.assertIn("hint", self.box.byline.get_css_classes())

    def test_a_pick_made_while_it_answers_does_not_rename_that_answer(self):
        prefs.save_ask_model("beta-2")
        self.ask()
        prefs.save_ask_model("alpha-1")
        self.button.refresh()
        self.answer()
        self.assertEqual(self.box.byline.get_text(), "answered by beta-2")
        self.assertEqual(self.shown(), "alpha-1")

    def test_the_next_question_takes_the_old_signature_down(self):
        self.ask()
        self.answer()
        self.ask("and then?")
        self.assertFalse(self.box.byline.get_visible())

    def test_a_word_from_the_window_is_not_signed_as_a_models(self):
        self.app.engine = None
        self.ask()
        self.assertIn("isn't running", self.box.answer.get_text())
        self.assertFalse(self.box.byline.get_visible())

    def test_nor_is_the_engines_own(self):
        self.ask()
        self.window.on_event(AdviceDone("Nothing has been transcribed yet.",
                                        by_model=False))
        settle()
        self.assertFalse(self.box.byline.get_visible())

    def test_a_question_that_failed_before_any_text_has_nothing_to_sign(self):
        self.ask()
        self.window.on_event(AdviceDone(""))
        settle()
        self.assertFalse(self.box.byline.get_visible())
        self.assertFalse(self.box.answer_scroll.get_visible())

    def test_a_question_nothing_answered_goes_back_into_the_box(self):
        """A model id the provider would not take used to cost the
        question: the box emptied and only a toast said so."""
        self.ask("what was decided?")
        self.assertEqual(self.box.entry.get_text(), "")
        self.window.on_event(AdviceDone("", failed=True))
        settle()
        self.assertEqual(self.box.entry.get_text(), "what was decided?")
        self.assertFalse(self.box.answer_scroll.get_visible())
        self.assertFalse(self.box.byline.get_visible())

        self.box.entry.emit("activate")   # asked again, with another pick
        settle()
        self.assertEqual([job[1] for job in self.sent],
                         ["what was decided?"] * 2)

    def test_a_question_typed_since_is_not_written_over(self):
        self.ask("first?")
        self.box.entry.set_text("second?")
        self.window.on_event(AdviceDone("", failed=True))
        settle()
        self.assertEqual(self.box.entry.get_text(), "second?")

    def test_an_answer_that_died_midway_is_kept_and_not_signed(self):
        self.ask()
        self.window.on_event(AdviceDelta("Sign on"))
        self.window.on_event(AdviceDone("", failed=True))
        settle()
        self.assertEqual(self.box.answer.get_text(), "Sign on")
        self.assertTrue(self.box.byline.get_visible())
        said = self.box.byline.get_text()
        self.assertNotIn("answered by", said)
        self.assertIn("Cut off", said)
        self.assertIn(DEFAULT, said)
        # It did answer something, so the question is not put back.
        self.assertEqual(self.box.entry.get_text(), "")

    def test_the_next_question_after_a_cut_off_one_is_signed_again(self):
        self.ask()
        self.window.on_event(AdviceDelta("Sign on"))
        self.window.on_event(AdviceDone("", failed=True))
        self.ask("and then?")
        self.answer()
        self.assertEqual(self.box.byline.get_text(), f"answered by {DEFAULT}")

    def test_the_live_box_shares_the_pick(self):
        prefs.save_ask_model("beta-2")
        self.window.stack.set_visible_child_name("live")
        settle()
        live = self.window.live.ask.model_button
        self.assertEqual(live.model, "beta-2")
        self.assertTrue(wait_for(lambda: live.model_label.get_text() == "beta-2"))


if __name__ == "__main__":
    unittest.main()
