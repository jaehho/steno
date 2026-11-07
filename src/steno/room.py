"""Meetings held in a room: one microphone, several people.

steno was built for calls. There the far side comes through the speakers
and the microphone hears one person, the user, so every near-side line is
the user's by construction. In a room nothing comes through the speakers
and the microphone hears everyone. Left as a call, every voice is "you",
and a labmate's promise is filed as the user's task.

A room meeting is told apart by how it started (Record pressed with no
call app behind it) and by a far side that said nothing (`is_room`). Its
recording stays on the laptop until the user says everyone there agreed
(`awaits_agreement`). Its mic pass then asks Deepgram for voices
(`speakers.MIC_VOICE`), and one voice alone is a voice memo, not a room
(`several_voices`). Its summary names the voices it can from who was
expected to be there (`roster`, `room_prompt`, `names_from_reply`); and
every one of its action items waits for the user to say whose it is
(`notes.Todo.held`).
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from .speakers import NAMES_FILENAME, USER, as_user, room_voice

META_FILENAME = "meta.json"
# More names than this and the prompt is a directory, not a guest list.
MAX_ROSTER = 25
# A recording can start this long before the calendar slot, or run past
# it, and still be that meeting.
CALENDAR_SLACK = timedelta(minutes=15)
# An event with no end is taken to last this long.
DEFAULT_EVENT = timedelta(hours=1)
# Fewer voices than this on the microphone is the user alone: a voice
# memo, whose to-dos are theirs as on a call.
MIN_ROOM_VOICES = 2

ROOM_PROMPT = """\
# A room, not a call

This meeting was recorded in a room, on one microphone that heard \
everyone, so 'you' and 'them' above do not apply as written. Each line is \
labeled with a voice (`voice 1`, `voice 2`, ...) or with the name that \
voice was given, and `You` marks the user's own voice once it is known. \
Speech recognition tells voices apart by sound: one person can have two \
voices, but one voice is never two people.

{people}

In `voices`, name a voice only where the transcript shows who it is: they \
introduce themselves ("I'm Ana"), or someone addresses a person by name \
and that voice answers. In a room "This is Ana" or "Ana here" is nearly \
always someone introducing Ana, so it names the voice that answers, never \
the voice that said it. Use only the names listed above, spelled as \
listed, and the user's name for the user's voice. Leave out every voice \
you cannot name that way: a guess is worse than a number.

In the summary, call each voice by the name you gave it, else by its \
label. Under Action items, `**You**` holds only what the user's own voice \
committed to; if no line shows which voice is the user, put nothing \
under it. Every other commitment goes under `**Them**`, and each of those \
bullets starts with whose it is: `- Ana: send the package [L4]`, or \
`- voice 2: ...`. The user will say which items are theirs, so a \
commitment you cannot place still goes in, under `**Them**`."""

VOICES_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "voice": {"type": "string"},
            "name": {"type": "string"},
        },
        "required": ["voice", "name"],
        "additionalProperties": False,
    },
}


def is_room(meta: dict, records: list[dict]) -> bool:
    """True when a meeting was held in a room rather than on a call.

    `room` in meta.json decides when it is set, by hand or by an earlier
    pass. Otherwise a recording started by hand whose far side said
    nothing is a room: no call app opened the microphone, and nobody
    spoke through the speakers.
    """
    flag = meta.get("room")
    if isinstance(flag, bool):
        return flag
    far_side_spoke = any(r.get("speaker") == "them" for r in records)
    return bool(meta.get("manual")) and not far_side_spoke


def awaits_agreement(meta: dict, records: list[dict]) -> bool:
    """True while a room recording is kept on the laptop for the user's
    word that everyone in it agreed to be recorded and transcribed.

    A room's microphone was never sent anywhere until the mic pass, and
    `mic_pending` stays set until that pass has run, so the two together
    mean nothing of the room has left the machine yet.
    """
    return bool(meta.get("mic_pending")) and is_room(meta, records)


def several_voices(
    rows: list[dict], *, at_least: int = MIN_ROOM_VOICES
) -> bool:
    """Whether a room mic pass heard `at_least` distinct voices."""
    voices = {str(r.get("who") or "") for r in rows}
    return sum(1 for who in voices if room_voice(who)) >= at_least


def without_voices(rows: list[dict]) -> list[dict]:
    """`rows` with no room voice on them: one person's lines, the user's,
    which every surface then calls "you" as on a call."""
    return [
        {k: v for k, v in r.items() if not (k == "who" and room_voice(str(v)))}
        for r in rows
    ]


def recorded_in_room(session_dir: Path) -> bool:
    """Whether meta.json says this meeting was held in a room."""
    try:
        meta = json.loads((session_dir / META_FILENAME).read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(meta, dict) and meta.get("room") is True


def roster(
    groups: Iterable[Iterable[str]], *, limit: int = MAX_ROSTER
) -> list[str]:
    """The names in `groups`, first spelling kept, each once, at most
    `limit`. Earlier groups come first: put the surest source first."""
    seen: set[str] = set()
    people: list[str] = []
    for group in groups:
        for raw in group:
            name = " ".join(raw.split())
            if not name or name.casefold() in seen or name == USER:
                continue
            seen.add(name.casefold())
            people.append(name)
    return people[:limit]


def room_prompt(people: list[str], user_name: str) -> str:
    """The system prompt's room section, naming who may be there."""
    lines = []
    if people:
        lines.append("People who may be in the room: " + ", ".join(people) + ".")
    else:
        lines.append("Nobody said in advance who would be in the room.")
    if user_name:
        lines.append(f"The user is {user_name}.")
    else:
        lines.append("The user's name was not given: name no voice as theirs.")
    return ROOM_PROMPT.format(people=" ".join(lines))


def room_schema(base: dict) -> dict:
    """`base` with the `voices` list the room prompt asks for."""
    schema = json.loads(json.dumps(base))
    schema["properties"]["voices"] = VOICES_SCHEMA
    schema["required"] = [*schema["required"], "voices"]
    return schema


def names_from_reply(
    reply: object,
    *,
    heard: Iterable[str],
    people: list[str],
    user_name: str,
) -> dict[str, str]:
    """The voice names in a model's `voices` list that can be trusted.

    Kept: a voice the transcript has (`heard`, as `mic_<n>`), named with
    a name on `people` (in its listed spelling) or the user's own (which
    becomes `USER`). Anything else, a voice it made up or a name nobody
    expected, is dropped: a wrong name on a line is worse than a number.
    """
    by_label = {room_voice(who): who for who in heard}
    allowed = {name.casefold(): name for name in people}
    if user_name:
        allowed[user_name.casefold()] = user_name
    names: dict[str, str] = {}
    for item in reply if isinstance(reply, list) else []:
        if not isinstance(item, dict):
            continue
        who = by_label.get(" ".join(str(item.get("voice", "")).lower().split()))
        name = allowed.get(str(item.get("name", "")).strip().casefold())
        if who and name and who not in names:
            names[who] = as_user(name, user_name)
    return names


def room_voices(records: list[dict]) -> list[str]:
    """The room voices the transcript has, `mic_0` first."""
    heard = {str(r.get("who") or "") for r in records}
    return sorted(
        (who for who in heard if room_voice(who)),
        key=lambda who: int(who.partition("_")[2]),
    )


# --------------------------------------------------------------- the shell


def earlier_names(
    sessions: Iterable[Path], *, project: str | None, leave_out: Path
) -> list[str]:
    """Names learned in other meetings of `project`; none when it is not
    known. The people met on its calls are the people met in its room.
    Never another project's: a name from one would be sent to the model
    with the other's meeting, and could be given to a voice there."""
    if not project:
        return []
    names: list[str] = []
    for session_dir in sessions:
        if session_dir == leave_out or _project(session_dir) != project:
            continue
        try:
            raw = json.loads((session_dir / NAMES_FILENAME).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict):
            names.extend(str(v) for v in raw.values() if v and v != USER)
    return names


def _project(session_dir: Path) -> str | None:
    try:
        meta = json.loads((session_dir / META_FILENAME).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return meta.get("project") if isinstance(meta, dict) else None


def calendar_attendees(
    calendar_dirs: Iterable[Path],
    at: datetime,
    *,
    slack: timedelta = CALENDAR_SLACK,
) -> list[str]:
    """Who the calendar expected at a meeting that began at `at`.

    Reads `.ics` files under each of `calendar_dirs` (vdirsyncer's copies)
    for timed events whose slot holds `at`, a weekly meeting's later
    occurrences included, and returns their attendees' and organizer's
    names (CN). Declined people, rooms and resources, and a CN that is
    only an address are left out. Empty when icalendar (the system's
    python-icalendar, as for gi) is not importable.
    """
    try:
        import icalendar  # noqa: PLC0415 - optional, like numpy for clean-echo
    except ImportError:
        return []
    names: list[str] = []
    for root in calendar_dirs:
        for path in sorted(Path(root).rglob("*.ics")):
            try:
                calendar = icalendar.Calendar.from_ical(path.read_bytes())
            except (OSError, ValueError):
                continue
            for event in calendar.walk("VEVENT"):
                if _holds(event, at, slack):
                    names.extend(_people(event))
    return names


def _holds(event, at: datetime, slack: timedelta) -> bool:
    """Whether `event`, or one of its occurrences, is on at `at`."""
    from dateutil.rrule import rrulestr  # noqa: PLC0415 - icalendar's own dependency

    start_prop = event.get("DTSTART")
    if start_prop is None or not isinstance(start_prop.dt, datetime):
        return False  # all-day: not a meeting slot
    start = _aware(start_prop.dt)
    end_prop = event.get("DTEND")
    if end_prop is not None and isinstance(end_prop.dt, datetime):
        length = _aware(end_prop.dt) - start
    elif event.get("DURATION") is not None:
        length = event["DURATION"].dt
    else:
        length = DEFAULT_EVENT
    if "RRULE" in event:
        try:
            rule = rrulestr(event["RRULE"].to_ical().decode(), dtstart=start)
            start = rule.before(at + slack, inc=True)
        except (ValueError, TypeError):
            return False
        if start is None:
            return False
    return start - slack <= at <= start + length


def _aware(moment: datetime) -> datetime:
    """A floating time is local time."""
    return moment if moment.tzinfo is not None else moment.astimezone()


def _people(event) -> list[str]:
    """The named individuals an event invites, organizer included."""
    attendees = event.get("ATTENDEE", [])
    if not isinstance(attendees, list):
        attendees = [attendees]
    organizer = event.get("ORGANIZER")
    names = []
    for person in [*attendees, *([organizer] if organizer is not None else [])]:
        params = getattr(person, "params", {})
        if str(params.get("CUTYPE", "INDIVIDUAL")).upper() != "INDIVIDUAL":
            continue
        if str(params.get("PARTSTAT", "")).upper() == "DECLINED":
            continue
        name = str(params.get("CN", "")).strip().strip('"')
        if name and "@" not in name:
            names.append(name)
    return names
