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
# A room's voices are everyone on the one microphone (`room`), named
# `mic_0`, `mic_1`, ... apart from the far side's `speaker_<n>`, so a name
# learned for one never lands on the other.
MIC_VOICE = "mic"
# What the live stream names a far-side voice; a file pass uses it too.
FAR_VOICE = "speaker"
# The name a voice is given when it turns out to be the user's own: the
# window then tags it "You", as it tags the microphone on a call.
USER = "You"

# "my name is Alice", "I'm Alice", "this is Alice" - the moments a voice
# announces itself. Deliberately narrow: a false name on a transcript is worse
# than a numbered voice, and the summary will say "Monday will send the deck".
# The lead-in is matched without regard to case ("I'm" is how ASR writes it);
# the name itself must be capitalized. "this is" only opens an utterance
# ("This is Alice from Acme"), since mid-sentence it is "this is Python".
_NAME = r"([A-Z][a-zA-Z'\u2019-]{1,24})\b"
# "I'm I'm gonna": a stutter. The second "I'm" (or "I'll", "I've") is
# nobody's name, and an introduction may still follow it.
_SELF = r"I['\u2019]"
_INTRODUCED = re.compile(
    r"\b(?i:my name is|i['\u2019]m|i am)\s+(?!" + _SELF + ")" + _NAME
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
    # A file learned before the stutter rule can hold "I'm": it is left
    # out, so the voice is "Them" again and the next summary relearns it.
    return {str(k): str(v) for k, v in raw.items()
            if v and not re.match(_SELF, str(v))}


def save_names(session_dir: Path, names: dict[str, str]) -> None:
    if not names:
        return
    names_path(session_dir).parent.mkdir(parents=True, exist_ok=True)
    names_path(session_dir).write_text(
        json.dumps(names, indent=2, sort_keys=True) + "\n"
    )


def name_of(record: dict, names: dict[str, str]) -> str:
    """The person a line's voice turned out to be, or "" if nobody knows."""
    return names.get(str(record.get("who") or ""), "")


def room_voice(who: str) -> str:
    """`voice 2` for the room voice `mic_1`; "" for any other voice.

    Counted from 1, as people count, and never "you": in a room the
    microphone is not one person.
    """
    prefix, _, number = who.partition("_")
    if prefix != MIC_VOICE or not number.isdigit():
        return ""
    return f"voice {int(number) + 1}"


def label(record: dict, names: dict[str, str]) -> str:
    """What a line should call its speaker: a name if we have one, else
    the room voice, else `them` or `you`."""
    return (
        name_of(record, names)
        or room_voice(str(record.get("who") or ""))
        or str(record.get("speaker", "them"))
    )


def tag(record: dict, names: dict[str, str]) -> str:
    """`label` as the window prints it: the name, else "Voice 2", else
    "Them" or "You"."""
    if name := name_of(record, names):
        return name
    if voice := room_voice(str(record.get("who") or "")):
        return voice.capitalize()
    return "Them" if record.get("speaker", "them") == "them" else "You"


def as_user(name: str, user_name: str) -> str:
    """`USER` when `name` is the user's own, else `name` unchanged."""
    if user_name and name.casefold() == user_name.casefold():
        return USER
    return name


def add_names(session_dir: Path, found: dict[str, str]) -> dict[str, str]:
    """Merge `found` into the session's name map and return the map.

    Only ever adds: a voice already named keeps its name, whoever gave
    it, so a later summary cannot rename a voice the user corrected.
    """
    names = load_names(session_dir)
    fresh = {who: name for who, name in found.items() if who not in names}
    if fresh:
        names.update(fresh)
        save_names(session_dir, names)
    return names


def _introduced(text: str, *, opening: bool = True) -> str | None:
    """The name in "I'm Alice" / "my name is Alice", or, with `opening`,
    in an opening "This is Alice"."""
    for pattern in (_INTRODUCED, _OPENING) if opening else (_INTRODUCED,):
        m = pattern.search(text)
        if m and m.group(1) not in _NOT_NAMES:
            return m.group(1)
    return None


def _self_named(text: str, *, in_room: bool) -> str | None:
    """The name a speaker gives as their own, or None.

    On a call "This is Alice" and "Alice here" are how a voice on the
    line says who it is. In a room they are how one person introduces,
    or points at, another ("This is Sam, who joins us"), so there only
    "I'm Alice" or "my name is Alice" counts.
    """
    if in_room:
        return _introduced(text, opening=False)
    return _introduced(text) or _called(text)


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


def learn_from(
    session_dir: Path, records: list[dict], user_name: str = ""
) -> dict[str, str]:
    """Fill the name map from introductions in the transcript.

    Only ever adds; a name already decided is not overwritten by a later
    sentence that happens to start with a capital. A voice that
    introduces itself by `user_name` is the user's (`USER`), as when the
    user says who they are to a room. A room voice (`mic_<n>`) is named
    only by a first-person introduction (`_self_named`).
    """
    found: dict[str, str] = {}
    for row in records:
        who = str(row.get("who") or "")
        text = str(row.get("text") or "")
        if not who or who in found or not text:
            continue
        name = _self_named(text, in_room=bool(room_voice(who)))
        if name:
            found[who] = as_user(name, user_name)
    # A file with nothing new in it is left as the user, or an earlier
    # summary, wrote it: summarizing again does not touch it.
    return add_names(session_dir, found)
