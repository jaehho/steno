"""What the window says, worked out without a widget in sight.

Kept apart from the widgets so the wording — list sections, row metadata, the
status card, how a summary is rendered — can be tested headlessly.
"""
from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .. import citations
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
# The href of the "Summarize again" link in the note by the heading.
SUMMARIZE_AGAIN = "summarize"


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
    speaker: str  # "You" or "Them"
    quote: str  # the start of the first of them
    more: int  # how many lines follow it inside the cite


def quote(text: str) -> str:
    """The start of a line, cut at a word."""
    text = " ".join(text.split())
    if len(text) <= QUOTE_CHARS:
        return text
    cut = text[:QUOTE_CHARS].rsplit(" ", 1)[0] or text[:QUOTE_CHARS]
    return cut.rstrip(" ,;:.-") + "…"


def cite_places(
    cites: Sequence[citations.Cite], records: Sequence[dict],
    offsets: Sequence[int],
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
            "Them" if first.get("speaker", "them") == "them" else "You",
            quote(str(first.get("text", ""))),
            len(rows) - 1,
        )
    return sorted(places.values(), key=lambda place: place.rows[0])


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
