"""What the window says, worked out without a widget in sight.

Kept apart from the widgets so the wording — list sections, row metadata, the
status card, how a summary is rendered — can be tested headlessly.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# Headings in the summary that the page already shows some other way: the title
# is the page title, the TL;DR needs no label on top of the summary itself, and
# action items are the to-do list beside it.
TITLE_PREFIX = "# "
UNLABELLED = {"tl;dr"}
SHOWN_AS_TODOS = {"action items"}


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


def row_meta(start: datetime, now: datetime, mins: float, project: str | None) -> list[str]:
    return [p for p in (when(start, now), minutes(mins), project or "") if p]


def page_subtitle(start: datetime, mins: float, project: str | None) -> str:
    parts = [f"{start:%A}, {start:%b} {start.day}, {start:%H:%M}", minutes(mins), project or ""]
    return " · ".join(p for p in parts if p)


def todo_count(n: int) -> str:
    return f"{n} to do" if n else ""


def clock(seconds: float) -> str:
    total = max(0, int(seconds))
    if total >= 3600:
        return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
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


def summary_body(md: str, has_todos: bool) -> str:
    """The summary as the page shows it: without what the page shows elsewhere.

    Action items are only dropped when the to-do list really has them; a
    summary whose items failed to parse keeps them, rather than losing them.
    """
    out: list[str] = []
    skipping = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith(TITLE_PREFIX):
            continue
        if line.startswith("## "):
            name = line[3:].strip().lower()
            skipping = has_todos and name in SHOWN_AS_TODOS
            if skipping or name in UNLABELLED:
                continue
        if skipping:
            continue
        out.append(line)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def markup(md: str) -> str:
    """Render the summary's small markdown subset as Pango markup.

    Deliberately not a markdown engine: the summary is written by a prompt we
    control, so headings, bullets and bold cover it, and anything unexpected
    should show as its own literal text rather than disappear.
    """
    out = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith(("### ", "## ")):
            out.append(f"<b>{escape(line.split(' ', 1)[1])}</b>")
        elif line.startswith("# "):
            out.append(f"<b>{escape(line[2:])}</b>")
        elif line.lstrip().startswith(("- ", "* ")):
            indent = " " * (len(line) - len(line.lstrip()))
            out.append(f"{indent}•  {inline(line.lstrip()[2:])}")
        else:
            out.append(inline(line))
    return "\n".join(out)


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def inline(text: str) -> str:
    """Bold and code spans, after escaping — order matters or the markup breaks."""
    out = escape(text)
    for token, tag in (("**", "b"), ("`", "tt")):
        parts = out.split(token)
        if len(parts) >= 3:
            rebuilt = parts[0]
            for i, part in enumerate(parts[1:], start=1):
                rebuilt += (f"<{tag}>{part}</{tag}>" if i % 2 else part)
            out = rebuilt
    return out
