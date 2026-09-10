"""Meeting detection: parsing pactl, and deciding what counts as a meeting."""
from __future__ import annotations

import unittest

from steno.detect import (
    SELF_CLIENT_NAME,
    parse_source_outputs,
    qualifying,
)

SAMPLE = f"""Source Output #216
\tDriver: PipeWire
\tClient: 215
\tCorked: no
\tProperties:
\t\tapplication.name = "Chromium"
\t\tapplication.process.id = "1234"
\t\tapplication.process.binary = "chromium"
Source Output #217
\tDriver: PipeWire
\tCorked: yes
\tProperties:
\t\tapplication.name = "Discord"
\t\tapplication.process.id = "5678"
\t\tapplication.process.binary = "Discord"
Source Output #218
\tCorked: no
\tProperties:
\t\tapplication.name = "{SELF_CLIENT_NAME}"
\t\tapplication.process.id = "9999"
\t\tapplication.process.binary = "pacat"
"""


class TestParse(unittest.TestCase):
    def test_reads_every_stream(self):
        streams = parse_source_outputs(SAMPLE)
        self.assertEqual([s.index for s in streams], [216, 217, 218])

    def test_reads_fields(self):
        first = parse_source_outputs(SAMPLE)[0]
        self.assertEqual(first.app_name, "Chromium")
        self.assertEqual(first.binary, "chromium")
        self.assertEqual(first.pid, 1234)
        self.assertFalse(first.corked)

    def test_corked_is_per_stream(self):
        streams = parse_source_outputs(SAMPLE)
        self.assertFalse(streams[0].corked)
        self.assertTrue(streams[1].corked)

    def test_empty_input(self):
        self.assertEqual(parse_source_outputs(""), [])

    def test_missing_fields_degrade(self):
        streams = parse_source_outputs("Source Output #5\n\tCorked: no\n")
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0].app_name, "")
        self.assertEqual(streams[0].pid, 0)

    def test_label_falls_back(self):
        streams = parse_source_outputs("Source Output #5\n\tCorked: no\n")
        self.assertEqual(streams[0].label, "#5")


class TestQualifying(unittest.TestCase):
    def setUp(self):
        self.streams = parse_source_outputs(SAMPLE)

    def test_allows_a_known_app(self):
        self.assertEqual([s.label for s in qualifying(self.streams)], ["Chromium"])

    def test_corked_never_counts(self):
        """A held-but-corked stream is an app that isn't actually listening."""
        labels = [s.label for s in qualifying(self.streams, allow=("discord",))]
        self.assertEqual(labels, [])

    def test_never_detects_itself(self):
        """Our own parec would otherwise latch the meeting on forever."""
        live = qualifying(self.streams, allow=None)
        self.assertNotIn(SELF_CLIENT_NAME, [s.app_name for s in live])

    def test_self_pids_excluded(self):
        live = qualifying(self.streams, allow=None, self_pids=frozenset({1234}))
        self.assertEqual([s.label for s in live], [])

    def test_deny_wins_over_allow(self):
        live = qualifying(self.streams, allow=("chromium",), deny=("chromium",))
        self.assertEqual(live, [])

    def test_allow_none_takes_anything_uncorked(self):
        labels = [s.label for s in qualifying(self.streams, allow=None)]
        self.assertEqual(labels, ["Chromium"])

    def test_unknown_app_is_ignored_by_default(self):
        text = (
            'Source Output #9\n\tCorked: no\n\tProperties:\n'
            '\t\tapplication.name = "SomeVoiceAssistant"\n'
            '\t\tapplication.process.binary = "assistantd"\n'
        )
        self.assertEqual(qualifying(parse_source_outputs(text)), [])


if __name__ == "__main__":
    unittest.main()
