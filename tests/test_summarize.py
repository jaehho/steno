"""Transcript handling and session resolution — everything up to the API call."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from steno.summarize import (
    duration_minutes,
    format_for_model,
    iter_sessions,
    load_records,
    load_title,
    parse_summary,
    resolve_session,
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
