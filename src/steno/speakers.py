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

# "this is alice", "i'm alice", "alice speaking" - the moments a voice
# announces itself. Deliberately narrow: a false name on a transcript is worse
# than a numbered voice.
_INTRODUCED = re.compile(
    r"\b(?:this is|i'm|i am|it's|it is|my name is)\s+([A-Z][a-zA-Z''-]{1,24})\b"
)
_CALLED = re.compile(
    r"\b([A-Z][a-zA-Z''-]{1,24})\s+(?:here|speaking)\b"
)


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
        for pattern in (_INTRODUCED, _CALLED):
            m = pattern.search(text)
            if m:
                names[who] = m.group(1)
                break
    save_names(session_dir, names)
    return names
