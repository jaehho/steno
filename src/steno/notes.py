"""What the user wrote, and what they owe.

Two artifacts per session, kept apart on purpose:

`notes.md` is the user's own typing during the call — never rewritten by the
tool, because a note you wrote yourself is worth more than a paragraph a model
wrote about you, and finding it edited would end any trust in the thing.

`todos.json` is the action items, seeded from the summary and then owned by the
user: checking one off has to survive a re-summarize, so state is keyed by the
todo's text rather than its position in a list.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

NOTES_FILENAME = "notes.md"
TODOS_FILENAME = "todos.json"

# `- [ ] text` / `**You**` headers, as the summary writes them.
_BULLET_RE = re.compile(r"^\s*[-*]\s+(?:\[[ xX]\]\s*)?(.+?)\s*$")
_OWNER_RE = re.compile(r"^\s*\*\*(You|Them|Unassigned)\*\*\s*:?\s*$", re.IGNORECASE)
_ACTION_HEADING_RE = re.compile(r"^\s*#+\s*action\s+items?\s*$", re.IGNORECASE)
_HEADING_RE = re.compile(r"^\s*#+\s+")


@dataclass
class Todo:
    text: str
    owner: str = "unassigned"
    done: bool = False
    done_at: float | None = None

    @property
    def key(self) -> str:
        """Identity is the wording, normalized — so a re-summarize that rephrases
        nothing keeps your checkmarks, and one that rewrites a line loses only
        that line."""
        return " ".join(self.text.lower().split())


def notes_path(session_dir: Path) -> Path:
    return session_dir / NOTES_FILENAME


def todos_path(session_dir: Path) -> Path:
    return session_dir / TODOS_FILENAME


def load_notes(session_dir: Path) -> str:
    try:
        return notes_path(session_dir).read_text()
    except OSError:
        return ""


def save_notes(session_dir: Path, text: str) -> None:
    """Write the user's notes, removing the file when they empty it."""
    path = notes_path(session_dir)
    if not text.strip():
        if path.is_file():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n")


def has_notes(session_dir: Path) -> bool:
    return notes_path(session_dir).is_file()


def parse_action_items(summary_body: str) -> list[Todo]:
    """Pull the Action items section out of a summary into todos.

    Owner comes from the `**You**` / `**Them**` sub-headers the summary prompt
    asks for; bullets before any owner header are unassigned.
    """
    todos: list[Todo] = []
    in_section = False
    owner = "unassigned"
    for line in summary_body.splitlines():
        if _ACTION_HEADING_RE.match(line):
            in_section = True
            owner = "unassigned"
            continue
        if in_section and _HEADING_RE.match(line):
            break  # the next section ends the list
        if not in_section:
            continue
        m = _OWNER_RE.match(line)
        if m:
            owner = m.group(1).lower()
            continue
        m = _BULLET_RE.match(line)
        if m:
            text = m.group(1).strip()
            if text:
                todos.append(Todo(text=text, owner=owner))
    return todos


def load_todos(session_dir: Path) -> list[Todo]:
    try:
        raw = json.loads(todos_path(session_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for row in raw if isinstance(raw, list) else []:
        if isinstance(row, dict) and row.get("text"):
            out.append(Todo(
                text=str(row["text"]),
                owner=str(row.get("owner", "unassigned")),
                done=bool(row.get("done", False)),
                done_at=row.get("done_at"),
            ))
    return out


def save_todos(session_dir: Path, todos: list[Todo]) -> None:
    todos_path(session_dir).parent.mkdir(parents=True, exist_ok=True)
    todos_path(session_dir).write_text(
        json.dumps([asdict(t) for t in todos], indent=2) + "\n"
    )


def sync_todos(session_dir: Path, summary_body: str) -> list[Todo]:
    """Reconcile the summary's action items with what's already on disk.

    Checked-off state is preserved by text. Items the user has completed are
    kept even if a re-summarize drops them — deleting the record of finished
    work would be the one unrecoverable thing this file could do.
    """
    existing = {t.key: t for t in load_todos(session_dir)}
    merged: list[Todo] = []
    seen: set[str] = set()
    for fresh in parse_action_items(summary_body):
        prior = existing.get(fresh.key)
        if prior is not None:
            fresh.done = prior.done
            fresh.done_at = prior.done_at
        merged.append(fresh)
        seen.add(fresh.key)
    merged.extend(t for key, t in existing.items() if key not in seen and t.done)
    save_todos(session_dir, merged)
    return merged


def set_done(session_dir: Path, key: str, done: bool) -> list[Todo]:
    todos = load_todos(session_dir)
    for t in todos:
        if t.key == key:
            t.done = done
            t.done_at = time.time() if done else None
    save_todos(session_dir, todos)
    return todos


def ensure_todos(session_dir: Path) -> list[Todo]:
    """Todos for a session, deriving them from an existing summary if needed.

    Sessions summarized before todos existed have a `summary.md` and no
    `todos.json`; re-running the model to recover a list already sitting in that
    file would be absurd, so parse it instead.
    """
    if todos_path(session_dir).is_file():
        return load_todos(session_dir)
    summary = session_dir / "summary.md"
    if not summary.is_file():
        return []
    try:
        return sync_todos(session_dir, summary.read_text())
    except OSError:
        return []


def open_count(session_dir: Path) -> int:
    return sum(1 for t in ensure_todos(session_dir) if not t.done)
