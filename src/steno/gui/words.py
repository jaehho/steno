"""What the window says, worked out without a widget in sight.

Kept apart from the widgets so the wording — list sections, row metadata, the
status card, how a summary is rendered — can be tested headlessly.
"""
from __future__ import annotations

import calendar
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple

from .. import citations, speakers
from ..notesjob import NotesPhase, NotesState
from ..playback import format_offset

if TYPE_CHECKING:
    from ..projectnotes import Result

SECONDS_PER_HOUR = 3600
SECONDS_PER_MINUTE = 60
MIN_WRAP_PARTS = 3  # token + two spans before wrapping kicks in
# Files a toast names before it says "…".
NAMED_AT_MOST = 2

# Headings in the summary that the page already shows some other way: the title
# is the page title, the TL;DR needs no label on top of the summary itself, and
# action items are the to-do list beside it.
TITLE_PREFIX = "# "
UNLABELLED = {"tl;dr"}
SHOWN_AS_TODOS = {"action items"}

# `**You**` alone on a line: whose action items the bullets below are.
OWNER_RE = re.compile(r"^\*\*[^*]+\*\*:?$")
NUMBERED_RE = re.compile(r"\d+[.)]")
# How much of a cited line the strip quotes before it says "…".
QUOTE_CHARS = 90
# A speaker tag's column: "Them" and "You" at the least, and a long name cut.
TAG_MIN_CHARS = 5
TAG_MAX_CHARS = 12
# The href of the "Summarize again" link in the note by the heading.
SUMMARIZE_AGAIN = "summarize"

# The words a date search is made of: each name whole, or one of the
# short forms people write, never any prefix ("marc" is a person).
_WEEKDAYS = {
    name: number for number, names in enumerate((
        ("monday", "mon"), ("tuesday", "tue", "tues"),
        ("wednesday", "wed", "weds"), ("thursday", "thu", "thur", "thurs"),
        ("friday", "fri"), ("saturday", "sat"), ("sunday", "sun"),
    )) for name in names
}
_MONTHS = {
    name: number for number, names in enumerate((
        ("january", "jan"), ("february", "feb"), ("march", "mar"),
        ("april", "apr"), ("may",), ("june", "jun"), ("july", "jul"),
        ("august", "aug"), ("september", "sep", "sept"),
        ("october", "oct"), ("november", "nov"), ("december", "dec"),
    ), start=1) for name in names
}
# "10/20", "10/20/26": month first, as dates are written here.
_SLASHED = re.compile(r"(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?")
_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DAY = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?")
_YEAR = re.compile(r"(?:19|20)\d{2}")
LAST_MONTH = 12
LAST_DAY = 31

# A search's words: anything between spaces.
_WORD = re.compile(r"\S+")
# What a `key:value` word of a search can be (issue #40). The rest of a
# search is one phrase.
FILTER_KEYS = frozenset({"project", "speaker", "after", "before", "has", "in"})
DAY_KEYS = frozenset({"after", "before"})
# Where a search looks, as a hit's row names it: `in:` picks from these.
SURFACES = (
    "title", "project", "speaker", "summary", "notes", "to-dos",
    "transcript", "date",
)
_SURFACE_ALIASES = {"todo": "to-dos", "todos": "to-dos"}
# What `has:` can ask for: open to-dos, as the row's "N to do" counts.
_TODO_WORDS = frozenset({"todo", "todos"})


def list_section(start: datetime, now: datetime) -> str:
    """Which heading a meeting sits under in the sidebar."""
    if start.date() == now.date():
        return "Today"
    if now - start < timedelta(days=7):
        return "Last 7 days"
    return "Earlier"


def when(start: datetime, now: datetime) -> str:
    """A meeting's date, as briefly as it can be said unambiguously."""
    if start.date() == now.date():
        return start.strftime("%H:%M")
    if now - start < timedelta(days=7):
        return start.strftime("%a %H:%M")
    if start.year == now.year:
        return f"{start:%b} {start.day}"
    return f"{start:%b} {start.day}, {start.year}"


def minutes(mins: float) -> str:
    return f"{mins:.0f} min" if mins >= 1 else ""


def row_meta(
    start: datetime, now: datetime, mins: float, project: str | None,
    summarized: bool = True,
) -> list[str]:
    """What the list row says under the title. A meeting with no summary says
    so: an italic "Untitled" alone reads as a name, not as something missing."""
    parts = [when(start, now), minutes(mins), project or ""]
    if not summarized:
        parts.append("no summary")
    return [p for p in parts if p]


def search_date(start: datetime) -> str:
    """The date a search can find a meeting by: "Sunday, September 27".

    No time and no year. "09:3" should not find a meeting by its hour, and
    with the year "20" would find them all.
    """
    return f"{start:%A}, {start:%B} {start.day}"


class DateQuery(NamedTuple):
    """A search read as a date. A part it does not name is None."""
    weekday: int | None  # Monday is 0
    month: int | None
    day: int | None
    year: int | None


def date_query(phrase: str) -> DateQuery | None:
    """A search as a date ("oct 20", "10/20", "weds", "tue oct 20th"), or
    None if any word of it is not part of one.

    A day needs a month or a weekday, and a year needs a month: "20" and
    "2026" alone would find every meeting.
    """
    found: dict[str, int] = {}
    for word in re.split(r"[\s,]+", phrase.lower().strip(" ,.")):
        parts = _date_word(word.rstrip("."))
        if parts is None or found.keys() & parts.keys():
            return None
        found.update(parts)
    if "day" in found and not found.keys() & {"month", "weekday"}:
        return None
    if "year" in found and "month" not in found:
        return None
    if not found:
        return None
    return DateQuery(found.get("weekday"), found.get("month"),
                     found.get("day"), found.get("year"))


def _date_word(word: str) -> dict[str, int] | None:
    """The parts of a date one word names, or None if it names none."""
    if word in _WEEKDAYS:
        return {"weekday": _WEEKDAYS[word]}
    if word in _MONTHS:
        return {"month": _MONTHS[word]}
    if _YEAR.fullmatch(word):
        return {"year": int(word)}
    return _numeric_date(word)


def _numeric_date(word: str) -> dict[str, int] | None:
    """A day ("20", "20th") or a date in figures ("10/20", "2026-10-20")."""
    if m := _DAY.fullmatch(word):
        return _calendar(None, int(m[1]), None)
    if m := _ISO.fullmatch(word):
        return _calendar(int(m[2]), int(m[3]), int(m[1]))
    if not (m := _SLASHED.fullmatch(word)):
        return None
    first, second = int(m[1]), int(m[2])
    # "20/10" can only be day first, so it is read that way.
    if first > LAST_MONTH >= second:
        first, second = second, first
    year = None if m[3] is None else int(m[3]) % 100 + 2000
    return _calendar(first, second, year)


def _calendar(
    month: int | None, day: int, year: int | None,
) -> dict[str, int] | None:
    """A month, day and year as date parts, or None if one cannot be."""
    if not 1 <= day <= LAST_DAY or (
        month is not None and not 1 <= month <= LAST_MONTH
    ):
        return None
    parts = {"day": day, "month": month, "year": year}
    return {k: v for k, v in parts.items() if v is not None}


def date_spans(
    query: DateQuery | None, start: datetime,
) -> list[tuple[int, int]]:
    """Where `query` lights `search_date(start)`, by character, or [] if it
    names another day. A month and its day are lit as one, "October 20".
    """
    if query is None:
        return []
    actual = (start.weekday(), start.month, start.day, start.year)
    if any(want is not None and want != got
           for want, got in zip(query, actual, strict=True)):
        return []
    weekday, month = f"{start:%A}", f"{start:%B}"
    month_at = len(weekday) + len(", ")
    day_at = month_at + len(month) + 1
    day_end = day_at + len(str(start.day))
    spans = []
    if query.weekday is not None:
        spans.append((0, len(weekday)))
    if query.month is not None:
        spans.append(
            (month_at, day_end if query.day is not None else day_at - 1))
    elif query.day is not None:
        spans.append((day_at, day_end))
    return spans


def page_subtitle(
    start: datetime, mins: float, project: str | None,
    archived: bool = False,
) -> str:
    parts = [
        f"{start:%A}, {start:%b} {start.day}, {start:%H:%M}",
        minutes(mins),
        project or "",
        "Archived" if archived else "",
    ]
    return " · ".join(p for p in parts if p)


def todo_count(n: int) -> str:
    return f"{n} to do" if n else ""


def reworded_warning(n: int) -> str:
    """What Summarize again does to `n` to-dos whose words are yours."""
    which = "1 to-do" if n == 1 else f"{n} to-dos"
    return (
        f"You reworded {which}, here or in asst. The new summary words "
        "every to-do as the model does, yours included, here and in asst."
    )


def owed_label(n: int) -> str:
    """The sidebar's cross-meeting count: what you still owe, everywhere."""
    return f"{n} to-do{'' if n == 1 else 's'} open"


def _open_tasks(n: int) -> str:
    return f"{n} open task{'s' if n != 1 else ''}"


# Unarchive never takes a task out of asst, so it does not try a refused
# removal again; archiving does. And the other way round for filing.
# A toast is one line: this is as short as it can be said.
RETRY_REMOVAL = "Unarchive and archive again to retry."
RETRY_FILING = "Archive and unarchive again to retry."


def archived_toast(title: str, removed: int, stuck: int = 0) -> str:
    """What archiving says it did. Its open to-dos are closed with it,
    which is the part that is not obvious; so is a task asst would not
    give up, which stays open there, and how to try again."""
    said = f"Archived “{title}”"
    if removed and stuck:
        return (
            f"{said}. Removed {_open_tasks(removed)} from asst; "
            f"could not remove {stuck}. {RETRY_REMOVAL}"
        )
    if removed:
        return f"{said}. Removed {_open_tasks(removed)} from asst."
    if stuck:
        return (
            f"{said}. Could not remove {_open_tasks(stuck)} from asst. "
            f"{RETRY_REMOVAL}"
        )
    return said


def put_away_notes(closed: bool, unclosed: bool, name: str = "") -> list[str]:
    """The lines over the to-dos of a meeting that is put away: what
    was closed with it, and what was not. A meeting put away before its
    to-dos were closed with it has the second kind, and so has one whose
    asst would not give a task up; neither says so anywhere else.
    Unarchive does not close them, so the second line says what does:
    archiving again, which `steno archive` does without the round trip
    (`name` is the meeting's name for that command)."""
    lines = []
    if closed:
        lines.append(
            "Closed when this meeting was put away. "
            "Unarchive it to open them again."
        )
    if unclosed:
        lines.append(
            "Some are still open: asst would not let go of a task, or "
            "this meeting was put away before its to-dos were closed "
            "with it. Unarchive it and archive it again to close them"
            + (f", or run steno archive {name} in a terminal." if name
               else ".")
        )
    return lines


def joined_toast(title: str, trashed: bool = True) -> str:
    """What joining two halves says it did. The later folder's fate is
    the part that is not obvious from the list: one row is gone because
    it is inside the earlier meeting now, not deleted by mistake."""
    said = f"Joined into “{title}”"
    return said + (". The later half is in the trash." if trashed else ".")


def unarchived_toast(title: str, filed: int, failed: int = 0) -> str:
    """What bringing a meeting back says it did to its to-dos, "" when
    there was nothing to say: no asst task came back with it."""
    said = f"Unarchived “{title}”"
    if filed and failed:
        return (
            f"{said}. Filed {_open_tasks(filed)} in asst again; "
            f"asst did not take {failed}. {RETRY_FILING}"
        )
    if filed:
        return f"{said}. Filed {_open_tasks(filed)} in asst again."
    if failed:
        return (
            f"{said}. asst did not take {_open_tasks(failed)}; "
            f"{'it is' if failed == 1 else 'they are'} open here only. "
            f"{RETRY_FILING}"
        )
    return ""


def project_notes_problem(result: Result) -> str:
    """What to tell the person when a meeting's project notes were not
    written all the way, "" when there is nothing to tell.

    A page that is not steno's to replace is left as it is and named;
    a write that failed says why. A meeting with no project, or notes
    switched off, is not a problem and says nothing.
    """
    if result.error:
        return f"Project notes: {result.error}"
    left = result.skipped
    if not left:
        return ""
    shown = left[:NAMED_AT_MOST]
    names = ", ".join(c.path.name for c in shown)
    if len(left) > len(shown):
        names += ", …"
    return f"Project notes: left {names} as it is ({left[0].note})"


def nothing_open(archived: int) -> tuple[str, str]:
    """(title, description) of the page with no meeting on it."""
    if archived:
        many = archived != 1
        return (
            "Nothing open",
            f"{archived} archived meeting{'s' if many else ''}. Open "
            f"Archived in the list to see {'them' if many else 'it'}.",
        )
    return (
        "No meetings yet",
        "Steno records by itself when an app uses the microphone. "
        "Start a call, or press Record.",
    )


def clock(seconds: float) -> str:
    total = max(0, int(seconds))
    if total >= SECONDS_PER_HOUR:
        return f"{total // SECONDS_PER_HOUR}:{(total % SECONDS_PER_HOUR) // SECONDS_PER_MINUTE:02d}:{total % 60:02d}"
    return f"{total // 60:02d}:{total % 60:02d}"


def status_card(
    status: str, detail: str, now: float, since: float, paused_until: float | None = None
) -> tuple[str, str, str]:
    """(title, subtitle, button) for the sidebar's status card.

    The subtitle says *why* the state is what it is, so that starting by itself
    is something the window explains rather than something the user has to know.
    An empty button label means no button.
    """
    if status == "recording":
        elapsed = clock(now - since)
        return "Recording", f"{elapsed} · {detail}" if detail else elapsed, "Stop"
    if status == "finalizing":
        return "Finishing up", detail or "Transcribing and summarizing", ""
    if status == "paused":
        left = max(0, int(((paused_until or now) - now) / 60))
        return "Paused", f"Listening again in {left} min", "Resume"
    return "Listening", "Records when an app uses the mic", "Record"


def notes_running_text(phase: NotesPhase | None) -> str:
    """What to say while a notes job is under way."""
    if phase is NotesPhase.TRANSCRIBING:
        return "Transcribing your side…"
    return "Writing the summary…"


def summary_missing(
    lines: int,
    *,
    state: NotesState | None = None,
    phase: NotesPhase | None = None,
    error: str = "",
    joined: bool = False,
) -> tuple[str, bool]:
    """(what the summary slot says, whether to offer "Summarize now").

    A meeting with no summary is a failed meeting, not a quiet one, so the
    page says which is true: work is under way (and whose), it failed and
    why, there was nothing to write it from, or it can be written now.
    A failure still offers the button — that is the retry.
    """
    if state is NotesState.RUNNING:
        text = notes_running_text(phase)
        return (f"Already {text[0].lower()}{text[1:]}" if joined else text), False
    if state is NotesState.FAILED:
        detail = error or "no reason given"
        return f"No summary: {detail}", True
    if lines <= 0:
        return "Nothing was transcribed in this meeting, so there is no summary.", False
    return "No summary yet.", True


def summary_note(
    seen: citations.Coverage,
    *,
    state: NotesState | None = None,
    phase: NotesPhase | None = None,
    error: str = "",
) -> str:
    """Pango markup for the quiet line beside the Summary heading.

    What is being done to the summary, why the last try failed, else (for
    one whose points cite nothing) how to fix that. Empty when there is
    nothing to say: a summary with nothing to cite is not nagged about.
    """
    if state is NotesState.RUNNING:
        return escape(notes_running_text(phase))
    if state is NotesState.FAILED:
        return escape(error or "summary failed")
    if seen.items and not seen.cited:
        return (
            f'No citations. <a href="{SUMMARIZE_AGAIN}" '
            'title="Write this summary again">Summarize again</a> '
            "to add them."
        )
    return ""


@dataclass
class Block:
    """One row of the summary as the page shows it.

    `kind` is `heading`, `owner` (a `**You**` line), `item` (a bullet, the
    only kind that can carry cites) or `text`. `markup` is Pango markup.
    """

    kind: str
    markup: str
    cites: list[citations.Cite] = field(default_factory=list)
    bullet: str = "•"  # an item's glyph: a dot, or its number
    indent: int = 0  # an item's leading spaces: a sub-bullet is indented


def _page_lines(md: str, has_todos: bool) -> Iterator[tuple[int, str]]:
    """(line number in `md`, the line without its marker) for each line
    the page shows: without what the page shows some other way.

    Action items are only dropped when the to-do list really has them; a
    summary whose items failed to parse keeps them, rather than losing
    them. Citation markers are not text: they are stripped before
    anything is shown.
    """
    skipping = False
    for number, raw in enumerate(md.splitlines()):
        line = citations.strip(raw).rstrip()
        if line.startswith(TITLE_PREFIX):
            continue
        if line.startswith("## "):
            name = line[3:].strip().lower()
            skipping = has_todos and name in SHOWN_AS_TODOS
            if skipping or name in UNLABELLED:
                continue
        if not skipping:
            yield number, line


def summary_blocks(md: str, has_todos: bool) -> list[Block]:
    """The summary as rows: headings, owners, items (with the cites they
    carry) and plain text. Deliberately not a markdown engine: the summary
    is written by a prompt we control, so headings, bullets and bold cover
    it, and anything unexpected shows as its own literal text rather than
    disappearing. Blank lines are spacing, which the page does itself.
    """
    found = {item.line_no: item for item in citations.items(md)}
    blocks = []
    for number, line in _page_lines(md, has_todos):
        stripped = line.lstrip()
        if (item := found.get(number)) is not None:
            numbered = NUMBERED_RE.match(stripped)
            blocks.append(Block(
                "item", inline(item.text), item.cites,
                numbered[0] if numbered else "•", len(line) - len(stripped),
            ))
        elif line.startswith(("### ", "## ")):
            title = escape(line.split(" ", 1)[1])
            blocks.append(Block("heading", f"<b>{title}</b>"))
        elif OWNER_RE.match(stripped):
            blocks.append(Block("owner", inline(stripped)))
        elif stripped:
            blocks.append(Block("text", inline(line)))
    return blocks


@dataclass
class Place:
    """Where a cite lands in the transcript, as the strip lists it."""

    rows: list[int]  # transcript records it covers, in order
    stamp: str  # when the first of them starts, as the gutter says
    speaker: str  # as the line's tag says it
    quote: str  # the start of the first of them
    more: int  # how many lines follow it inside the cite


def tag_chars(tags: Sequence[str]) -> int:
    """How wide a column of speaker tags is: its longest, so that the words
    beside them all start under each other, up to where a long name is cut."""
    longest = max((len(t) for t in tags), default=0)
    return min(max(longest, TAG_MIN_CHARS), TAG_MAX_CHARS)


def quote(text: str) -> str:
    """The start of a line, cut at a word."""
    text = " ".join(text.split())
    if len(text) <= QUOTE_CHARS:
        return text
    cut = text[:QUOTE_CHARS].rsplit(" ", 1)[0] or text[:QUOTE_CHARS]
    return cut.rstrip(" ,;:.-") + "…"


def cite_places(
    cites: Sequence[citations.Cite], records: Sequence[dict],
    offsets: Sequence[int], names: dict[str, str],
) -> list[Place]:
    """The places a list of cites points at, top of the transcript first.

    `offsets[i]` is record i's gutter second. A cite that no line falls in
    and that comes before the first line is no place; two cites that cover
    the same lines are one.
    """
    places: dict[tuple[int, ...], Place] = {}
    for cite in cites:
        rows = citations.resolve([cite], offsets)
        if not rows or tuple(rows) in places:
            continue
        first = records[rows[0]]
        places[tuple(rows)] = Place(
            rows,
            format_offset(offsets[rows[0]]),
            speakers.tag(first, names),
            quote(str(first.get("text", ""))),
            len(rows) - 1,
        )
    return sorted(places.values(), key=lambda place: place.rows[0])


class Filter(NamedTuple):
    """One `key:value` word of a search, which every hit must pass."""
    key: str  # one of FILTER_KEYS
    # As typed; for `in:` the surface as SURFACES spells it, and for
    # `has:` "todo".
    value: str
    negated: bool  # written `!key:value`
    span: tuple[int, int]  # where in the query, by character, `!` included


def parse_filters(query: str) -> tuple[list[Filter], str]:
    """The filters in a search, and the phrase the rest of it makes.

    A filter is one word, `key:value`, and `!` in front negates it. Its
    value runs to the next space. A word whose key is no filter's, or
    whose value its key cannot read (`after:soon`, `in:everything`),
    is a word of the phrase like any other.
    """
    filters: list[Filter] = []
    rest: list[str] = []
    for word in _WORD.finditer(query):
        found = _filter(word[0], word.span())
        if found is None:
            rest.append(word[0])
        else:
            filters.append(found)
    return filters, " ".join(rest)


def _filter(word: str, span: tuple[int, int]) -> Filter | None:
    """The filter a word of a search is, or None for a word to find."""
    negated = word.startswith("!")
    key, colon, typed = word.removeprefix("!").partition(":")
    key = key.lower()
    if not colon or key not in FILTER_KEYS:
        return None
    value = _filter_value(key, typed)
    return None if value is None else Filter(key, value, negated, span)


def _filter_value(key: str, typed: str) -> str | None:
    """A filter's value as it is kept, or None if `key` cannot read
    it."""
    if key in DAY_KEYS:
        return typed if _day_query(typed) is not None else None
    if key == "in":
        surface = _SURFACE_ALIASES.get(typed.lower(), typed.lower())
        return surface if surface in SURFACES else None
    if key == "has":
        return "todo" if typed.lower() in _TODO_WORDS else None
    return typed or None


def _day_query(typed: str) -> DateQuery | None:
    """`typed` as a date an `after:` or `before:` can stop at: one
    with a month in it and no weekday, read as a date search reads
    it."""
    query = date_query(typed)
    if query is None or query.month is None or query.weekday is not None:
        return None
    return query


def filter_day(day_filter: Filter, today: date) -> date:
    """The day an `after:` or `before:` filter names, which it includes.

    A month alone is all of it, as a day is: `after:oct` takes October
    from its first day and `before:oct` up to its last, so the two
    together are October. A date with no year is the one nearest
    `today`, except that `after:` never takes one in a month still to
    come, since nothing has happened after it yet. So in November
    `before:12/31` is this December and `after:11/1 before:11/30` is
    this month, and in January `after:12/1` is last December.
    """
    query = _day_query(day_filter.value)
    if day_filter.key not in DAY_KEYS or query is None or query.month is None:
        msg = f"not a date filter: {day_filter}"
        raise ValueError(msg)
    first = day_filter.key == "after"
    if query.year is not None:
        return _day_of(query.year, query.month, query.day, first=first)
    days = [
        _day_of(year, query.month, query.day, first=first)
        for year in (today.year - 1, today.year, today.year + 1)
    ]
    if first:
        days = [
            day for day in days
            if (day.year, day.month) <= (today.year, today.month)
        ]
    return min(days, key=lambda day: abs(day - today))


def _day_of(year: int, month: int, day: int | None, *, first: bool) -> date:
    """A day of a month, or with no day its first (`first`) or its
    last. A day past the month's end is its last."""
    last = calendar.monthrange(year, month)[1]
    if day is None:
        return date(year, month, 1 if first else last)
    return date(year, month, min(day, last))


def searched(filters: Sequence[Filter]) -> frozenset[str]:
    """Where a search looks for its phrase: the surfaces its `in:`
    filters name, or all of them, less those `!in:` leaves out."""
    named = {f.value for f in filters if f.key == "in" and not f.negated}
    left_out = {f.value for f in filters if f.key == "in" and f.negated}
    return frozenset(named or SURFACES) - left_out


def phrase_in(query: str, surface: str) -> str:
    """The phrase a search looks for in `surface`, "" where its `in:`
    filters leave that surface out."""
    filters, phrase = parse_filters(query)
    return phrase if surface in searched(filters) else ""


def search_phrase(query: str) -> str:
    """What a search looks for: the words as typed, in order, together,
    less its filters."""
    return parse_filters(query)[1]


def box_spans(query: str) -> list[tuple[int, int]]:
    """What the search box sets apart, by character: each filter, and
    every word of the phrase when the phrase reads as a date."""
    filters, phrase = parse_filters(query)
    taken = {f.span for f in filters}
    dated = date_query(phrase) is not None
    return [
        word.span() for word in _WORD.finditer(query)
        if dated or word.span() in taken
    ]


def match_spans(text: str, phrase: str) -> list[tuple[int, int]]:
    """Where `phrase` occurs in `text`, by character.

    Case is ignored, and so is how the words are spaced, since a note or a
    summary can break a line between them.
    """
    if not phrase:
        return []
    pattern = r"\s+".join(re.escape(word) for word in phrase.split())
    return [m.span() for m in re.finditer(pattern, text, re.IGNORECASE)]


def spans_in_parts(
    parts: Sequence[str], phrase: str, *, lit: Sequence[int], sep: str,
) -> list[tuple[int, int]]:
    """Where `phrase` occurs in the parts numbered `lit`, by character in
    `sep.join(parts)`. The rest of the line is not searched: a row lights
    what the meeting was found by, not every "min" in "45 min"."""
    spans: list[tuple[int, int]] = []
    at = 0
    for i, part in enumerate(parts):
        if i in lit:
            spans += [(at + a, at + b) for a, b in match_spans(part, phrase)]
        at += len(part) + len(sep)
    return spans


def byte_spans(text: str, spans: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Character spans as UTF-8 byte offsets, which is what Pango counts."""
    return [
        (len(text[:start].encode()), len(text[:end].encode()))
        for start, end in spans
    ]


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def inline(text: str) -> str:
    """Bold and code spans, after escaping — order matters or the markup breaks."""
    out = escape(text)
    for token, tag in (("**", "b"), ("`", "tt")):
        parts = out.split(token)
        if len(parts) >= MIN_WRAP_PARTS:
            rebuilt = parts[0]
            for i, part in enumerate(parts[1:], start=1):
                rebuilt += (f"<{tag}>{part}</{tag}>" if i % 2 else part)
            out = rebuilt
    return out
