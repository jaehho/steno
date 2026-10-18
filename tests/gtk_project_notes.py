"""GTK checks for the meeting page and the app keeping project notes (#27).

Run by test_project_notes_window.py, in a child process on a Broadway
display and never on a real one. The name keeps test discovery from
importing this file in the main process, where `import gi` would connect
to the desktop.

The projects directory is a temp one, the rewriter's clock is a fake
one (a minute passes when the test says), and a folder is never handed to
the desktop: `_open_folder` is replaced. The app object is made but never
started, so nothing listens to a microphone.
"""
from __future__ import annotations

# ruff: noqa: E402  # gi.require_version must precede gi.repository imports
import errno
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk

from steno import projectnotes
from steno.events import SummaryReady
from steno.gui import meeting
from steno.gui.app import App
from steno.gui.menu import menu_actions
from steno.gui.rewrite import DELAY_S, Rewriter
from steno.gui.window import Window
from steno.notes import load_todos, save_todos
from steno.summarize import update_meta
from tests.gtk_quit import find, press
from tests.gtk_sidebar_menu import StubApp, settle
from tests.test_projectnotes import SUMMARY, make_session
from tests.test_rewrite import FakeTime

ALPHA, BRAVO, CHARLIE = "20260928-143000", "20261006-161200", "20261007-090000"
ALPHA_PAGE = "2026-09-28-1430-vendor-pricing-call.md"
OPEN_ITEM = "- [ ] Draft the comparison table"
DONE_ITEM = "- [x] Draft the comparison table"


def setUpModule():
    display = Gdk.Display.get_default()
    name = None if display is None else display.__gtype__.name
    assert name == "GdkBroadwayDisplay", f"refusing to open windows on {name}"


class Base(unittest.TestCase):
    def setUp(self):
        self.raised: list[BaseException] = []
        self._hook = sys.excepthook
        sys.excepthook = lambda _kind, value, _tb: self.raised.append(value)
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
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

        self.alpha = make_session(self.root, ALPHA)
        self.bravo = make_session(self.root, BRAVO, title="Bravo")
        self.charlie = make_session(self.root, CHARLIE, title="Charlie",
                                    project=None)
        self.time = FakeTime()
        self.problems: list[str] = []
        self.rp = Rewriter(self.problems.append, **self.time.timing)

    def tearDown(self):
        self.rp.wait(10)
        sys.excepthook = self._hook
        self._tmp.cleanup()
        self.assertEqual(self.raised, [])

    def meetings(self, project: str = "demo") -> Path:
        return self.projects / project / "notes" / "meetings"

    def settle(self) -> None:
        """Writes that were started finish, and their reports arrive."""
        self.rp.wait(10)
        settle()


class TestInTheWindow(Base):
    def setUp(self):
        super().setUp()
        self.app = StubApp()
        self.app.register(None)
        self.app.rewriter = self.rp
        self.window = Window(self.app, self.root)
        self.window.present()
        settle()
        self.page = self.window.meeting
        self.toasts: list[str] = []
        self.page._toast = lambda text, **_kw: self.toasts.append(text)

    def tearDown(self):
        self.window.destroy()
        settle()
        super().tearDown()

    def open(self, folder: Path) -> None:
        self.window.sidebar.select(folder)
        settle()
        self.assertEqual(self.page.current.dir, folder)

    def page_text(self, name: str = ALPHA_PAGE) -> str:
        return (self.meetings() / name).read_text()

    def tick_second_item(self) -> None:
        """Click the box of the to-do the page shows open."""
        boxes = []
        child = self.page.todo_box.get_first_child()
        while child is not None:
            check = find(child, lambda w: isinstance(w, Gtk.CheckButton))
            if check is not None:
                boxes.append(check)
            child = child.get_next_sibling()
        self.assertEqual(len(boxes), 2)
        boxes[1].set_active(not boxes[1].get_active())

    def header_actions(self) -> list[str]:
        return menu_actions(self.window.meeting_menu.get_menu_model())

    # ----------------------------------------------------------------- edits

    def test_a_tick_reaches_the_project_after_a_pause(self):
        projectnotes.write(self.alpha)
        self.open(self.alpha)
        self.tick_second_item()
        self.assertTrue(load_todos(self.alpha)[1].done)
        self.time.advance(DELAY_S - 1)
        self.settle()
        self.assertIn(OPEN_ITEM, self.page_text())
        self.time.advance(1)
        self.settle()
        self.assertIn(DONE_ITEM, self.page_text())
        self.assertIn("todos_open: 0", self.page_text())

    def test_a_tick_taken_back_leaves_every_file_as_it_was(self):
        projectnotes.write(self.alpha)
        folder = self.meetings()
        before = {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}
        self.open(self.alpha)
        self.tick_second_item()
        self.tick_second_item()
        self.time.advance(DELAY_S)
        self.settle()
        after = {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}
        self.assertEqual(after, before)

    def test_notes_typed_reach_the_project(self):
        projectnotes.write(self.alpha)
        self.open(self.alpha)
        self.page.notes.view.get_buffer().set_text("call Dana back\n")
        self.page.notes.flush()
        self.time.advance(DELAY_S)
        self.settle()
        self.assertIn("call Dana back", self.page_text())

    def test_a_meeting_in_no_project_is_not_put_into_one_by_an_edit(self):
        self.open(self.bravo)
        self.page.notes.view.get_buffer().set_text("thoughts\n")
        self.page.notes.flush()
        self.time.advance(10 * DELAY_S)
        self.settle()
        self.assertFalse(self.meetings().exists())

    def test_opening_another_meeting_writes_what_was_waiting(self):
        projectnotes.write(self.alpha)
        self.open(self.alpha)
        self.tick_second_item()
        self.assertIn(OPEN_ITEM, self.page_text())
        self.open(self.charlie)  # no minute has passed
        self.settle()
        self.assertIn(DONE_ITEM, self.page_text())

    def test_the_notes_typed_last_are_written_with_it(self):
        projectnotes.write(self.alpha)
        self.open(self.alpha)
        self.page.notes.view.get_buffer().set_text("typed just now\n")
        self.open(self.charlie)  # the box saves, the page is left
        self.settle()
        self.assertIn("typed just now", self.page_text())

    def test_the_phone_ticking_is_a_change_too(self):
        projectnotes.write(self.alpha)
        self.open(self.alpha)
        s = self.page.current
        ticked = load_todos(self.alpha)
        ticked[1].done = True
        save_todos(self.alpha, ticked)  # as `pull_todos` leaves the file
        self.page._apply_pull(s, ticked)
        self.time.advance(DELAY_S)
        self.settle()
        self.assertIn(DONE_ITEM, self.page_text())

    def test_a_re_timing_that_finished_reaches_the_project(self):
        projectnotes.write(self.alpha)
        self.open(self.alpha)
        meta = json.loads((self.alpha / "meta.json").read_text())
        row = {"ts": meta["started_at"] + 5, "speaker": "them",
               "text": "re-timed words"}
        (self.alpha / "transcript.jsonl").write_text(json.dumps(row) + "\n")
        self.page._realigned(self.alpha, 1, "")
        self.time.advance(DELAY_S)
        self.settle()
        words = self.page_text(ALPHA_PAGE.replace(".md", ".transcript.md"))
        self.assertIn("[00:05] them: re-timed words", words)

    def test_a_pull_that_finds_nothing_new_waits_for_nothing(self):
        projectnotes.write(self.alpha)
        self.open(self.alpha)
        self.page._apply_pull(self.page.current, load_todos(self.alpha))
        self.assertEqual(self.time.timers, {})

    # --------------------------------------------------------------- project

    def choose_project(self, label: str) -> None:
        self.page.actions.activate_action("project", None)
        settle()
        dialog = self.window.get_visible_dialog()
        self.assertIsInstance(dialog, Adw.AlertDialog)
        choice = dialog.get_extra_child()
        labels = [item.get_string() for item in choice.get_model()]
        choice.set_selected(labels.index(label))
        press(dialog, "Save")
        self.settle()

    def test_setting_a_project_writes_its_pages_there(self):
        self.open(self.charlie)
        self.assertNotIn("meeting.show-notes", self.header_actions())
        self.choose_project("demo")
        self.assertEqual(len(list(self.meetings().glob("2026-10-07-*"))), 2)
        self.assertIn("meeting.show-notes", self.header_actions())

    def test_moving_it_to_another_project_takes_the_pages_along(self):
        projectnotes.write(self.alpha)
        self.open(self.alpha)
        self.choose_project("other")
        self.assertFalse(self.meetings().exists())
        self.assertTrue((self.meetings("other") / ALPHA_PAGE).is_file())
        self.assertEqual(self.toasts[0], "Project: other")

    def test_clearing_the_project_takes_them_away(self):
        projectnotes.write(self.alpha)
        self.open(self.alpha)
        self.choose_project("None")
        self.assertFalse(self.meetings().exists())
        self.assertNotIn("meeting.show-notes", self.header_actions())

    # ------------------------------------------------------------------ menu

    def test_the_menu_offers_the_pages_only_to_a_meeting_that_has_some(self):
        projectnotes.write(self.alpha)
        self.window.sidebar.refresh()
        settle()
        self.open(self.alpha)
        self.assertIn("meeting.show-notes", self.header_actions())
        self.open(self.bravo)
        self.assertNotIn("meeting.show-notes", self.header_actions())

    def test_a_summary_landing_on_the_open_meeting_offers_its_pages(self):
        self.open(self.charlie)
        self.assertNotIn("meeting.show-notes", self.header_actions())
        update_meta(self.charlie, project="demo")
        projectnotes.write(self.charlie)  # what the app did on its worker
        self.window.on_event(
            SummaryReady(self.charlie, "Charlie", SUMMARY, requested=True)
        )
        settle()
        self.assertIn("meeting.show-notes", self.header_actions())

    def test_show_in_project_notes_opens_the_folder_and_nothing_else(self):
        projectnotes.write(self.alpha)
        self.window.sidebar.refresh()
        settle()
        self.open(self.alpha)
        with mock.patch.object(self.page, "_open_folder") as opened:
            self.page.actions.activate_action("show-notes", None)
        opened.assert_called_once_with(self.meetings().resolve())

    def test_if_the_pages_have_gone_it_says_so_instead(self):
        projectnotes.write(self.alpha)
        self.window.sidebar.refresh()
        settle()
        self.open(self.alpha)
        for page in self.meetings().iterdir():
            page.unlink()
        with mock.patch.object(self.page, "_open_folder") as opened:
            self.page.actions.activate_action("show-notes", None)
        opened.assert_not_called()
        self.assertEqual(
            self.toasts, ["This meeting is not in a project's notes"]
        )

    # ---------------------------------------------------------------- delete

    def delete(self, outcome: str | None = None) -> list[str]:
        """Delete the open meeting; what its project held when the
        meeting was deleted."""
        seen: list[str] = []
        real = meeting.delete_session

        def spy(folder, permanent=False):
            seen.extend(p.name for p in self.meetings().glob("2026-09-28*"))
            return (outcome, None) if outcome else real(folder, permanent)

        with mock.patch.object(meeting, "delete_session", spy):
            self.page._delete(self.page.current)
        self.settle()
        return seen

    def test_deleting_takes_the_pages_out_before_the_meeting_goes(self):
        projectnotes.write(self.alpha)
        self.open(self.alpha)
        self.page.notes.view.get_buffer().set_text("typed just now\n")
        self.assertEqual(self.delete(), [])
        self.assertFalse(self.alpha.exists())
        self.assertFalse(self.meetings().exists())
        self.assertEqual(self.time.timers, {})  # nothing is waiting to run

    def test_a_meeting_that_cannot_be_trashed_is_put_back(self):
        projectnotes.write(self.alpha)
        self.open(self.alpha)
        with mock.patch.object(self.page, "_dialog"):
            self.assertEqual(self.delete("failed"), [])
        self.assertTrue(self.alpha.exists())
        self.assertTrue((self.meetings() / ALPHA_PAGE).is_file())

    def test_deleting_a_meeting_with_no_pages_costs_it_nothing(self):
        self.open(self.bravo)
        self.delete()
        self.assertFalse(self.bravo.exists())
        self.assertFalse(self.meetings().exists())

    def test_a_copy_that_cannot_be_taken_out_keeps_the_meeting(self):
        """Delete anyway and the transcript page stays in the project,
        with the record of where it is gone with the meeting."""
        projectnotes.write(self.alpha)
        self.open(self.alpha)
        self.page.notes.view.get_buffer().set_text("typed just now\n")
        denied = PermissionError(errno.EACCES, "Permission denied")
        for delete in (self.page._delete, self.page._delete_permanently):
            with self.subTest(delete=delete.__name__):
                with mock.patch.object(
                    projectnotes, "remove", side_effect=denied
                ), mock.patch.object(meeting, "forget_asst") as forget, \
                        mock.patch.object(meeting, "delete_session") as gone, \
                        self.assertLogs("steno.projectnotes", "WARNING"):
                    delete(self.page.current)
                    settle()
                forget.assert_not_called()  # its tasks stay filed too
                gone.assert_not_called()
                dialog = self.window.get_visible_dialog()
                self.assertEqual(
                    dialog.get_heading(),
                    "Could not take it out of the project's notes",
                )
                self.assertIn("Permission denied", dialog.get_body())
                self.assertIn("was not deleted", dialog.get_body())
                dialog.force_close()
                settle()
        # Still the open meeting, with its notes box and its copy kept up.
        self.assertEqual(self.page.current.dir, self.alpha)
        self.assertEqual(self.page.notes.session_dir, self.alpha)
        self.assertIn(self.alpha, self.rp._debounce)
        self.assertTrue((self.meetings() / ALPHA_PAGE).is_file())
        # Once the folder can be written, the same delete goes through.
        self.page._delete(self.page.current)
        self.settle()
        self.assertFalse(self.alpha.exists())
        self.assertFalse(self.meetings().exists())

    # --------------------------------------------------------------- problem

    def test_a_page_of_yours_in_the_way_is_left_and_the_window_says_so(self):
        projectnotes.write(self.alpha)
        self.open(self.alpha)
        self.rp._on_problem = self.toasts.append
        (self.meetings() / ALPHA_PAGE).write_text("mine\n")
        self.tick_second_item()
        with self.assertLogs("steno.projectnotes", "WARNING"):
            self.time.advance(DELAY_S)
            self.settle()
        self.assertEqual(self.page_text(), "mine\n")
        self.assertEqual(len(self.toasts), 1)
        self.assertIn(ALPHA_PAGE, self.toasts[0])


class TestInTheApp(Base):
    """The app, made and never started: no engine, no microphone."""

    def setUp(self):
        super().setUp()
        self.app = App(root=self.root)
        self.landed = SummaryReady(
            self.charlie, "Charlie", SUMMARY, requested=True
        )

    def land(self, event: SummaryReady) -> None:
        self.app._finish_summary(event)
        for worker in self.app._todo_workers:
            worker.join(10)
        settle()

    def test_a_summary_that_lands_is_in_its_project(self):
        (self.charlie / "meta.json").write_text('{"project": "other"}')
        self.land(self.landed)
        self.assertEqual(len(list(self.meetings("other").glob("*.md"))), 3)
        self.assertEqual(list((self.projects / "demo").iterdir()), [])
        self.assertTrue(load_todos(self.charlie))  # and the to-dos came first

    def test_one_with_no_project_goes_nowhere(self):
        self.land(self.landed)
        self.assertEqual(list(self.projects.rglob("notes")), [])

    def test_the_pages_list_the_to_dos_that_were_just_derived(self):
        (self.charlie / "meta.json").write_text('{"project": "other"}')
        (self.charlie / "todos.json").unlink()
        self.land(self.landed)
        page = next(p for p in self.meetings("other").glob("2026-10-07-*.md")
                    if "transcript" not in p.name)
        self.assertIn("- [ ] Send the usage figures by Friday",
                      page.read_text())

    def test_quitting_writes_what_was_waiting_for_a_pause(self):
        projectnotes.write(self.alpha)
        done = [{"text": "Draft the comparison table", "owner": "you",
                 "done": True}]
        self.alpha.joinpath("todos.json").write_text(json.dumps(done))
        self.app.rewriter = Rewriter(**self.time.timing)
        self.app.rewriter.changed(self.alpha)
        with mock.patch("steno.gui.app.clear_state"):
            self.app.do_shutdown()
        self.assertIn(DONE_ITEM, (self.meetings() / ALPHA_PAGE).read_text())


if __name__ == "__main__":
    unittest.main()
