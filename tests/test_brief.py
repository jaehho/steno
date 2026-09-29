"""The brief as standing context, and the names it hands to Deepgram."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from steno.brief import brief_for, format_status, proper_nouns
from steno.transcribe import MAX_KEYTERMS, listen_url


class TestProperNouns(unittest.TestCase):
    def test_finds_names_and_acronyms(self):
        text = "Met Jane Doe at Lakeview Research Lab. Deepgram Nova-3 is the model."
        nouns = proper_nouns(text)
        self.assertIn("Jane Doe", nouns)
        self.assertIn("Lakeview Research Lab", nouns)
        self.assertIn("Deepgram Nova-3", nouns)

    def test_backticked_terms_win(self):
        nouns = proper_nouns("Remember `CR-39` and the `dispersion formula`.")
        self.assertIn("CR-39", nouns)
        self.assertIn("dispersion formula", nouns)

    def test_acronyms_survive_inside_a_sentence(self):
        self.assertIn("ERL", proper_nouns("The ERL module owns that."))

    def test_glue_words_are_dropped(self):
        nouns = proper_nouns("The API and the project both matter.")
        self.assertNotIn("The API", nouns)
        self.assertNotIn("The", nouns)
        self.assertIn("API", nouns)

    def test_a_heading_does_not_swallow_the_next_line(self):
        nouns = proper_nouns("# Brief\nI work at ACME.")
        self.assertNotIn("Brief\nI", nouns)
        self.assertNotIn("I", nouns)

    def test_sentence_openers_are_not_names(self):
        nouns = proper_nouns("Use the tool. Call me later. Meeting notes go here.")
        for word in ("Use", "Call", "Meeting"):
            self.assertNotIn(word, nouns)

    def test_repeats_are_listed_once(self):
        nouns = proper_nouns("ACME builds it. ACME ships it.")
        self.assertEqual(nouns.count("ACME"), 1)

    def test_respects_the_limit(self):
        text = "\n".join(f"Name{i} works here." for i in range(60))
        self.assertEqual(len(proper_nouns(text, limit=5)), 5)

    def test_empty_input(self):
        self.assertEqual(proper_nouns(""), [])


class TestStatus(unittest.TestCase):
    def test_no_brief(self):
        self.assertEqual(format_status(None, None), "no brief")

    def test_with_age(self):
        self.assertIn("2d old", format_status("x" * 40, 2))


class TestListenUrl(unittest.TestCase):
    """Issue 6: the brief's proper nouns become Deepgram keyterms."""

    def test_keyterms_are_repeatable_params(self):


        url = listen_url(["Deepgram Nova-3", "ACME"])
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q["keyterm"], ["Deepgram Nova-3", "ACME"])
        self.assertEqual(q["model"], ["nova-3"])

    def test_no_keyterms_is_a_plain_url(self):


        self.assertNotIn("keyterm", urlparse(listen_url()).query)
        self.assertNotIn("keyterm", urlparse(listen_url(None)).query)

    def test_blanks_are_skipped(self):


        q = parse_qs(urlparse(listen_url(["", "  ", "ACME"])).query)
        self.assertEqual(q["keyterm"], ["ACME"])

    def test_cap_on_terms(self):


        many = [f"Name{i}" for i in range(MAX_KEYTERMS + 10)]
        q = parse_qs(urlparse(listen_url(many)).query)
        self.assertEqual(len(q["keyterm"]), MAX_KEYTERMS)


class TestBriefFor(unittest.TestCase):
    """Issue 3: an app can have its own brief, and anything else falls back."""

    def setUp(self):

        self.dir = Path(tempfile.mkdtemp())
        self.standing = self.dir / "brief.md"
        self.standing.write_text("standing context")

    def _named(self, slug: str, text: str = "app context") -> Path:
        p = self.dir / f"brief.{slug}.md"
        p.write_text(text)
        return p

    def test_named_brief_wins(self):
        self._named("zoom")
        self.assertEqual(brief_for(self.standing, "Zoom"), self.dir / "brief.zoom.md")

    def test_label_is_normalised(self):
        self._named("google-chrome")
        self.assertEqual(
            brief_for(self.standing, "Google Chrome"),
            self.dir / "brief.google-chrome.md",
        )

    def test_unknown_app_falls_back(self):
        self._named("zoom")
        self.assertEqual(brief_for(self.standing, "Discord"), self.standing)

    def test_no_app_is_the_standing_brief(self):
        self.assertEqual(brief_for(self.standing, ""), self.standing)

    def test_missing_file_falls_back_even_with_a_slug(self):
        self.assertEqual(brief_for(self.standing, "zoom"), self.standing)
