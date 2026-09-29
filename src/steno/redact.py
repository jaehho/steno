"""Scrub personally identifying details out of a transcript before it is sent
to a model.

The transcript is other people's speech. It stays on disk exactly as heard;
this runs only on the copy that leaves for a summary or an answer, so a
provider's logs never hold an email address or a card number that happened to
be read aloud. Placeholders keep the summary readable - "call [PERSON] at
[PHONE]" still yields the action item.

Names are deliberately *not* removed: the brief exists to resolve them, and a
summary that cannot say who owes what is the failure this tool is built against.
"""
from __future__ import annotations

import re

# Order matters: the card pattern is the loosest and would eat phone numbers.
_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # Email addresses.
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "[EMAIL]"),
    # Credit / debit cards: 13-19 digits, optional separators. Checked loosely
    # here; a number that merely looks like a card is cheaper to mask than to
    # leak.
    (re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), "[CARD]"),
    # US SSN.
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    # Phone numbers: (415) 555-0100, 415-555-0100, +1 415 555 0100. No leading
    # `\b` - the form with parentheses starts on a non-word character, and `\b`
    # only fires next to a word character.
    (re.compile(
        r"(?<![\w.])(?:\+?\d{1,2}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]?\d{3}[ .-]?\d{4}(?!\w)"
    ), "[PHONE]"),
    (re.compile(r"(?<!\w)\d{7,}(?!\w)"), "[NUMBER]"),
    # Street addresses: a house number followed by a capitalized street word.
    (re.compile(
        r"\b\d{1,5}\s+(?:[A-Z][\w.']+\s+){0,3}"
        r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|"
        r"Court|Ct|Way|Place|Pl|Terrace|Ter)\b(?![-\w])",
        re.IGNORECASE,
    ), "[ADDRESS]"),
    # IPv4.
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
)


def redact_text(text: str) -> str:
    """Replace PII spans with stable placeholders. The text keeps its shape."""
    for pattern, placeholder in _PATTERNS:
        text = pattern.sub(placeholder, text)
    return text


def redact_records(records: list[dict]) -> list[dict]:
    """Redact the `text` of each transcript row, leaving timestamps alone."""
    out = []
    for row in records:
        copied = dict(row)
        if isinstance(copied.get("text"), str):
            copied["text"] = redact_text(copied["text"])
        out.append(copied)
    return out
