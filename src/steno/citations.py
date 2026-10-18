"""Where in the meeting a summary line was said.

An item in `summary.md` may end with a hidden marker naming the seconds
of the meeting that support it:

    - Pays the deposit by Friday <!--cite 201-221,460-->

A cite is a closed range of whole seconds (`201-221`) or a single second
(`460`) on the transcript gutter's timeline (`playback.display_offset`).
Seconds, not line numbers: `steno realign` rewrites the transcript and
every line number with it, while the seconds stay roughly right.

The marker is an HTML comment, so GitHub and Obsidian hide it and the
summary stays the one place the citations live. That also makes it the
thing every reader of `summary.md` has to know about. Whatever shows or
parses the text goes through `strip` (or `to_refs`, for a copy that
leaves the app); otherwise the marker turns up in a to-do's wording and
changes its key.

The model never writes a marker. It ends a bullet with the ids of the
transcript lines that say it, `[L12-15, L40]`, and `from_model` turns
those into markers once it has checked them against the transcript.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from .playback import format_offset, index_at

# Inclusive whole seconds on the meeting timeline: what is stored.
Cite = tuple[int, int]
# Inclusive transcript record indices: what the model wrote.
Run = tuple[int, int]

MAX_CITES_PER_ITEM = 6
# A run of lines is a passage: speech is cut into short fragments, and on
# three real meetings 10% of the runs a model cited were wider than 40 s
# (8-21 lines, up to 72 s) and were exactly the passage to show. 90 s keeps
# those; a run past it is a sprawl, not a quote.
MAX_CITE_SECONDS = 90
# Seven digits, the most the marker grammar takes (115 days).
MAX_SECOND = 9_999_999

_MARK = "<!--cite"
# Digits are bounded so that int() is never handed a number it refuses.
_SPAN = r"\d{1,7}(?:-\d{1,7})?"
_SPANS = rf"{_SPAN}(?:,{_SPAN})*"
# One per line, at the end of it. The single space before it (`gap`)
# goes with it. Markers piled up at the end of a line (a hand edit, a
# model copying the format) are one marker to remove and to replace, so
# none is left to be read as a cite after `strip`; `spans` is the last.
_MARKER_RE = re.compile(
    rf"(?P<gap> ?)(?:<!--cite {_SPANS}-->[ \t]*)*"
    rf"<!--cite (?P<spans>{_SPANS})-->(?=[ \t]*\Z)"
)

# `- [ ] text`, `* text`, `1. text`, `2) text`: the shapes a model
# writes a list item in. A line this matches is an item; `**You**`
# owner headers and `##` headings are not.
BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.+?)\s*$")

# The model's tag is `[L12-15, L40]`. Copying the transcript's own
# `[L12 02:11]`, or writing `(lines 12-15)`, is no reason for it to
# reach summary.md, where it would become the wording of a to-do. So a
# group in brackets, or at the end of the line in parentheses, is a tag
# when nothing in it is but line ids: `L12`, `12`, `L12-15` with any
# dash, the words "line(s)" and "and", gutter stamps, emphasis and
# separators. A word of any other kind (`[L2 cache is slow]`) makes it
# text, and so does a link target after it (`[L2](url)`). Bare numbers,
# which any footnote could be, count only at the end of the line.
_GROUP_RE = re.compile(
    r"[ \t]?(?P<tick>`?)"
    r"(?:\[(?P<square>[^\[\]()`\n]*)\]|\((?P<round>[^\[\]()`\n]*)\))"
    r"(?P=tick)(?!\()"
)
_STAMP_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_LINE_WORD_RE = re.compile(r"\blines?\b", re.IGNORECASE)
_NOISE_RE = re.compile(r"[*_,;&\s]+|\band\b|\.(?=\s*$)", re.IGNORECASE)
_REF_RE = re.compile(
    r"(L?)\s?(\d+)(?:\s?[-\N{EN DASH}\N{EM DASH}]\s?L?\s?(\d+))?",
    re.IGNORECASE,
)
# What may follow a tag that is the last thing on its line.
_LINE_END_RE = re.compile(r"[\s.*_`]*\Z")
# Longer than this is no line of any transcript, and not for int().
_MAX_ID_DIGITS = 9


@dataclass
class Item:
    """A citable line of the summary."""

    line_no: int  # index into md.splitlines()
    text: str  # the words, without bullet, checkbox or marker
    cites: list[Cite]


class Coverage(NamedTuple):
    items: int = 0
    cited: int = 0


def _lines(md: str) -> list[tuple[str, str]]:
    """Each line as (text, its line ending); joined, they are `md`."""
    pairs = []
    for raw in md.splitlines(keepends=True):
        text = raw.splitlines()[0]
        pairs.append((text, raw[len(text):]))
    return pairs


def _spans(text: str) -> list[Cite]:
    """`201-221,460` as cites, in the order written."""
    cites = []
    for part in text.split(","):
        first, _, last = part.partition("-")
        low, high = int(first), int(last or first)
        cites.append((min(low, high), max(low, high)))
    return cites


def _marker(cites: Iterable[Cite]) -> str:
    """`<!--cite 201-221,460-->`, or "" when there are no cites."""
    spans = []
    for first, last in cites:
        if not 0 <= first <= last <= MAX_SECOND:
            msg = f"not a cite: {first}-{last}"
            raise ValueError(msg)
        spans.append(str(first) if first == last else f"{first}-{last}")
    return f"<!--cite {','.join(spans)}-->" if spans else ""


def strip(md: str) -> str:
    """`md` without its markers, and the space that led into each.

    Every other character, line endings included, is untouched.
    """
    if _MARK not in md:
        return md
    return "".join(_MARKER_RE.sub("", text) + end for text, end in _lines(md))


def parse(line: str) -> list[Cite]:
    """The cites at the end of one line, in the order written."""
    found = _MARKER_RE.search(line.rstrip("\r\n"))
    return _spans(found["spans"]) if found else []


def items(md: str) -> list[Item]:
    """The citable lines of a summary, in order."""
    found = []
    for line_no, line in enumerate(md.splitlines()):
        bullet = BULLET_RE.match(_MARKER_RE.sub("", line))
        text = bullet.group(1).strip() if bullet else ""
        if text:
            found.append(Item(line_no, text, parse(line)))
    return found


def coverage(md: str) -> Coverage:
    """How many items there are and how many of them have a cite."""
    found = items(md)
    return Coverage(len(found), sum(1 for item in found if item.cites))


def apply(md: str, cites: Mapping[int, Iterable[Cite]]) -> str:
    """Put each named item's cites at the end of its line.

    Keys are positions in `items(md)`. An item's marker is replaced if it
    has one, added if it has not, and removed when its cites are empty;
    items not named keep what they have. Nothing else in `md` changes,
    so `strip(apply(md, c)) == strip(md)`, and applying twice is
    applying once.
    """
    found = items(md)
    for index in cites:
        if not 0 <= index < len(found):
            msg = f"no item {index}; the summary has {len(found)}"
            raise ValueError(msg)
    by_line = {found[i].line_no: list(c) for i, c in cites.items()}
    return "".join(
        (_with_marker(text, by_line[n]) if n in by_line else text) + end
        for n, (text, end) in enumerate(_lines(md))
    )


def _with_marker(line: str, cites: list[Cite]) -> str:
    new = _marker(cites)
    old = _MARKER_RE.search(line)
    if old is None:
        return f"{line} {new}" if new else line
    kept = old["gap"] + new if new else ""
    return line[:old.start()] + kept + line[old.end():]


def resolve(cites: Iterable[Cite], offsets: Sequence[int]) -> list[int]:
    """The record indices the cites point at, sorted and unique.

    `offsets[i]` is record i's `display_offset`. A cite covers every
    record that starts inside it. One that covers none (a single second
    in the middle of a long utterance, or a range a realign has since
    emptied) falls back to the record being spoken at its first second.
    Offsets are scanned whole: a transcript can be out of order.
    """
    hit: set[int] = set()
    for first, last in cites:
        inside = {i for i, t in enumerate(offsets) if first <= t <= last}
        if not inside and (i := index_at(list(offsets), first)) >= 0:
            inside = {i}
        hit |= inside
    return sorted(hit)


def validate(raw_lines: Iterable[Run], offsets: Sequence[int]) -> list[Run]:
    """Line ids as the model wrote them, made safe to store.

    Ids outside the transcript are dropped (a range is cut to the part
    inside it); touching and overlapping runs merge; a run wider than
    `MAX_CITE_SECONDS` becomes its first and last line; an item keeps at
    most `MAX_CITES_PER_ITEM` runs. Offsets are by record index.
    """
    last = len(offsets) - 1
    clipped = sorted(
        (max(0, min(a, b)), min(last, max(a, b))) for a, b in raw_lines
    )
    merged: list[list[int]] = []
    for first, final in clipped:
        if first > final:
            continue
        if merged and first <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], final)
        else:
            merged.append([first, final])
    kept: list[Run] = []
    for first, final in merged:
        span = offsets[first:final + 1]
        if max(span) - min(span) > MAX_CITE_SECONDS:
            kept += [(first, first), (final, final)]
        else:
            kept.append((first, final))
    return kept[:MAX_CITES_PER_ITEM]


def _in_seconds(runs: Iterable[Run], offsets: Sequence[int]) -> list[Cite]:
    """Runs of record indices as cites: sorted, without repeats.

    A run the marker cannot hold (a record with a wild timestamp) is no
    cite: one bad row must not cost the summary it came with.
    """
    cites = set()
    for first, final in runs:
        span = offsets[first:final + 1]
        low, high = min(span), max(span)
        if 0 <= low <= high <= MAX_SECOND:
            cites.add((low, high))
    return sorted(cites)


def _tag_runs(inside: str, parens: bool, rest: str) -> list[Run] | None:
    """The runs a bracketed group names, or None when it is no tag.

    `rest` is the line after the group, to tell a number that ends it
    from one in the middle of a sentence.
    """
    plain = _NOISE_RE.sub(
        " ", _LINE_WORD_RE.sub("L", _STAMP_RE.sub(" ", inside))
    )
    refs = list(_REF_RE.finditer(plain))
    if not refs or _REF_RE.sub("", plain).strip():
        return None
    # `[12]` and `(L12)` could be anything but a tag; at the end of a
    # line they are not.
    lettered = any(m[1] for m in refs)
    if parens and not lettered:
        return None
    if (parens or not lettered) and not _LINE_END_RE.match(rest):
        return None
    runs = []
    for m in refs:
        first, last = m[2], m[3] or m[2]
        if max(len(first), len(last)) <= _MAX_ID_DIGITS:
            runs.append((int(first), int(last)))
    return runs


def _untag(text: str) -> tuple[str, list[Run] | None]:
    """A line without its tags, and the runs they named: None when it
    had none."""
    found: list[Run] | None = None

    def take(m: re.Match[str]) -> str:
        nonlocal found
        inside = m["square"] if m["square"] is not None else m["round"]
        runs = _tag_runs(inside, m["round"] is not None, text[m.end():])
        if runs is None:
            return m[0]
        found = (found or []) + runs
        return ""

    return _GROUP_RE.sub(take, text).rstrip(), found


def _take_tags(md: str) -> tuple[str, dict[int, list[Run]]]:
    """`md` without the model's tags, and the runs each line carried."""
    kept = []
    tagged: dict[int, list[Run]] = {}
    for line_no, (text, end) in enumerate(_lines(md)):
        bare, runs = _untag(text)
        if runs is None:
            kept.append(text + end)
        else:
            tagged[line_no] = runs
            kept.append(bare + end)
    return "".join(kept), tagged


def from_model(md: str, offsets: Sequence[int]) -> str:
    """A reply tagged `[L12-15, L40]` as a summary carrying markers.

    The tags are validated against the transcript and turned into
    seconds with `offsets` (record i's `display_offset`), then they
    leave the text: they never reach `summary.md`. A tag on a line that
    is not an item is dropped, as is any marker the model wrote itself,
    which is not something to trust. An item with no usable tag is just
    not clickable.
    """
    text, tagged = _take_tags(strip(md))
    cites: dict[int, list[Cite]] = {}
    for index, item in enumerate(items(text)):
        runs = validate(tagged.get(item.line_no, []), offsets)
        if runs:
            cites[index] = _in_seconds(runs, offsets)
    return apply(text, cites)


def refs(cites: Iterable[Cite]) -> str:
    """`[03:21, 07:40]`: where each cite starts, as the transcript
    section of an export stamps its lines. "" when there are none."""
    starts = []
    for first, _last in cites:
        stamp = format_offset(first)
        if stamp not in starts:
            starts.append(stamp)
    return f"[{', '.join(starts)}]" if starts else ""


def to_refs(md: str) -> str:
    """`md` with each marker written out as a visible `[mm:ss]` ref."""
    if _MARK not in md:
        return md
    return "".join(_with_refs(text) + end for text, end in _lines(md))


def _with_refs(line: str) -> str:
    found = _MARKER_RE.search(line)
    if found is None:
        return line
    before = line[:found.start()]
    ref = refs(_spans(found["spans"]))
    return before + (" " if before else "") + ref + line[found.end():]


def coerce(raw: object) -> list[Cite]:
    """Cites read back from JSON; anything but a pair of seconds is
    dropped, so a damaged file costs a cite and never a crash."""
    if not isinstance(raw, list):
        return []
    cites = []
    for pair in raw:
        try:
            first, last = pair
        except (TypeError, ValueError):
            continue
        if (
            type(first) is int and type(last) is int
            and 0 <= first <= last <= MAX_SECOND
        ):
            cites.append((first, last))
    return cites
