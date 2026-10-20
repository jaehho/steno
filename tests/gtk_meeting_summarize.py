"""GTK checks for "Summarize again" on a past meeting (issue #26).

Run by test_meeting_summarize.py, in a child process on a Broadway
display and never on a real one. The name keeps test discovery from
importing this file in the main process, where `import gi` would connect
to the desktop.

The model is never reached: the page is given a callback that records
what it was asked to write, and the window's own callback is given a
stand-in engine. Nothing here can start a summary, a recording or a
call to the task app.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk

from steno.cost import SUMMARY_GUESS, format_usd, prerecorded_cost
from steno.events import NotesStatus, SummaryReady
from steno.notesjob import NotesPhase, NotesState
from tests.gtk_meeting_cites import CitedMeeting, click
from tests.gtk_sidebar_menu import right_click, settle

WRITING = "Writing the summary…"
ALREADY = "Already writing the summary…"
NEW_SUMMARY = """# Rewritten call

## Decisions
- Sign on Monday <!--cite 15-->
- Budget stays <!--cite 45-->
"""


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


class SummarizeAgain(CitedMeeting):
    """A window on the Cited meeting (open) and the Plain one below."""

    def setUp(self):
        super().setUp()
        self.asked: list[tuple[Path, bool]] = []

        def ask(folder: Path, *, force: bool = False) -> None:
            self.asked.append((folder, force))

        self.page._on_summarize = ask
        self.dialogs: list[Adw.AlertDialog] = []
        present = Adw.AlertDialog.present

        def spy(dialog: Adw.AlertDialog, parent) -> None:
            self.dialogs.append(dialog)
            present(dialog, parent)

        patch = mock.patch.object(Adw.AlertDialog, "present", spy)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.close_dialogs)
        self.cited = self.page.current.dir
        self.plain = self.cited.parent / "20260110-090000"

    def close_dialogs(self) -> None:
        for dialog in self.dialogs:
            dialog.force_close()

    def open_row(self, index: int) -> None:
        self.window.sidebar.list.select_row(
            self.window.sidebar.list.get_row_at_index(index)
        )

    def choose(self, response: str) -> None:
        """Answer the dialog the page last put up."""
        self.dialogs[-1].emit("response", response)
        settle()

    def ask_and_confirm(self) -> None:
        self.page.actions.activate_action("summarize", None)
        self.choose("go")

    def engine_reports(
        self,
        folder: Path,
        *,
        state: NotesState = NotesState.RUNNING,
        phase: NotesPhase | None = NotesPhase.SUMMARIZING,
        error: str = "",
        joined: bool = False,
    ) -> None:
        """What the engine says about a notes job, as the window hears it."""
        self.window._on_finishing_event(NotesStatus(
            folder, state, phase=phase, error=error, joined=joined
        ))
        settle()

    def action_enabled(self) -> bool:
        return self.page.actions.lookup_action("summarize").get_enabled()

    def note(self) -> str:
        return self.page.summary_hint.get_text()

    def titles(self) -> list[str]:
        return [s.markup for s in self.view.sources]


class TestAsking(SummarizeAgain):
    def test_it_asks_before_spending_anything_and_says_what_stays(self):
        before = (self.cited / "summary.md").read_bytes()

        self.page.actions.activate_action("summarize", None)

        (dialog,) = self.dialogs
        self.assertEqual(
            dialog.get_heading(), "Summarize this meeting again?"
        )
        body = dialog.get_body()
        self.assertIn(f"about {format_usd(SUMMARY_GUESS)}", body)
        self.assertIn("The new summary replaces this one", body)
        self.assertIn("notes", body)
        self.assertIn("project", body)
        self.assertIn("ticked", body)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.page._notes, {})
        self.assertEqual((self.cited / "summary.md").read_bytes(), before)

    def test_cancelling_spends_nothing(self):
        self.page.actions.activate_action("summarize", None)
        self.choose("cancel")

        self.assertEqual(self.asked, [])
        self.assertEqual(self.page._notes, {})
        self.assertTrue(self.action_enabled())

    def test_the_default_answer_is_not_to(self):
        self.page.actions.activate_action("summarize", None)
        self.assertEqual(self.dialogs[0].get_default_response(), "cancel")

    def test_confirming_asks_the_engine_and_waits_for_its_word(self):
        """The page must not claim the summary is being written until the
        engine says a job is under way — the bug this replaced."""
        self.ask_and_confirm()

        self.assertEqual(self.asked, [(self.cited, True)])
        self.assertEqual(self.page._notes, {})

    def test_when_the_engine_says_it_is_writing_the_page_says_so(self):
        self.ask_and_confirm()
        self.engine_reports(self.cited)

        self.assertEqual(self.note(), WRITING)
        self.assertFalse(self.action_enabled())

    def test_it_costs_more_when_your_side_was_never_transcribed(self):
        """The engine transcribes the mic first then; the price shown
        has to include it."""
        meta = self.cited / "meta.json"
        meta.write_text(json.dumps({
            **json.loads(meta.read_text()), "mic_pending": True,
        }))
        minutes = self.page.current.minutes

        self.page.actions.activate_action("summarize", None)

        expected = format_usd(SUMMARY_GUESS + prerecorded_cost(minutes))
        self.assertNotEqual(expected, format_usd(SUMMARY_GUESS))
        self.assertIn(f"about {expected}", self.dialogs[0].get_body())

    def test_a_meeting_without_a_summary_is_written_not_written_again(self):
        (self.plain / "summary.md").unlink()
        self.open_row(1)

        self.page.actions.activate_action("summarize", None)

        (dialog,) = self.dialogs
        self.assertEqual(dialog.get_heading(), "Write the summary?")
        self.assertNotIn("replaces", dialog.get_body())
        self.assertNotIn("again", dialog.get_body())
        self.choose("go")
        self.assertEqual(self.asked, [(self.plain, False)])
        self.engine_reports(self.plain)
        self.assertEqual(self.page.summary_label.get_text(), WRITING)

    def test_the_button_of_a_meeting_with_no_summary_writes_it_at_once(self):
        (self.plain / "summary.md").unlink()
        self.open_row(1)
        button = self.page.summarize_button
        self.assertTrue(button.get_visible())

        button.emit("clicked")

        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.asked, [(self.plain, False)])
        self.engine_reports(self.plain)
        self.assertEqual(self.page.summary_label.get_text(), WRITING)
        self.assertFalse(button.get_visible())

    def test_nothing_transcribed_nothing_to_summarize(self):
        (self.cited / "transcript.jsonl").write_text("")
        self.page.reload()

        self.assertFalse(self.action_enabled())
        self.page._confirm_summarize()
        self.assertEqual(self.dialogs, [])


class TestWhileItWrites(SummarizeAgain):
    def setUp(self):
        super().setUp()
        click(self.item("deposit").mark)
        self.assertEqual(self.lit(), [120, 121, 122])
        self.ask_and_confirm()
        self.engine_reports(self.cited)

    def test_the_old_summary_stays_up_and_so_does_what_is_lit(self):
        self.assertEqual(
            self.titles(),
            ["Opening point", "Pay the deposit by Friday", "Ship in March"],
        )
        self.assertTrue(self.view.get_visible())
        self.assertEqual(self.lit(), [120, 121, 122])
        self.assertTrue(self.item("deposit").mark.has_css_class("item-active"))

    def test_the_heading_says_it_is_being_written(self):
        self.assertTrue(self.page.summary_hint.get_visible())
        self.assertEqual(self.note(), WRITING)
        self.assertFalse(self.page.summary_hint.get_label().count("<a "))

    def test_it_cannot_be_asked_twice(self):
        self.assertFalse(self.action_enabled())
        self.dialogs.clear()

        self.page.actions.activate_action("summarize", None)
        self.page._confirm_summarize()

        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.asked, [(self.cited, True)])

    def test_it_can_be_asked_for_another_meeting_meanwhile(self):
        self.open_row(1)

        self.assertTrue(self.action_enabled())
        self.assertEqual(self.page.current.dir, self.plain)
        self.assertNotEqual(self.note(), WRITING)

    def test_coming_back_to_it_shows_it_still_writing(self):
        self.open_row(1)
        self.open_row(0)

        self.assertEqual(self.page.current.dir, self.cited)
        self.assertEqual(self.note(), WRITING)
        self.assertFalse(self.action_enabled())


class TestWhenItEnds(SummarizeAgain):
    def setUp(self):
        super().setUp()
        self.ask_and_confirm()
        self.engine_reports(self.cited)

    def finish(self, folder: Path) -> None:
        """What the engine leaves behind, then what the app tells the
        window (it has been through the real `_on_finishing_event`)."""
        (folder / "summary.md").write_text(NEW_SUMMARY)
        meta = folder / "meta.json"
        meta.write_text(json.dumps({
            **json.loads(meta.read_text()), "title": "Rewritten call",
        }))
        self.window._on_finishing_event(
            SummaryReady(folder, "Rewritten call", NEW_SUMMARY, requested=True)
        )
        settle()

    def test_the_page_shows_the_new_summary_and_the_new_title(self):
        self.finish(self.cited)

        self.assertEqual(self.titles(), ["Sign on Monday", "Budget stays"])
        self.assertEqual(self.window.page_title.get_text(), "Rewritten call")
        self.assertEqual(self.page._notes, {})
        self.assertFalse(self.page.summary_hint.get_visible())
        self.assertTrue(self.action_enabled())

    def test_what_was_lit_belongs_to_the_old_summary_and_goes(self):
        self.finish(self.cited)
        self.assertEqual(self.lit(), [])
        self.assertIsNone(self.page._active)

    def test_a_meeting_opened_meanwhile_is_not_disturbed(self):
        self.open_row(1)
        before = self.titles()

        self.finish(self.cited)

        self.assertEqual(self.titles(), before)
        self.assertEqual(self.page._notes, {})
        self.open_row(0)
        self.assertEqual(self.titles(), ["Sign on Monday", "Budget stays"])
        self.assertFalse(self.page.summary_hint.get_visible())

    def test_a_failure_leaves_the_old_summary_and_the_way_to_try_again(self):
        before = (self.cited / "summary.md").read_bytes()
        titles = self.titles()

        self.window._on_finishing_event(
            NotesStatus(self.cited, NotesState.FAILED, error="no key")
        )
        settle()

        # The failure is kept so the heading can say why, and the action
        # stays available as the retry.
        failed = self.page._notes[self.cited]
        self.assertIs(failed.state, NotesState.FAILED)
        self.assertEqual(failed.error, "no key")
        self.assertEqual((self.cited / "summary.md").read_bytes(), before)
        self.assertEqual(self.titles(), titles)
        self.assertEqual(self.note(), "no key")
        self.assertTrue(self.action_enabled())


class TestFromTheRowMenu(SummarizeAgain):
    def test_the_row_menu_summarizes_the_meeting_it_was_opened_on(self):
        self.assertEqual(self.page.current.dir, self.cited)

        right_click(self.window.sidebar.list.get_row_at_index(1))
        popover = self.window.sidebar._menu
        self.assertTrue(popover.activate_action("meeting.summarize", None))
        self.choose("go")

        self.assertEqual(self.page.current.dir, self.plain)
        self.assertEqual(self.asked, [(self.plain, True)])
        self.engine_reports(self.plain)
        self.assertEqual(self.note(), WRITING)

    def test_both_menus_name_it_the_same(self):
        header = self.window.meeting_menu.get_menu_model()
        labels = []
        for i in range(header.get_n_items()):
            label = header.get_item_attribute_value(i, "label", None)
            if label is not None:
                labels.append(label.get_string())
        self.assertIn("Summarize again...", labels)


class TestTheNoteBesideTheHeading(SummarizeAgain):
    HINT = "No citations. Summarize again to add them."

    def setUp(self):
        super().setUp()
        self.open_row(1)

    def test_a_summary_with_nothing_cited_offers_to_do_it_again(self):
        self.assertEqual(self.note(), self.HINT)
        self.assertIn(
            'href="summarize"', self.page.summary_hint.get_label()
        )
        # Reachable without a pointer.
        self.assertTrue(self.page.summary_hint.get_focusable())

    def test_following_the_link_asks_the_same_question_as_the_menu(self):
        self.assertTrue(
            self.page.summary_hint.emit("activate-link", "summarize")
        )

        (dialog,) = self.dialogs
        self.assertEqual(
            dialog.get_heading(), "Summarize this meeting again?"
        )
        self.choose("go")
        self.assertEqual(self.asked, [(self.plain, True)])
        self.engine_reports(self.plain)
        self.assertEqual(self.note(), WRITING)

    def test_enter_on_the_focused_link_asks_too(self):
        self.assertTrue(self.page.summary_hint.grab_focus())
        self.assertIs(self.window.get_focus(), self.page.summary_hint)

        self.page.summary_hint.emit("activate-current-link")

        (dialog,) = self.dialogs
        self.assertEqual(
            dialog.get_heading(), "Summarize this meeting again?"
        )

    def test_a_link_to_anything_else_is_left_to_gtk(self):
        self.assertFalse(self.page._on_hint_link(None, "https://example.org"))
        self.assertEqual(self.dialogs, [])


class TestTheWindowsOwnCallback(CitedMeeting):
    """What Summarize reaches in the window, with a stand-in engine."""

    def setUp(self):
        super().setUp()
        self.window_toasts: list[str] = []
        self.window._toast = lambda text, **_kw: self.window_toasts.append(
            text
        )
        patch = mock.patch.object(Adw.AlertDialog, "present")
        patch.start()
        self.addCleanup(patch.stop)

    def confirm(self) -> None:
        """The page's own confirm, answered without a dialog."""
        with mock.patch.object(
            self.page, "_dialog",
            side_effect=lambda _h, _b, _c, go, **_kw: go(),
        ):
            self.page.actions.activate_action("summarize", None)

    def test_it_hands_the_engine_this_meeting_on_the_bridge(self):
        sent = []

        def summarize_now(folder: Path, *, force: bool = False):
            return ("summarize_now", folder, force)

        self.app.engine = SimpleNamespace(
            current=None,
            summarize_now=summarize_now,
        )
        self.app.bridge = SimpleNamespace(
            submit=lambda job: sent.append(job) or object()
        )
        self.addCleanup(setattr, self.app, "engine", None)
        self.addCleanup(setattr, self.app, "bridge", None)

        self.confirm()

        self.assertEqual(
            sent, [("summarize_now", self.page.current.dir, True)]
        )
        # The page waits for the engine's NotesStatus; it does not claim
        # work has started on its own.
        self.assertEqual(self.page._notes, {})

    def test_with_the_listener_down_it_says_so_and_does_not_hang(self):
        self.confirm()

        self.assertEqual(self.page._notes, {})
        self.assertEqual(
            self.window_toasts,
            ["Steno isn't running, so it can't write the summary"],
        )
        self.assertFalse(self.page.summary_hint.get_visible())
        self.assertTrue(
            self.page.actions.lookup_action("summarize").get_enabled()
        )
