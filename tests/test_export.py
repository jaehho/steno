"""What a meeting looks like when it leaves the app as markdown."""
from __future__ import annotations

import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from steno import claude
from steno.export import default_filename, meeting_markdown
from steno.summarize import summarize_session
from tests.test_summarize import CITED_REPLY, SESSION_ROWS, FakeModel

SUMMARY = """# Vendor Pricing Call

## TL;DR
- They will send revised numbers.

## Action items
**You**
- Send the usage figures by Friday

**Them**
- Revised quote next week

## Open questions
- Whether the discount applies retroactively
"""


def _session(root: Path, name: str = "20260928-143000", **files: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    for filename, text in files.items():
        (d / filename.replace("_", ".")).write_text(text)
    return d


def _rows(*rows: dict) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


ROWS = [
    {"ts": 1000.0, "speaker": "you", "text": "morning", "offset": 0.0},
    {"ts": 1075.5, "speaker": "them", "text": "shall we start", "offset": 75.5, "who": "speaker_0"},
]


class TestMeetingMarkdown(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_full_page(self):
        d = _session(
            self.root,
            transcript_jsonl=_rows(*ROWS),
            summary_md=SUMMARY,
            meta_json=json.dumps({
                "started_at": 1000.0,
                "title": "Vendor Pricing Call",
                "project": "steno",
            }),
            notes_md="ask about the discount\n",
            todos_json=json.dumps([
                {"text": "Send the usage figures by Friday", "owner": "you", "done": True},
                {"text": "Draft the comparison table", "owner": "you", "done": False},
                {"text": "Revised quote next week", "owner": "them", "done": False},
            ]),
            speakers_json=json.dumps({"speaker_0": "Alice"}),
        )
        md = meeting_markdown(d)
        self.assertTrue(md.startswith("# Vendor Pricing Call\n"))
        self.assertIn("steno", md)
        self.assertIn("## TL;DR", md)
        self.assertIn("They will send revised numbers.", md)
        self.assertIn("## Action items", md)
        self.assertIn("- [x] Send the usage figures by Friday", md)
        self.assertIn("- [ ] Draft the comparison table", md)
        self.assertIn("**Them**", md)
        self.assertIn("## Notes", md)
        self.assertIn("ask about the discount", md)
        self.assertIn("## Transcript", md)
        self.assertIn("[00:00] you: morning", md)
        self.assertIn("[01:15] Alice: shall we start", md)
        self.assertTrue(md.endswith("\n"))

    def test_todos_replace_the_summary_action_items(self):
        d = _session(
            self.root,
            transcript_jsonl=_rows(*ROWS),
            summary_md=SUMMARY,
            meta_json=json.dumps({"started_at": 1000.0, "title": "Vendor Pricing Call"}),
            todos_json=json.dumps([
                {"text": "Send the usage figures by Friday", "owner": "you", "done": True},
            ]),
        )
        md = meeting_markdown(d)
        self.assertNotIn("- Send the usage figures by Friday", md)
        self.assertNotIn("- Revised quote next week", md)
        self.assertIn("- [x] Send the usage figures by Friday", md)

    def test_citations_are_written_out_as_stamps(self):
        """A copy leaves the app: the marker is a comment only steno reads."""
        marked = SUMMARY.replace(
            "revised numbers.", "revised numbers. <!--cite 201-221,460-->"
        )
        d = _session(
            self.root,
            transcript_jsonl=_rows(*ROWS),
            summary_md=marked,
            meta_json=json.dumps({"started_at": 1000.0, "title": "Pricing"}),
            todos_json=json.dumps([
                {"text": "Send the usage figures by Friday", "owner": "you",
                 "cites": [[75, 80]]},
                {"text": "Draft the comparison table", "owner": "you"},
            ]),
        )
        md = meeting_markdown(d)
        self.assertNotIn("cite", md)
        self.assertIn("- They will send revised numbers. [03:21, 07:40]", md)
        self.assertIn("- [ ] Send the usage figures by Friday [01:15]", md)
        self.assertIn("- [ ] Draft the comparison table\n", md)

    def test_summary_action_items_kept_when_there_are_no_todos(self):
        d = _session(
            self.root,
            transcript_jsonl=_rows(*ROWS),
            summary_md=SUMMARY,
            meta_json=json.dumps({"started_at": 1000.0, "title": "Vendor Pricing Call"}),
        )
        md = meeting_markdown(d)
        self.assertIn("- Send the usage figures by Friday", md)
        self.assertIn("- Revised quote next week", md)
        self.assertNotIn("- [ ]", md)

    def test_omits_empty_sections(self):
        d = _session(
            self.root,
            transcript_jsonl=_rows(*ROWS),
            meta_json=json.dumps({"started_at": 1000.0, "title": "Quiet call"}),
        )
        md = meeting_markdown(d)
        self.assertNotIn("## Notes", md)
        self.assertNotIn("## Action items", md)
        self.assertNotIn("## TL;DR", md)
        self.assertIn("## Transcript", md)

    def test_omits_transcript_when_there_are_no_records(self):
        d = _session(
            self.root,
            meta_json=json.dumps({"started_at": 1000.0, "title": "Silent"}),
            notes_md="nothing was said\n",
        )
        md = meeting_markdown(d)
        self.assertNotIn("## Transcript", md)
        self.assertIn("## Notes", md)

    def test_unsummarized_still_exports(self):
        d = _session(
            self.root,
            transcript_jsonl=_rows(*ROWS),
            notes_md="typed live\n",
        )
        md = meeting_markdown(d)
        self.assertTrue(md.startswith("# Untitled meeting\n"))
        self.assertIn("## Notes", md)
        self.assertIn("typed live", md)
        self.assertIn("## Transcript", md)

    def test_pii_and_garble_pass_through(self):
        """Export is the user's own record. Redaction is only for the model."""
        d = _session(
            self.root,
            transcript_jsonl=_rows({
                "ts": 1000.0,
                "speaker": "them",
                "text": "call me on 555-0102, the thruput is… wrong",
            }),
            meta_json=json.dumps({"started_at": 1000.0, "title": "Garbled"}),
        )
        md = meeting_markdown(d)
        self.assertIn("555-0102", md)
        self.assertIn("thruput is… wrong", md)


class TestRefsFindTheirLines(unittest.TestCase):
    """A ref in the summary is a stamp in the transcript section of the
    same page, or it points at nothing. They come from different code:
    the cites from what the model was shown, the stamps from the export.
    """

    REF_RE = re.compile(r"\[(\d{2}:\d{2}(?:, \d{2}:\d{2})*)\]")
    STAMP_RE = re.compile(r"^\[(\d+:\d{2}(?::\d{2})?)\] ", re.MULTILINE)

    def export_of(self, rows: list[dict]) -> str:
        root = Path(tempfile.mkdtemp())
        folder = _session(
            root, transcript_jsonl=_rows(*rows),
            meta_json=json.dumps({"started_at": 1000.0}),
        )
        with mock.patch.object(claude, "run_structured", FakeModel(CITED_REPLY)):
            asyncio.run(summarize_session(folder, "m", None, root / "none"))
        return meeting_markdown(folder)

    def check(self, rows: list[dict]) -> None:
        summary, _, transcript = self.export_of(rows).partition("## Transcript")
        refs = [
            stamp
            for found in self.REF_RE.findall(summary)
            for stamp in found.split(", ")
        ]
        stamps = self.STAMP_RE.findall(transcript)
        self.assertGreaterEqual(len(refs), 3)
        self.assertEqual([r for r in refs if r not in stamps], [])

    def test_when_the_first_line_is_said_some_seconds_in(self):
        self.check(SESSION_ROWS)

    def test_when_the_transcriber_reported_where_each_line_starts(self):
        self.check([dict(r, offset=r["ts"] - 1000.0 - 1.7) for r in SESSION_ROWS])


class TestDefaultFilename(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_slug_from_title(self):
        d = _session(
            self.root,
            meta_json=json.dumps({
                "started_at": 1000.0,
                "title": "Vendor Pricing: Call #2!",
            }),
        )
        name = default_filename(d)
        self.assertRegex(name, r"^\d{4}-\d{2}-\d{2}-vendor-pricing-call-2\.md$")

    def test_falls_back_to_the_directory_name(self):
        d = _session(self.root, transcript_jsonl=_rows(*ROWS))
        self.assertEqual(default_filename(d), "20260928-143000.md")

    def test_title_that_slugs_to_nothing(self):
        d = _session(
            self.root,
            meta_json=json.dumps({"started_at": 1000.0, "title": "???"}),
        )
        self.assertRegex(default_filename(d), r"^\d{4}-\d{2}-\d{2}\.md$")
