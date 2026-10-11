"""Which voice is which person.

Deepgram's diarization numbers the far side `speaker_0`, `speaker_1`, ... and
those numbers mean nothing across a meeting, let alone across a week. This is
the small map that turns them into names the summary can use: `speaker_0` is
"Alice" because the brief lists her, or because someone said "this is Alice"
and the label appeared next to her voice.

Kept beside the session rather than in the brief: a name is a property of
*this* call's voices, and two people can swap chairs between Tuesday and
Thursday.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

NAMES_FILENAME = "speakers.json"

# "my name is Alice", "I'm Alice", "this is Alice" - the moments a voice
# announces itself. Deliberately narrow: a false name on a transcript is worse
# than a numbered voice, and the summary will say "Monday will send the deck".
# The lead-in is matched without regard to case ("I'm" is how ASR writes it);
# the name itself must be capitalized. "this is" only opens an utterance
# ("This is Alice from Acme"), since mid-sentence it is "this is Python".
_NAME = r"([A-Z][a-zA-Z'\u2019-]{1,24})\b"
_INTRODUCED = re.compile(
    r"\b(?i:my name is|i['\u2019]m|i am)\s+" + _NAME
)
_OPENING = re.compile(r"^\W*(?i:this is)\s+" + _NAME)
# Capitalized words that follow those lead-ins without being anyone's name.
_NOT_NAMES = frozenset({
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
    "Okay", "Ok", "Sorry", "Good", "Fine", "Great", "Sure", "Here", "Back",
    "Just", "Not", "Glad", "Happy", "Going", "Trying", "Looking", "Using",
    "Still", "Also", "Really", "Very", "Now", "Well", "Yes", "No", "Thanks",
    "Thank", "Afraid", "Done", "Ready", "Late", "Early", "Online", "Muted",
    "Alone", "Calling", "Wondering", "Thinking", "Saying",
})
# "alice here", "bob speaking" - but only mid-sentence; `_called` checks.
_CALLED = re.compile(
    r"\b([A-Z][a-zA-Z''-]{1,24})\s+(?:here|speaking)\b"
)
_SENTENCE_END = (".", "!", "?")


def names_path(session_dir: Path) -> Path:
    return session_dir / NAMES_FILENAME


def load_names(session_dir: Path) -> dict[str, str]:
    """`{"speaker_0": "Alice"}`. Missing file is simply an empty map."""
    try:
        raw = json.loads(names_path(session_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if v}


def save_names(session_dir: Path, names: dict[str, str]) -> None:
    if not names:
        return
    names_path(session_dir).parent.mkdir(parents=True, exist_ok=True)
    names_path(session_dir).write_text(
        json.dumps(names, indent=2, sort_keys=True) + "\n"
    )


def label(record: dict, names: dict[str, str]) -> str:
    """What a line should call its speaker: a name if we have one, else `them`."""
    who = str(record.get("who") or "")
    if who and who in names:
        return names[who]
    return str(record.get("speaker", "them"))


def _introduced(text: str) -> str | None:
    """The name in "I'm Alice" / "my name is Alice" / an opening "This is Alice"."""
    for pattern in (_INTRODUCED, _OPENING):
        m = pattern.search(text)
        if m and m.group(1) not in _NOT_NAMES:
            return m.group(1)
    return None


def _called(text: str) -> str | None:
    """The name in an "Alice here" / "Alice speaking" mention, or None.

    Mid-sentence only: ASR capitalizes the first word of an utterance and
    the word after sentence punctuation, so "So here again, ..." looks
    like an introduction but is just speech (issue 14). A false name is
    worse than a numbered voice.
    """
    for m in _CALLED.finditer(text):
        before = text[: m.start()].rstrip()
        if before and not before.endswith(_SENTENCE_END) and m.group(1) not in _NOT_NAMES:
            return m.group(1)
    return None


def learn_from(session_dir: Path, records: list[dict]) -> dict[str, str]:
    """Fill the name map from introductions in the transcript.

    Only ever adds; a name already decided is not overwritten by a later
    sentence that happens to start with a capital.
    """
    names = load_names(session_dir)
    for row in records:
        who = str(row.get("who") or "")
        text = str(row.get("text") or "")
        if not who or who in names or not text:
            continue
        name = _introduced(text) or _called(text)
        if name:
            names[who] = name
    save_names(session_dir, names)
    return names
