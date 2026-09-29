"""One markdown page for a meeting: what was decided, what you owe, what was said.

Built from what is already on disk — the summary, the live to-dos, your notes,
the transcript — and never sent anywhere. The user takes the page out; this
module only writes the text.

Unlike what goes to Claude, this is not redacted. The transcript on disk is
other people's speech as heard, and a copy you asked for is that same record.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .notes import Todo, load_notes, load_todos
from .playback import display_offset, format_offset, session_start
from .speakers import label as speaker_label
from .speakers import load_names
from .summarize import (
    SUMMARY_FILENAME,
    duration_minutes,
    load_project,
    load_records,
    load_title,
)

_ACTION_HEADING_RE = re.compile(r"^##\s+action\s+items?\s*$", re.IGNORECASE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def meeting_markdown(session_dir: Path) -> str:
    """The whole meeting as one markdown document. Ends with a newline."""
    records = load_records(session_dir)
    title = load_title(session_dir) or "Untitled meeting"
    project = load_project(session_dir)
    minutes = duration_minutes(records)
    todos = load_todos(session_dir)
    notes = load_notes(session_dir).strip()
    summary_md = _read_summary(session_dir)

    parts = [f"# {title}", "", _meta_line(session_dir, minutes, project)]

    body = _summary_body(summary_md, drop_action_items=bool(todos))
    if body:
        parts += ["", body]

    action = _action_items(todos)
    if action:
        parts += ["", action]

    if notes:
        parts += ["", "## Notes", notes]

    transcript = _transcript(session_dir, records)
    if transcript:
        parts += ["", "## Transcript", transcript]

    return "\n".join(parts).rstrip() + "\n"


def default_filename(session_dir: Path) -> str:
    """`2026-09-28-vendor-pricing-call.md`, or the directory name when untitled."""
    date = _session_date(session_dir)
    title = load_title(session_dir)
    if not title:
        return f"{session_dir.name}.md"
    slug = _SLUG_RE.sub("-", title.lower()).strip("-")
    return f"{date}-{slug}.md" if slug else f"{date}.md"


def _read_summary(session_dir: Path) -> str:
    try:
        return (session_dir / SUMMARY_FILENAME).read_text(errors="replace")
    except OSError:
        return ""


def _session_datetime(session_dir: Path) -> datetime:
    started = session_start(session_dir)
    if started > 0:
        return datetime.fromtimestamp(started)  # noqa: DTZ006
    # `YYYYMMDD-HHMMSS`, which every session directory is named after.
    try:
        return datetime.strptime(session_dir.name[:15], "%Y%m%d-%H%M%S")  # noqa: DTZ007
    except ValueError:
        return datetime.fromtimestamp(session_dir.stat().st_mtime)  # noqa: DTZ006


def _session_date(session_dir: Path) -> str:
    return _session_datetime(session_dir).strftime("%Y-%m-%d")


def _meta_line(session_dir: Path, minutes: float, project: str | None) -> str:
    when = _session_datetime(session_dir)
    parts = [f"{when:%A}, {when:%b} {when.day}, {when.year}, {when:%H:%M}"]
    if minutes >= 1:
        parts.append(f"{minutes:.0f} min")
    if project:
        parts.append(project)
    return " · ".join(parts)


def _summary_body(md: str, drop_action_items: bool) -> str:
    """The summary without its title line, and without Action items when the
    to-do list already has them."""
    out: list[str] = []
    skipping = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("# "):
            continue  # the document already has its own title
        if line.startswith("## "):
            skipping = drop_action_items and bool(_ACTION_HEADING_RE.match(line))
            if skipping:
                continue
        if skipping:
            continue
        out.append(line)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def _action_items(todos: list[Todo]) -> str:
    if not todos:
        return ""
    groups: dict[str, list[Todo]] = {}
    for t in todos:
        groups.setdefault(t.owner, []).append(t)
    lines: list[str] = ["## Action items"]
    for owner, label in (("you", "You"), ("them", "Them"), ("unassigned", "Unassigned")):
        items = groups.get(owner)
        if not items:
            continue
        lines.append(f"**{label}**")
        for t in items:
            mark = "x" if t.done else " "
            lines.append(f"- [{mark}] {t.text}")
    return "\n".join(lines)


def _transcript(session_dir: Path, records: list[dict]) -> str:
    if not records:
        return ""
    names = load_names(session_dir)
    started_at = session_start(session_dir, records)
    lines = []
    for r in records:
        stamp = format_offset(display_offset(r, started_at))
        who = speaker_label(r, names)
        lines.append(f"[{stamp}] {who}: {r.get('text', '')}")
    return "\n".join(lines)
