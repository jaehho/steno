"""A meeting's pages in its project's notes/meetings, and only there.

Everything runs on a temp projects root that each call is given; no test
reads ~/projects or the data directory, and none needs a display.
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from steno import projectnotes
from steno.projectnotes import SWITCH_ENV

ROWS = [
    {"ts": 1000.0, "speaker": "you", "text": "morning", "offset": 0.0},
    {"ts": 1075.5, "speaker": "them", "text": "shall we start",
     "offset": 75.5, "who": "speaker_0"},
]
SUMMARY = """# Vendor Pricing Call

## TL;DR
- They will send revised numbers. <!--cite 201-221,460-->

## Action items
**You**
- Send the usage figures by Friday
- Draft the comparison table
"""
TODOS = [
    {"text": "Send the usage figures by Friday", "owner": "you",
     "done": True},
    {"text": "Draft the comparison table", "owner": "you"},
]
SUMMARY_NAME = "2026-09-28-1430-vendor-pricing-call.md"
TRANSCRIPT_NAME = "2026-09-28-1430-vendor-pricing-call.transcript.md"


def make_session(
    sessions: Path,
    name: str = "20260928-143000",
    *,
    title: str | None = "Vendor Pricing Call",
    project: str | None = "demo",
    todos: list[dict] | None = None,
) -> Path:
    """A summarized meeting, the way the app leaves one on disk."""
    folder = sessions / name
    folder.mkdir(parents=True)
    started = datetime.strptime(name[:15], "%Y%m%d-%H%M%S").timestamp()
    folder.joinpath("transcript.jsonl").write_text("".join(
        json.dumps(dict(r, ts=r["ts"] - 1000.0 + started)) + "\n"
        for r in ROWS
    ))
    folder.joinpath("summary.md").write_text(SUMMARY)
    folder.joinpath("todos.json").write_text(
        json.dumps(TODOS if todos is None else todos)
    )
    folder.joinpath("notes.md").write_text("ask about the discount\n")
    folder.joinpath("speakers.json").write_text(
        json.dumps({"speaker_0": "Alice"})
    )
    meta: dict = {"started_at": started}
    if title:
        meta["title"] = title
    if project:
        meta["project"] = project
    folder.joinpath("meta.json").write_text(json.dumps(meta))
    return folder


def edit_meta(session: Path, **changes: object) -> None:
    path = session / "meta.json"
    meta = json.loads(path.read_text())
    for key, value in changes.items():
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
    path.write_text(json.dumps(meta))


def meta_of(session: Path) -> dict:
    return json.loads((session / "meta.json").read_text())


def listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def snapshot(root: Path) -> dict[str, tuple]:
    """Every path under `root`, what is in it and when it changed."""
    found = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        if path.is_symlink():
            found[key] = ("link", str(path.readlink()))
        elif path.is_dir():
            found[key] = ("dir",)
        else:
            found[key] = ("file", path.read_bytes(), path.stat().st_mtime_ns)
    return found


class Base(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.projects = self.base / "projects"
        self.sessions = self.base / "sessions"
        for name in ("demo", "other"):
            (self.projects / name).mkdir(parents=True)
        self.sessions.mkdir()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(SWITCH_ENV, None)

    def meetings(self, project: str = "demo") -> Path:
        return self.projects / project / "notes" / "meetings"

    def names(self, project: str = "demo") -> list[str]:
        folder = self.meetings(project)
        return listing(folder) if folder.is_dir() else []

    def session(self, *args, **kwargs) -> Path:
        return make_session(self.sessions, *args, **kwargs)

    def publish(self, session: Path, **kwargs) -> projectnotes.Result:
        return projectnotes.publish(session, self.projects, **kwargs)

    def unpublish(self, session: Path, **kwargs) -> projectnotes.Result:
        return projectnotes.unpublish(session, self.projects, **kwargs)

    def read(self, name: str, project: str = "demo") -> str:
        return (self.meetings(project) / name).read_text()


class TestPublish(Base):
    def test_makes_the_exact_tree(self):
        d = self.session()
        result = self.publish(d)
        self.assertEqual(
            self.names(),
            [".gitignore", SUMMARY_NAME, TRANSCRIPT_NAME, "README.md"],
        )
        # Nothing outside notes/meetings, and no other project touched.
        self.assertEqual(
            sorted(str(p.relative_to(self.projects))
                   for p in self.projects.rglob("*")),
            sorted([
                "demo", "demo/notes", "demo/notes/meetings", "other",
                *(f"demo/notes/meetings/{n}" for n in self.names()),
            ]),
        )
        self.assertEqual(
            sorted((c.action, c.path.name) for c in result.changes),
            sorted(("create", n) for n in self.names()),
        )
        self.assertEqual(result.note, "")
        self.assertEqual(meta_of(d)["published"], {
            "project": "demo", "files": [SUMMARY_NAME, TRANSCRIPT_NAME],
        })
        # The rest of meta.json is as it was.
        self.assertEqual(meta_of(d)["title"], "Vendor Pricing Call")

    def test_the_summary_page(self):
        self.publish(self.session())
        page = self.read(SUMMARY_NAME)
        marker = (
            "<!-- Generated by steno from session 20260928-143000. "
            "Edits are overwritten. -->"
        )
        self.assertTrue(page.startswith(
            "---\n"
            'title: "Vendor Pricing Call"\n'
            "date: 2026-09-28\n"
            'time: "14:30"\n'
            "duration_min: 1\n"
            "project: demo\n"
            'session: "20260928-143000"\n'
            "generated_by: steno\n"
            f'transcript: "{TRANSCRIPT_NAME}"\n'
            "todos_open: 1\n"
            f"---\n{marker}\n\n# Vendor Pricing Call\n"
        ), page)
        self.assertIn("- [x] Send the usage figures by Friday", page)
        self.assertIn("- [ ] Draft the comparison table", page)
        self.assertIn("## Notes\nask about the discount", page)
        # The words are in their own file, linked from here.
        self.assertNotIn("morning", page)
        self.assertIn(f"[{TRANSCRIPT_NAME}]({TRANSCRIPT_NAME})", page)
        self.assertTrue(page.endswith("\n"))

    def test_the_cites_are_stamps_not_comments(self):
        self.publish(self.session())
        page = self.read(SUMMARY_NAME)
        self.assertIn("- They will send revised numbers. [03:21, 07:40]", page)
        self.assertNotIn("cite", page)

    def test_the_transcript_page(self):
        self.publish(self.session())
        page = self.read(TRANSCRIPT_NAME)
        self.assertIn("session: \"20260928-143000\"\n", page)
        self.assertIn("\n<!-- Generated by steno from session ", page)
        self.assertIn("[00:00] you: morning\n", page)
        self.assertIn("[01:15] Alice: shall we start\n", page)

    def test_republishing_the_same_meeting_changes_nothing(self):
        d = self.session()
        self.publish(d)
        for name in self.names():
            os.utime(self.meetings() / name, ns=(10**9, 10**9))
        before = snapshot(self.base)
        result = self.publish(d)
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(result.altered, [])
        self.assertEqual({c.action for c in result.changes}, {"same"})
        self.assertEqual(
            [n for n in self.names() if n.endswith(".tmp")], []
        )

    def test_a_new_title_renames_cleanly(self):
        d = self.session()
        self.publish(d)
        edit_meta(d, title="Vendor Follow Up")
        result = self.publish(d)
        new = "2026-09-28-1430-vendor-follow-up"
        self.assertEqual(
            self.names(),
            [".gitignore", f"{new}.md", f"{new}.transcript.md", "README.md"],
        )
        self.assertIn(
            ("remove", SUMMARY_NAME),
            [(c.action, c.path.name) for c in result.changes],
        )
        self.assertEqual(meta_of(d)["published"]["files"],
                         [f"{new}.md", f"{new}.transcript.md"])
        index = self.read("README.md")
        self.assertIn(f"[Vendor Follow Up]({new}.md)", index)
        self.assertNotIn(SUMMARY_NAME, index)

    def test_a_new_project_moves_the_meeting(self):
        d = self.session()
        self.publish(d)
        edit_meta(d, project="other")
        self.publish(d)
        self.assertFalse(self.meetings().exists())
        self.assertEqual(
            self.names("other"),
            [".gitignore", SUMMARY_NAME, TRANSCRIPT_NAME, "README.md"],
        )
        self.assertEqual(meta_of(d)["published"]["project"], "other")
        self.assertIn("project: other", self.read(SUMMARY_NAME, "other"))

    def test_clearing_the_project_takes_the_pages_away(self):
        d = self.session()
        self.publish(d)
        edit_meta(d, project=None)
        result = self.publish(d)
        self.assertEqual(self.names(), [])
        self.assertNotIn("published", meta_of(d))
        self.assertEqual(result.project, None)

    def test_a_meeting_with_no_project_is_published_nowhere(self):
        d = self.session(project=None)
        before = snapshot(self.base)
        result = self.publish(d)
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(result.changes, [])

    def test_a_meeting_nobody_spoke_in_is_published_nowhere(self):
        d = self.session()
        d.joinpath("transcript.jsonl").write_text("")
        result = self.publish(d)
        self.assertEqual(self.names(), [])
        self.assertIn("nothing was said", result.note)

    def test_a_project_that_is_gone_keeps_the_record(self):
        d = self.session()
        self.publish(d)
        recorded = meta_of(d)["published"]
        shutil.move(self.projects / "demo", self.base / "away")
        result = self.publish(d)
        self.assertIn("no such project", result.note)
        self.assertEqual(meta_of(d)["published"], recorded)

    def test_a_long_title_stays_a_file_name(self):
        d = self.session(title="word " * 200)
        self.publish(d)
        longest = max(len(n) for n in self.names())
        self.assertLess(longest, 120)

    def test_a_title_with_no_letters_names_the_page_by_its_minute(self):
        d = self.session(title="???")
        self.publish(d)
        self.assertIn("2026-09-28-1430.md", self.names())

    def test_two_meetings_of_a_minute_and_title_both_stay(self):
        a = self.session("20260928-143001")
        b = self.session("20260928-143045")
        self.publish(a)
        self.publish(b)
        self.assertEqual(
            self.names(),
            [".gitignore", SUMMARY_NAME, TRANSCRIPT_NAME,
             "2026-09-28-143045-vendor-pricing-call.md",
             "2026-09-28-143045-vendor-pricing-call.transcript.md",
             "README.md"],
        )
        self.assertIn("20260928-143001", self.read(SUMMARY_NAME))
        self.assertIn("20260928-143045",
                      self.read("2026-09-28-143045-vendor-pricing-call.md"))
        before = snapshot(self.base)
        self.assertEqual(self.publish(a).altered, [])
        self.assertEqual(self.publish(b).altered, [])
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(self.read("README.md").count("\n- "), 2)

    def test_the_project_by_a_second_name_does_not_lose_its_pages(self):
        (self.projects / "alias").symlink_to(self.projects / "demo")
        d = self.session()
        self.publish(d)
        before = self.names()
        edit_meta(d, project="alias")
        self.publish(d)
        self.assertEqual(self.names(), before)
        self.assertEqual(meta_of(d)["published"]["project"], "alias")

    def test_a_failed_write_leaves_the_old_page_and_no_temp_file(self):
        d = self.session()
        self.publish(d)
        old = self.read(SUMMARY_NAME)
        d.joinpath("todos.json").write_text(json.dumps(TODOS[:1]))
        full = OSError(errno.ENOSPC, "No space left on device")
        with (
            mock.patch.object(Path, "replace", side_effect=full),
            self.assertRaises(OSError),
        ):
            self.publish(d)
        self.assertEqual(self.read(SUMMARY_NAME), old)
        self.assertEqual(
            [n for n in self.names() if n.endswith(".tmp")], []
        )
        # The record was not touched, and the next run does the work.
        self.assertEqual(meta_of(d)["published"]["files"],
                         [SUMMARY_NAME, TRANSCRIPT_NAME])
        self.publish(d)
        self.assertNotEqual(self.read(SUMMARY_NAME), old)


class TestUnpublish(Base):
    def test_removes_the_pages_and_the_index_line_but_not_the_folder(self):
        a = self.session("20260928-143000", title="Alpha")
        b = self.session("20261006-161200", title="Bravo")
        self.publish(a)
        self.publish(b)
        result = self.unpublish(a)
        self.assertEqual(
            self.names(),
            [".gitignore", "2026-10-06-1612-bravo.md",
             "2026-10-06-1612-bravo.transcript.md", "README.md"],
        )
        index = self.read("README.md")
        self.assertIn("[Bravo]", index)
        self.assertNotIn("Alpha", index)
        self.assertEqual(len(result.altered), 3)  # two pages, the index
        self.assertNotIn("published", meta_of(a))
        self.assertIn("published", meta_of(b))

    def test_the_last_meeting_takes_the_folder_with_it(self):
        d = self.session()
        self.publish(d)
        self.unpublish(d)
        self.assertFalse(self.meetings().exists())
        # The project, and the notes folder it may have had, stay.
        self.assertTrue((self.projects / "demo").is_dir())

    def test_the_last_meeting_leaves_a_gitignore_somebody_changed(self):
        d = self.session()
        self.publish(d)
        (self.meetings() / ".gitignore").write_text("mine\n")
        self.unpublish(d)
        self.assertEqual(self.names(), [".gitignore"])
        self.assertEqual(self.read(".gitignore"), "mine\n")

    def test_files_of_the_folder_that_are_not_steno_s_stay(self):
        d = self.session()
        self.publish(d)
        (self.meetings() / "mine.md").write_text("hand-made\n")
        self.unpublish(d)
        self.assertEqual(self.names(), [".gitignore", "mine.md"])

    def test_a_meeting_never_published(self):
        d = self.session()
        before = snapshot(self.base)
        result = self.unpublish(d)
        self.assertEqual(result.note, "not published")
        self.assertEqual(snapshot(self.base), before)

    def test_works_with_the_switch_off(self):
        d = self.session()
        self.publish(d)
        os.environ[SWITCH_ENV] = "0"
        self.unpublish(d)
        self.assertEqual(self.names(), [])


class TestWhatIsNotSteno(Base):
    def test_a_hand_made_page_is_not_overwritten(self):
        d = self.session()
        self.meetings().mkdir(parents=True)
        mine = self.meetings() / SUMMARY_NAME
        mine.write_text("my own notes on that call\n")
        with self.assertLogs("steno.projectnotes", "WARNING"):
            result = self.publish(d)
        self.assertEqual(mine.read_text(), "my own notes on that call\n")
        self.assertEqual(
            [(c.path.name, c.note) for c in result.skipped],
            [(SUMMARY_NAME, "not written by steno")],
        )
        # The transcript has its own name, and goes in.
        self.assertIn(TRANSCRIPT_NAME, self.names())
        self.assertEqual(meta_of(d)["published"]["files"], [TRANSCRIPT_NAME])
        # Hand-made, so no line in an index, which has nothing to list.
        self.assertNotIn("README.md", self.names())
        self.unpublish(d)
        self.assertEqual(mine.read_text(), "my own notes on that call\n")

    def test_a_hand_made_index_is_not_overwritten(self):
        d = self.session()
        self.meetings().mkdir(parents=True)
        (self.meetings() / "README.md").write_text("# My meetings\n")
        with self.assertLogs("steno.projectnotes", "WARNING"):
            result = self.publish(d)
        self.assertEqual(self.read("README.md"), "# My meetings\n")
        self.assertEqual([c.path.name for c in result.skipped], ["README.md"])
        self.assertIn(SUMMARY_NAME, self.names())

    def test_a_page_that_lost_its_marker_is_the_user_s(self):
        d = self.session()
        self.publish(d)
        page = self.meetings() / SUMMARY_NAME
        text = page.read_text()
        kept = re.sub(r"<!-- Generated by steno.*-->\n", "", text)
        page.write_text(kept + "my addition\n")
        with self.assertLogs("steno.projectnotes", "WARNING"):
            self.publish(d)
        self.assertEqual(page.read_text(), kept + "my addition\n")
        self.unpublish(d)
        self.assertEqual(page.read_text(), kept + "my addition\n")

    def test_the_marker_far_down_a_file_does_not_make_it_steno_s(self):
        d = self.session()
        self.meetings().mkdir(parents=True)
        mine = self.meetings() / SUMMARY_NAME
        mine.write_text(
            "# Mine\n\n" + projectnotes.marker("20260928-143000") + "\n"
        )
        with self.assertLogs("steno.projectnotes", "WARNING"):
            self.publish(d)
        self.assertTrue(mine.read_text().startswith("# Mine"))

    def test_a_directory_in_the_way_is_left_alone(self):
        d = self.session()
        (self.meetings() / SUMMARY_NAME).mkdir(parents=True)
        with self.assertLogs("steno.projectnotes", "WARNING"):
            result = self.publish(d)
        self.assertEqual(
            [(c.path.name, c.note) for c in result.skipped],
            [(SUMMARY_NAME, "is a directory")],
        )

    def test_a_link_in_the_way_is_not_followed(self):
        d = self.session()
        precious = self.base / "precious.md"
        precious.write_text("do not touch\n")
        self.meetings().mkdir(parents=True)
        (self.meetings() / SUMMARY_NAME).symlink_to(precious)
        with self.assertLogs("steno.projectnotes", "WARNING"):
            result = self.publish(d)
        self.assertEqual(precious.read_text(), "do not touch\n")
        self.assertTrue((self.meetings() / SUMMARY_NAME).is_symlink())
        self.assertEqual(result.skipped[0].note, "is a symbolic link")

    def test_a_damaged_record_cannot_name_a_file_outside(self):
        d = self.session()
        victim = self.projects / "demo" / "victim.md"
        victim.write_text(projectnotes.marker("x") + "\nmine\n")
        far = self.base / "far.md"
        far.write_text(projectnotes.marker("x") + "\nmine\n")
        self.publish(d)
        hostile = ["../../victim.md", str(far), ".gitignore", "a/b.md"]
        edit_meta(d, published={"project": "demo", "files": hostile})
        with self.assertLogs("steno.projectnotes", "WARNING"):
            self.publish(d)
        edit_meta(d, published={"project": "demo", "files": hostile})
        with self.assertLogs("steno.projectnotes", "WARNING"):
            result = self.unpublish(d)
        mine = projectnotes.marker("x") + "\nmine\n"
        self.assertEqual(victim.read_text(), mine)
        self.assertEqual(far.read_text(), mine)
        self.assertEqual(
            [c.note.split(":")[0] for c in result.skipped],
            ["not a page"] * len(hostile),
        )

    def test_a_page_another_meeting_took_over_is_not_removed(self):
        a = self.session("20260928-143001")
        b = self.session("20260928-143045")
        self.publish(a)
        for name in (SUMMARY_NAME, TRANSCRIPT_NAME):
            (self.meetings() / name).unlink()
        self.publish(b)  # takes the names a had
        with self.assertLogs("steno.projectnotes", "WARNING"):
            result = self.publish(a)
        self.assertIn("20260928-143045", self.read(SUMMARY_NAME))
        self.assertEqual(
            {c.note for c in result.skipped}, {"a page of another meeting"}
        )
        self.assertEqual(
            meta_of(a)["published"]["files"],
            ["2026-09-28-143001-vendor-pricing-call.md",
             "2026-09-28-143001-vendor-pricing-call.transcript.md"],
        )
        self.assertEqual(self.read("README.md").count("\n- "), 2)

    def test_a_damaged_record_cannot_name_a_project_outside(self):
        d = self.session()
        (self.base / "etc").mkdir()
        mine = projectnotes.marker("x") + "\nmine\n"
        (self.base / "etc" / SUMMARY_NAME).write_text(mine)
        edit_meta(d, published={
            "project": "../etc", "files": [SUMMARY_NAME],
        })
        result = self.unpublish(d)
        self.assertEqual((self.base / "etc" / SUMMARY_NAME).read_text(), mine)
        self.assertIn("not a project directory name", result.skipped[0].note)

    def test_a_record_that_is_not_one_is_ignored(self):
        d = self.session()
        for junk in ("yes", [1], {"project": 3, "files": []},
                     {"project": "demo", "files": "x"}):
            edit_meta(d, published=junk)
            self.assertIsNone(projectnotes.read_record(d))


class TestOnlyInsideTheProject(Base):
    made = 0

    def refused(self, project: str) -> projectnotes.Result:
        self.made += 1
        d = self.session(f"2026092{self.made}-143000", project=project)
        before = snapshot(self.base)
        result = self.publish(d)
        self.assertEqual(snapshot(self.base), before, project)
        return result

    def test_a_name_that_is_not_a_plain_directory_is_refused(self):
        (self.base / "etc").mkdir()
        (self.base / "etc" / "notes").mkdir()
        for project in ("../etc", "..", ".", "/tmp", "a/b", ".hidden",
                        "demo/../other", "demo/"):
            with self.subTest(project=project):
                result = self.refused(project)
                self.assertIn("not a project directory name", result.note)
                self.assertEqual(result.changes, [])

    def test_a_project_that_does_not_exist_is_not_made(self):
        result = self.refused("ghost")
        self.assertIn("no such project", result.note)
        self.assertFalse((self.projects / "ghost").exists())

    def test_a_project_that_is_a_file_is_refused(self):
        (self.projects / "file").write_text("x")
        self.assertIn("no such project", self.refused("file").note)

    def test_a_notes_link_that_leaves_the_project_is_refused(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.projects / "demo" / "notes").symlink_to(outside)
        d = self.session()
        result = self.publish(d)
        self.assertIn("leads out of", result.note)
        self.assertEqual(listing(outside), [])
        self.assertNotIn("published", meta_of(d))

    def test_a_meetings_link_that_leaves_the_project_is_refused(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.projects / "demo" / "notes").mkdir()
        (self.projects / "demo" / "notes" / "meetings").symlink_to(outside)
        result = self.publish(self.session())
        self.assertIn("leads out of", result.note)
        self.assertEqual(listing(outside), [])

    def test_a_link_to_a_folder_inside_the_project_is_fine(self):
        docs = self.projects / "demo" / "docs"
        docs.mkdir()
        (self.projects / "demo" / "notes").symlink_to(docs)
        self.publish(self.session())
        self.assertIn(SUMMARY_NAME, listing(docs / "meetings"))

    def test_a_meetings_that_is_a_file_is_refused(self):
        (self.projects / "demo" / "notes").mkdir()
        (self.projects / "demo" / "notes" / "meetings").write_text("x")
        result = self.publish(self.session())
        self.assertIn("is not a directory", result.note)

    def test_unpublishing_cannot_leave_the_project_either(self):
        d = self.session()
        self.publish(d)
        outside = self.base / "outside"
        outside.mkdir()
        shutil.move(self.projects / "demo" / "notes", self.base / "kept")
        (self.projects / "demo" / "notes").symlink_to(outside)
        (outside / "meetings").mkdir()
        shutil.copy(
            self.base / "kept" / "meetings" / SUMMARY_NAME,
            outside / "meetings" / SUMMARY_NAME,
        )
        result = self.unpublish(d)
        self.assertEqual(listing(outside / "meetings"), [SUMMARY_NAME])
        self.assertEqual(result.altered, [])
        self.assertEqual(len(result.skipped), 1)


class TestGitignore(Base):
    def test_ignores_everything_and_says_why(self):
        self.publish(self.session())
        text = self.read(".gitignore")
        lines = [x for x in text.splitlines() if not x.startswith("#")]
        self.assertEqual(lines, ["*", "!.gitignore"])
        self.assertIn("other people's speech", text)
        self.assertIn("Delete this file", text)

    def test_is_written_once_and_not_rewritten(self):
        d = self.session()
        self.publish(d)
        path = self.meetings() / ".gitignore"
        path.write_text("# mine\n*\n")
        os.utime(path, ns=(10**9, 10**9))
        d.joinpath("todos.json").write_text(json.dumps(TODOS[:1]))
        self.publish(d)  # the pages change; the ignore does not
        self.assertEqual(path.read_text(), "# mine\n*\n")
        self.assertEqual(path.stat().st_mtime_ns, 10**9)

    def test_deleting_it_makes_the_meetings_committable(self):
        d = self.session()
        self.publish(d)
        (self.meetings() / ".gitignore").unlink()
        self.publish(d)
        self.assertNotIn(".gitignore", self.names())
        # Not for the next meeting either.
        self.publish(self.session("20261006-161200", title="Bravo"))
        self.assertNotIn(".gitignore", self.names())

    def test_a_folder_that_is_new_to_steno_gets_one(self):
        self.meetings().mkdir(parents=True)
        (self.meetings() / "mine.md").write_text("hand-made\n")
        self.publish(self.session())
        self.assertIn(".gitignore", self.names())


class TestSwitch(Base):
    def test_off_writes_nothing(self):
        d = self.session()
        before = snapshot(self.base)
        for value in ("0", "false", "off", "no", "nope", " 0 "):
            with self.subTest(value=value):
                os.environ[SWITCH_ENV] = value
                result = self.publish(d)
                self.assertEqual(snapshot(self.base), before)
                self.assertEqual(result.changes, [])
                self.assertIn(SWITCH_ENV, result.note)

    def test_on_by_default_and_when_asked(self):
        for value in (None, "", "1", "true", "yes", "on", "ON"):
            with self.subTest(value=value):
                if value is None:
                    os.environ.pop(SWITCH_ENV, None)
                else:
                    os.environ[SWITCH_ENV] = value
                self.assertTrue(projectnotes.enabled())

    def test_force_is_for_a_person_at_the_keyboard(self):
        os.environ[SWITCH_ENV] = "0"
        result = self.publish(self.session(), force=True)
        self.assertEqual(result.note, "")
        self.assertIn(SUMMARY_NAME, self.names())

    def test_the_index_follows_the_switch_too(self):
        d = self.session()
        self.publish(d)
        (self.meetings() / "README.md").unlink()
        os.environ[SWITCH_ENV] = "0"
        projectnotes.rebuild_index("demo", self.projects)
        self.assertNotIn("README.md", self.names())


class TestIndex(Base):
    def lines(self, project: str = "demo") -> list[str]:
        return [x for x in self.read("README.md", project).splitlines()
                if x.startswith("- ")]

    def test_is_newest_first(self):
        todos = [{"text": "one", "owner": "you"},
                 {"text": "two", "owner": "you"}]
        for name, title, kw in (
            ("20260901-090000", "Alpha", {"todos": []}),
            ("20261006-161200", "Charlie", {"todos": todos}),
            ("20260915-120000", "Bravo", {"todos": todos[:1]}),
        ):
            self.publish(self.session(name, title=title, **kw))
        self.assertEqual(self.lines(), [
            "- 2026-10-06 16:12"
            " · [Charlie](2026-10-06-1612-charlie.md)"
            " · 1 min · 2 open to-dos",
            "- 2026-09-15 12:00"
            " · [Bravo](2026-09-15-1200-bravo.md)"
            " · 1 min · 1 open to-do",
            "- 2026-09-01 09:00"
            " · [Alpha](2026-09-01-0900-alpha.md) · 1 min",
        ])
        first = self.read("README.md").splitlines()[0]
        self.assertTrue(first.startswith(projectnotes.MARKER_PREFIX))

    def test_lists_only_what_steno_made(self):
        self.publish(self.session())
        (self.meetings() / "mine.md").write_text("# Mine\n")
        (self.meetings() / "other-session.md").write_text(
            "---\nsession: x\n---\n# Not generated\n"
        )
        self.publish(self.session("20261006-161200", title="Bravo"))
        self.assertEqual(len(self.lines()), 2)
        self.assertNotIn("mine.md", self.read("README.md"))

    def test_a_title_cannot_break_its_line(self):
        title = 'He said: "yes" & #1 [draft]\n— 100%'
        d = self.session(title=title)
        self.publish(d)
        [name] = [n for n in self.names()
                  if n.endswith(".md") and "transcript" not in n
                  and n != "README.md"]
        entry = projectnotes.entry_of(name, self.read(name))
        assert entry is not None
        self.assertEqual(entry.title, title)
        self.assertIn(r"[He said: \"yes\" & #1 \[draft\] — 100%]".replace(
            '\\"', '"'), self.lines()[0])

    def test_rebuild_restores_a_lost_index_to_the_same_bytes(self):
        self.publish(self.session())
        self.publish(self.session("20261006-161200", title="Bravo"))
        index = self.read("README.md")
        (self.meetings() / "README.md").unlink()
        projectnotes.rebuild_index("demo", self.projects)
        self.assertEqual(self.read("README.md"), index)

    def test_rebuild_does_not_make_a_folder(self):
        result = projectnotes.rebuild_index("demo", self.projects)
        self.assertEqual(result.changes, [])
        self.assertFalse((self.projects / "demo" / "notes").exists())


class TestDryRun(Base):
    def check(self, act) -> projectnotes.Result:
        """A dry run changes nothing; `act` then does what it said."""
        before = snapshot(self.base)
        dry = act(dry_run=True)
        self.assertEqual(snapshot(self.base), before)
        real = act()
        self.assertEqual(dry.changes, real.changes)
        self.assertEqual(dry.note, real.note)
        self.assertNotEqual(snapshot(self.base), before)
        return real

    def test_reports_what_the_real_run_does(self):
        a = self.session("20260928-143000", title="Alpha")
        b = self.session("20261006-161200", title="Bravo")
        self.check(lambda **kw: self.publish(a, **kw))
        self.check(lambda **kw: self.publish(b, **kw))
        (a / "todos.json").write_text(json.dumps(TODOS[:1]))
        self.assertEqual(
            [c.action for c in self.check(
                lambda **kw: self.publish(a, **kw)).altered],
            ["update", "update"],
        )
        edit_meta(a, title="Alpha Again")
        self.check(lambda **kw: self.publish(a, **kw))
        edit_meta(a, project="other")
        self.check(lambda **kw: self.publish(a, **kw))
        self.check(lambda **kw: self.unpublish(b, **kw))
        # The last one out: its pages, the index, the folder.
        result = self.check(lambda **kw: self.unpublish(a, **kw))
        self.assertEqual(
            [(c.action, c.path.name) for c in result.changes][-1],
            ("remove", "meetings"),
        )


class TestWhereCallersCannotRaise(Base):
    """A worker thread and a terminal line have no one to raise to."""

    def test_a_failed_write_comes_back_as_an_error_not_an_exception(self):
        d = self.session()
        full = OSError(errno.ENOSPC, "No space left on device")
        with (
            mock.patch.object(Path, "replace", side_effect=full),
            self.assertLogs("steno.projectnotes", "WARNING"),
        ):
            result = projectnotes.try_publish(d, self.projects)
        self.assertIn("No space left", result.error)
        self.assertEqual(result.project, "demo")
        self.assertNotIn("published", meta_of(d))

    def test_a_failed_removal_too(self):
        d = self.session()
        self.publish(d)
        denied = OSError(errno.EACCES, "Permission denied")
        with (
            mock.patch.object(Path, "unlink", side_effect=denied),
            self.assertLogs("steno.projectnotes", "WARNING"),
        ):
            result = projectnotes.try_unpublish(d, self.projects)
        self.assertIn("Permission denied", result.error)
        self.assertIn("published", meta_of(d))

    def test_when_nothing_fails_it_is_publish(self):
        d = self.session()
        result = projectnotes.try_publish(d, self.projects)
        self.assertEqual(result.error, "")
        self.assertEqual(self.names(), [
            ".gitignore", SUMMARY_NAME, TRANSCRIPT_NAME, "README.md"])


class TestPublishedFolder(Base):
    def test_it_is_where_the_pages_are_once_there_are_some(self):
        d = self.session()
        self.assertIsNone(projectnotes.published_folder(d, self.projects))
        self.publish(d)
        self.assertEqual(
            projectnotes.published_folder(d, self.projects),
            self.meetings().resolve(),
        )
        self.unpublish(d)
        self.assertIsNone(projectnotes.published_folder(d, self.projects))

    def test_a_record_whose_pages_are_gone_is_not_published(self):
        d = self.session()
        self.publish(d)
        shutil.rmtree(self.projects / "demo" / "notes")
        self.assertIsNone(projectnotes.published_folder(d, self.projects))
        shutil.rmtree(self.projects / "demo")
        self.assertIsNone(projectnotes.published_folder(d, self.projects))


class TestPageText(unittest.TestCase):
    def test_front_matter_values_are_valid_yaml_scalars(self):
        text = projectnotes.frontmatter({
            "a": "steno", "b": "yes", "c": "x: y # z", "d": 3,
            "e": date(2026, 1, 2), "f": "14:30", "g": "한글",
        })
        self.assertEqual(text, (
            '---\na: steno\nb: "yes"\nc: "x: y # z"\nd: 3\n'
            'e: 2026-01-02\nf: "14:30"\ng: "한글"\n---'
        ))

    def test_is_generated_means_the_first_line_after_the_front_matter(self):
        marker = projectnotes.marker("s1")
        front = projectnotes.frontmatter({"session": "s1"})
        for text, expected in (
            (f"{front}\n{marker}\n\n# x\n", True),
            (f"{marker}\n# index\n", True),
            (f"{front}\n\n{marker}\n", True),
            (f"{front}\n# x\n\n{marker}\n", False),
            (f"{front}\n# x\n", False),
            (f"# x\n{marker}\n", False),
            (f"---\nsession: s1\n{marker}\n", False),
            ("", False),
        ):
            with self.subTest(text=text):
                self.assertEqual(projectnotes.is_generated(text), expected)

    def test_the_stem_is_the_minute_and_the_title(self):
        when = datetime(2026, 10, 6, 16, 12, 45)
        self.assertEqual(
            projectnotes.page_stem(when, "RCNN toolbox!"),
            "2026-10-06-1612-rcnn-toolbox",
        )
        self.assertEqual(
            projectnotes.page_stem(when, "RCNN toolbox!", seconds=True),
            "2026-10-06-161245-rcnn-toolbox",
        )
        self.assertEqual(projectnotes.page_stem(when, None),
                         "2026-10-06-1612")


if __name__ == "__main__":
    unittest.main()
