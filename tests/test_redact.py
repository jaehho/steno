"""PII must not ride along to the model."""
from __future__ import annotations

import unittest

from steno.redact import redact_records, redact_text


class TestRedactText(unittest.TestCase):
    def test_email(self):
        self.assertEqual(
            redact_text("write to jane.doe+steno@example.co.uk now"),
            "write to [EMAIL] now",
        )

    def test_card(self):
        self.assertEqual(
            redact_text("the card is 4111 1111 1111 1111 thanks"),
            "the card is [CARD] thanks",
        )

    def test_ssn(self):
        self.assertEqual(redact_text("ssn 123-45-6789"), "ssn [SSN]")

    def test_phone(self):
        self.assertEqual(
            redact_text("call (415) 555-0100 or 415-555-0100"),
            "call [PHONE] or [PHONE]",
        )

    def test_address(self):
        self.assertEqual(
            redact_text("I live at 742 Evergreen Terrace."),
            "I live at [ADDRESS].",
        )

    def test_ip(self):
        self.assertEqual(redact_text("the host is 192.168.1.10."), "the host is [IP].")

    def test_names_survive(self):
        """A summary that cannot say who owes what has failed at its job."""
        text = "Jane Doe will send the report to Bob Smith."
        self.assertEqual(redact_text(text), text)

    def test_plain_speech_untouched(self):
        text = "we should ship the firmware on friday"
        self.assertEqual(redact_text(text), text)

    def test_empty(self):
        self.assertEqual(redact_text(""), "")


class TestRedactRecords(unittest.TestCase):
    def test_only_text_changes(self):
        rows = [
            {"ts": 1.0, "speaker": "them", "text": "mail me at a@b.com"},
            {"ts": 2.0, "speaker": "you", "text": "on it"},
        ]
        out = redact_records(rows)
        self.assertEqual(out[0]["text"], "mail me at [EMAIL]")
        self.assertEqual(out[0]["ts"], 1.0)
        self.assertEqual(out[1]["text"], "on it")
        # original rows untouched
        self.assertEqual(rows[0]["text"], "mail me at a@b.com")
