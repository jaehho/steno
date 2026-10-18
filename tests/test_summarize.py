"""Transcript handling and session resolution — everything up to the API call."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steno import asstlink, citations, claude
from steno.notes import load_todos, set_done_local, sync_todos
from steno.speakers import load_names, save_names
from steno.summarize import (
    duration_minutes,
    format_for_model,
    has_body,
    has_summary,
    iter_sessions,
    load_records,
    load_title,
    parse_summary,
    resolve_session,
    save_project,
    summarize_session,
)

ROWS = [
    {"ts": 1000.0, "speaker": "you", "text": "morning"},
    {"ts": 1075.5, "speaker": "them", "text": "shall we start"},
]


def _session(root: Path, name: str, rows=ROWS) -> Path:
    d = root / name
    d.mkdir(parents=True)
    with (d / "transcript.jsonl").open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return d


class TestRecords(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_reads_rows(self):
        d = _session(self.root, "20260101-090000")
        self.assertEqual(len(load_records(d)), 2)

    def test_sorts_by_time(self):
        """The mic is merged in after the fact, so order can't be trusted."""
        d = _session(self.root, "20260101-090000", list(reversed(ROWS)))
        self.assertEqual([r["ts"] for r in load_records(d)], [1000.0, 1075.5])

    def test_tolerates_a_truncated_final_line(self):
        """A killed session leaves a half-written line; the rest still counts."""
        d = _session(self.root, "20260101-090000")
        with (d / "transcript.jsonl").open("a") as fh:
            fh.write('{"ts": 1100.0, "speaker": "them", "te')
        self.assertEqual(len(load_records(d)), 2)

    def test_missing_transcript(self):
        (self.root / "empty").mkdir()
        self.assertEqual(load_records(self.root / "empty"), [])

    def test_format_is_relative_to_the_start(self):
        out = format_for_model(ROWS)
        self.assertIn("[00:00] you: morning", out)
        self.assertIn("[01:15] them: shall we start", out)

    def test_format_of_nothing(self):
        self.assertEqual(format_for_model([]), "")

    def test_duration(self):
        self.assertAlmostEqual(duration_minutes(ROWS), 1.2583, places=3)

    def test_duration_of_one_line(self):
        self.assertEqual(duration_minutes(ROWS[:1]), 0.0)


class TestSummaryParsing(unittest.TestCase):
    def test_takes_the_leading_heading_as_title(self):
        s = parse_summary("# Vendor Pricing Call\n\n## TL;DR\n- x")
        self.assertEqual(s.title, "Vendor Pricing Call")

    def test_body_keeps_the_heading(self):
        s = parse_summary("# T\n\nbody")
        self.assertTrue(s.body.startswith("# T"))

    def test_missing_heading_falls_back(self):
        self.assertEqual(parse_summary("no heading here").title, "Untitled meeting")

    def test_title_only_has_no_body(self):
        """A flash model sometimes stops after `# Title`; that is not a summary."""
        self.assertFalse(has_body("# Vendor Pricing Call"))
        self.assertFalse(has_body("# Vendor Pricing Call\n\n  \n"))

    def test_a_marker_alone_is_not_a_body(self):
        self.assertFalse(has_body("# T\n\n<!--cite 1-5-->\n"))
        self.assertFalse(has_body("# T <!--cite 1-->\n"))
        self.assertTrue(has_body("# T\n\n- x <!--cite 1-5-->\n"))

    def test_the_title_never_carries_a_marker_the_body_keeps_its_own(self):
        s = parse_summary("# Pricing call <!--cite 1-->\n\n- x <!--cite 1-5-->")
        self.assertEqual(s.title, "Pricing call")
        self.assertIn("<!--cite 1-5-->", s.body)

    def test_a_section_counts_as_a_body(self):
        self.assertTrue(has_body("# T\n\n## TL;DR\n- x"))
        self.assertTrue(has_body("# T\n\nThey agreed on Thursday."))


class TestHasSummary(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.a = _session(self.root, "20260101-090000")

    def test_missing_file(self):
        self.assertFalse(has_summary(self.a))

    def test_title_only_is_not_summarized(self):
        """Otherwise `steno status` reports done and never retries it."""
        (self.a / "summary.md").write_text("# Only a title\n")
        self.assertFalse(has_summary(self.a))

    def test_a_marker_only_summary_is_not_summarized(self):
        (self.a / "summary.md").write_text("# T\n\n<!--cite 1-5-->\n")
        self.assertFalse(has_summary(self.a))

    def test_a_real_summary_counts(self):
        (self.a / "summary.md").write_text("# T\n\n## TL;DR\n- x\n")
        self.assertTrue(has_summary(self.a))


class TestSessionResolution(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.a = _session(self.root, "20260101-090000")
        self.b = _session(self.root, "20260202-100000")

    def test_lists_newest_first(self):
        self.assertEqual([p.name for p in iter_sessions(self.root)],
                         ["20260202-100000", "20260101-090000"])

    def test_skips_dirs_without_a_transcript(self):
        (self.root / "20260303-110000").mkdir()
        self.assertEqual(len(iter_sessions(self.root)), 2)

    def test_none_resolves_to_the_newest(self):
        self.assertEqual(resolve_session(None, self.root), self.b)

    def test_resolves_by_name(self):
        self.assertEqual(resolve_session("20260101-090000", self.root), self.a)

    def test_resolves_by_path(self):
        self.assertEqual(resolve_session(str(self.a), self.root), self.a)

    def test_unknown_name_raises(self):
        with self.assertRaises(FileNotFoundError):
            resolve_session("nope", self.root)

    def test_empty_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            resolve_session(None, Path(tempfile.mkdtemp()))

    def test_title_is_none_before_summarizing(self):
        self.assertIsNone(load_title(self.a))

    def test_title_reads_meta(self):
        (self.a / "meta.json").write_text(json.dumps({"title": "Kickoff"}))
        self.assertEqual(load_title(self.a), "Kickoff")

    def test_corrupt_meta_is_not_fatal(self):
        (self.a / "meta.json").write_text("{broken")
        self.assertIsNone(load_title(self.a))


if __name__ == "__main__":
    unittest.main()


class TestFormatForModel(unittest.TestCase):
    """Issue 8: the copy that leaves for a model is scrubbed; disk is not."""

    def test_pii_is_replaced(self):
        out = format_for_model(
            [{"ts": 60.0, "speaker": "them", "text": "mail jane@example.com at 415-555-0100"}]
        )
        self.assertIn("[EMAIL]", out)
        self.assertIn("[PHONE]", out)
        self.assertNotIn("jane@example.com", out)

    def test_keeps_speaker_and_stamp(self):
        out = format_for_model([
            {"ts": 100.0, "speaker": "them", "text": "hi"},
            {"ts": 165.0, "speaker": "you", "text": "on it"},
        ])
        self.assertEqual(out, "[00:00] them: hi\n[01:05] you: on it")

    def test_names_reach_the_model(self):
        """Issue 7: a diarized voice becomes a name the summary can attribute to."""
        out = format_for_model(
            [{"ts": 0.0, "speaker": "them", "who": "speaker_0",
              "text": "I will send it"}],
            names={"speaker_0": "Alice"},
        )
        self.assertEqual(out, "[00:00] Alice: I will send it")

    def test_unnamed_voice_stays_them(self):
        out = format_for_model(
            [{"ts": 0.0, "speaker": "them", "who": "speaker_1", "text": "ok"}]
        )
        self.assertIn("them: ok", out)

    def test_can_skip_redaction(self):
        out = format_for_model(
            [{"ts": 0.0, "speaker": "them", "text": "a@b.com"}], redact=False
        )
        self.assertIn("a@b.com", out)

    def test_cited_lines_carry_an_id_and_the_gutters_time(self):
        out = format_for_model(ROWS, offsets=[5, 80])
        self.assertEqual(
            out, "[L0 00:05] you: morning\n[L1 01:20] them: shall we start"
        )

    def test_cited_lines_count_hours_like_the_gutter(self):
        out = format_for_model(ROWS[:1], offsets=[3725])
        self.assertEqual(out, "[L0 1:02:05] you: morning")


SESSION_ROWS = [
    {"ts": 1005.0, "speaker": "them", "text": "shall we start"},
    {"ts": 1009.0, "speaker": "you", "text": "sure"},
    {"ts": 1021.0, "speaker": "them",
     "text": "can you send the usage figures by Friday"},
    {"ts": 1024.0, "speaker": "you", "text": "yes, I will send them Thursday"},
    {"ts": 1090.0, "speaker": "them",
     "text": "the discount may be retroactive"},
]

CITED_REPLY = """\
# Vendor Pricing Call

## TL;DR
- They want the usage figures by Friday. [L2-3]
- Nothing else was settled.

## Action items
**You**
- Send the usage figures by Friday [L2-3]

## Open questions
- Whether the discount is retroactive [L4]
"""


class FakeModel:
    """A stand-in for claude.run_structured: replies in order, the last one
    for every call after, and a record of what it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def __call__(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        last = len(self.replies) == 1
        reply = self.replies[0] if last else self.replies.pop(0)
        if isinstance(reply, str):
            reply = {"summary": reply, "project": None}
        return reply


class TestSummarizeSession(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.dir = _session(self.root, "20260101-090000", SESSION_ROWS)
        (self.dir / "meta.json").write_text(
            json.dumps({"started_at": 1000.0}) + "\n"
        )
        self.projects = Path(tempfile.mkdtemp())

    def run_with(self, *replies):
        fake = FakeModel(*replies)
        with mock.patch.object(claude, "run_structured", fake):
            summary = asyncio.run(
                summarize_session(self.dir, "m", None, self.projects)
            )
        self.fake = fake
        return summary

    def written(self) -> str:
        return (self.dir / "summary.md").read_text()

    def test_the_model_sees_line_ids_at_the_gutters_times(self):
        self.run_with(CITED_REPLY)
        prompt, kwargs = self.fake.calls[0]
        self.assertIn("[L0 00:05] them: shall we start", prompt)
        self.assertIn(
            "[L4 01:30] them: the discount may be retroactive", prompt
        )
        self.assertIn("[L12-15, L40]", kwargs["system"])

    def test_a_recorded_offset_beats_the_timestamp(self):
        """The number the gutter shows, so a cite lands on the same row."""
        rows = [dict(r, offset=r["ts"] - 1000.0 + 2.0) for r in SESSION_ROWS]
        self.dir = _session(self.root, "20260102-090000", rows)
        self.run_with(CITED_REPLY)
        self.assertIn("[L0 00:07] them", self.fake.calls[0][0])

    def test_tags_become_markers_in_seconds(self):
        self.run_with(CITED_REPLY)
        text = self.written()
        self.assertNotIn("[L", text)
        self.assertIn(
            "- Send the usage figures by Friday <!--cite 21-24-->", text
        )
        self.assertIn("retroactive <!--cite 90-->", text)
        self.assertIn("- Nothing else was settled.\n", text)

    def test_the_body_handed_on_is_what_was_written(self):
        """to-dos are made from it, so it has to carry the markers."""
        summary = self.run_with(CITED_REPLY)
        self.assertEqual(summary.body + "\n", self.written())
        self.assertEqual(summary.title, "Vendor Pricing Call")
        self.assertEqual(summary.coverage, citations.Coverage(4, 3))

    def test_meta_says_how_much_is_cited(self):
        self.run_with(CITED_REPLY)
        meta = json.loads((self.dir / "meta.json").read_text())
        self.assertEqual(meta["cites"], {"items": 4, "cited": 3})
        self.assertEqual(meta["started_at"], 1000.0)

    def test_the_cites_reach_the_todos(self):
        summary = self.run_with(CITED_REPLY)
        todos = sync_todos(self.dir, summary.body)
        self.assertEqual(
            [(t.text, t.cites) for t in todos],
            [("Send the usage figures by Friday", [(21, 24)])],
        )
        self.assertEqual(
            json.loads((self.dir / "todos.json").read_text())[0]["cites"],
            [[21, 24]],
        )

    def test_no_temp_file_is_left_behind(self):
        self.run_with(CITED_REPLY)
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertFalse([n for n in names if n.endswith(".tmp")], names)

    def test_a_marker_the_model_wrote_itself_is_not_trusted(self):
        self.run_with(
            CITED_REPLY.replace("- Nothing else was settled.",
                                "- Nothing else was settled. <!--cite 1-2-->")
        )
        self.assertNotIn("<!--cite 1-2-->", self.written())

    def test_ids_outside_the_transcript_cite_nothing(self):
        self.run_with("# T\n\n## TL;DR\n- Said nowhere [L90-95]\n")
        self.assertNotIn("cite", self.written())
        self.assertNotIn("[L", self.written())

    def test_an_untagged_reply_is_asked_again_once(self):
        summary = self.run_with("# T\n\n## TL;DR\n- plain\n")
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(summary.coverage, citations.Coverage(1, 0))
        self.assertEqual(self.written(), "# T\n\n## TL;DR\n- plain\n")
        meta = json.loads((self.dir / "meta.json").read_text())
        self.assertEqual(meta["cites"], {"items": 1, "cited": 0})

    def test_the_second_ask_can_be_the_cited_one(self):
        summary = self.run_with("# T\n\n## TL;DR\n- plain\n", CITED_REPLY)
        self.assertEqual(len(self.fake.calls), 2)
        self.assertEqual(summary.title, "Vendor Pricing Call")
        self.assertIn("<!--cite", self.written())

    def test_a_cited_reply_is_not_asked_twice(self):
        self.run_with(CITED_REPLY)
        self.assertEqual(len(self.fake.calls), 1)

    def test_nothing_to_cite_is_not_asked_again(self):
        """A summary with no bullets cannot have citations."""
        self.run_with("# T\n\nThey agreed on Thursday.\n")
        self.assertEqual(len(self.fake.calls), 1)

    def test_uncited_beats_no_summary(self):
        """Untagged once, then only titles: the first reply is kept."""
        summary = self.run_with(
            "# T\n\n## TL;DR\n- plain\n", "# Only a title"
        )
        self.assertEqual(len(self.fake.calls), 3)
        self.assertEqual(summary.title, "T")
        self.assertIn("- plain", self.written())

    def test_title_only_three_times_still_raises_without_writing(self):
        with self.assertRaisesRegex(claude.ClaudeError, "only a title"):
            self.run_with("# Only a title")
        self.assertFalse((self.dir / "summary.md").exists())

    def test_the_start_is_kept_so_cites_survive_the_audio_going(self):
        """With no started_at and no audio the first line is time zero; that
        must be written down, or deleting the audio later moves every cite."""
        (self.dir / "meta.json").unlink()
        self.run_with(CITED_REPLY)
        meta = json.loads((self.dir / "meta.json").read_text())
        self.assertEqual(meta["started_at"], 1005.0)
        self.assertIn("[L0 00:00] them", self.fake.calls[0][0])


# 2001-09-09, long before any session in the tests.
OLD_NS = 1_000_000_000 * 10**9


REWORDED_REPLY = """\
# Vendor Pricing Call

## TL;DR
- They want the usage figures by Friday. [L2-3]

## Action items
**You**
- Send usage figures to Alice by Friday [L2-3]

## Open questions
- Whether the discount is retroactive [L4]
"""


class TestResummarize(unittest.TestCase):
    """Summarizing a meeting again must leave what the user decided alone:
    ticks, filed tasks, the project, the notes, the names, the start."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.dir = _session(self.root, "20260101-090000", SESSION_ROWS)
        (self.dir / "meta.json").write_text(
            json.dumps({"started_at": 1000.0}) + "\n"
        )
        self.projects = Path(tempfile.mkdtemp())
        for name in ("thesis", "lab"):
            (self.projects / name).mkdir()

    def summarize(self, *replies):
        fake = FakeModel(*replies)
        with mock.patch.object(claude, "run_structured", fake):
            return asyncio.run(
                summarize_session(self.dir, "m", None, self.projects)
            )

    def land(self, summary):
        """What the app does when a summary arrives, with asst up."""
        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "target_list", return_value=None), \
             mock.patch.object(asstlink, "add_todo",
                               return_value="AB12") as add:
            todos = sync_todos(self.dir, summary.body)
        self.filed = getattr(self, "filed", 0) + add.call_count
        return todos

    def meta(self) -> dict:
        return json.loads((self.dir / "meta.json").read_text())

    def test_a_ticked_and_filed_to_do_survives_a_reworded_summary(self):
        self.land(self.summarize(CITED_REPLY))
        key = load_todos(self.dir)[0].key
        set_done_local(self.dir, key, True)
        before = load_todos(self.dir)[0]
        self.assertEqual(before.uid, "AB12")

        todos = self.land(self.summarize(REWORDED_REPLY))

        row, = todos
        self.assertEqual(row.text, "Send usage figures to Alice by Friday")
        self.assertEqual(
            (row.done, row.done_at, row.uid, row.src, row.list_name),
            (True, before.done_at, "AB12", before.src, before.list_name),
        )
        self.assertEqual(self.filed, 1)  # asst heard of it once, ever
        self.assertEqual(load_todos(self.dir), todos)

    def test_a_project_set_by_hand_is_not_overwritten(self):
        save_project(self.dir, "thesis")
        summary = self.summarize(
            {"summary": CITED_REPLY, "project": "lab"}
        )
        self.assertEqual(self.meta()["project"], "thesis")
        self.assertEqual(summary.project, "thesis")

    def test_a_blank_answer_does_not_clear_a_project(self):
        save_project(self.dir, "thesis")
        self.summarize({"summary": CITED_REPLY, "project": None})
        self.assertEqual(self.meta()["project"], "thesis")

    def test_the_models_project_fills_a_blank(self):
        summary = self.summarize({"summary": CITED_REPLY, "project": "lab"})
        self.assertEqual(self.meta()["project"], "lab")
        self.assertEqual(summary.project, "lab")
        save_project(self.dir, None)
        self.summarize({"summary": CITED_REPLY, "project": "thesis"})
        self.assertEqual(self.meta()["project"], "thesis")

    def test_start_names_notes_and_other_meta_are_untouched(self):
        meta = self.meta() | {
            "app": "zoom", "manual": True, "mic_pending": False,
            "project": "thesis",
        }
        (self.dir / "meta.json").write_text(json.dumps(meta) + "\n")
        save_names(self.dir, {"speaker_0": "Bob"})
        (self.dir / "notes.md").write_text("ask about the discount\n")
        rows = [dict(r, who="speaker_0") for r in SESSION_ROWS]
        rows[0]["text"] = "I'm Alice, shall we start"
        (self.dir / "transcript.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows)
        )
        # An old mtime makes "not even rewritten" something to assert.
        mine = [self.dir / "speakers.json", self.dir / "notes.md"]
        for path in mine:
            os.utime(path, ns=(OLD_NS, OLD_NS))
        before = [path.read_bytes() for path in mine]

        self.summarize(CITED_REPLY)

        self.assertEqual([path.read_bytes() for path in mine], before)
        self.assertEqual([path.stat().st_mtime_ns for path in mine],
                         [OLD_NS, OLD_NS])
        self.assertEqual(load_names(self.dir), {"speaker_0": "Bob"})
        after = self.meta()
        for key in ("started_at", "app", "manual", "mic_pending", "project"):
            self.assertEqual(after[key], meta[key], key)

    def test_a_failed_summary_leaves_the_last_one_alone(self):
        self.land(self.summarize(CITED_REPLY))
        names = ("summary.md", "meta.json", "todos.json")
        before = {n: (self.dir / n).read_bytes() for n in names}
        with self.assertRaises(claude.ClaudeError):
            self.summarize("# Only a title")
        self.assertEqual(
            {n: (self.dir / n).read_bytes() for n in names}, before
        )
