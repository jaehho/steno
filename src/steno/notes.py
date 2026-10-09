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

from . import asstlink

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
    # asst's short id, once the item has been filed there. Empty means it lives
    # only in this file — either asst is not installed, or the item is theirs.
    uid: str = ""
    # The `--source` this row was filed under. Frozen at first save so a
    # re-summarize that rephrases the text still finds the same phone task.
    src: str = ""
    # asst list the task sits in (`Inbox`, `thesis`, …). Empty when unfiled.
    list_name: str = ""

    @property
    def key(self) -> str:
        """Wording, normalized — so a re-summarize that rephrases nothing keeps
        your checkmarks, and one that rewrites a line loses only that line."""
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
    return [
        Todo(
            text=str(row["text"]),
            owner=str(row.get("owner", "unassigned")),
            done=bool(row.get("done", False)),
            done_at=row.get("done_at"),
            uid=str(row.get("uid", "") or ""),
            src=str(row.get("src", "") or ""),
            list_name=str(row.get("list_name", "") or ""),
        )
        for row in raw if isinstance(raw, list) and isinstance(row, dict) and row.get("text")
    ]


def save_todos(session_dir: Path, todos: list[Todo]) -> None:
    todos_path(session_dir).parent.mkdir(parents=True, exist_ok=True)
    todos_path(session_dir).write_text(
        json.dumps([asdict(t) for t in todos], indent=2) + "\n"
    )


def _with_src(session_dir: Path, todo: Todo) -> None:
    """Freeze the asst source on first save. A later rephrase gets a new one."""
    if not todo.src:
        todo.src = asstlink.source_key(session_dir, todo.key)


def sync_todos(session_dir: Path, summary_body: str) -> list[Todo]:
    """Reconcile the summary's action items with what's already on disk.

    Checked-off state is preserved by source, then by text. Items the user has
    completed are kept even if a re-summarize drops them — deleting the record
    of finished work would be the one unrecoverable thing this file could do.
    An open item already on the phone is kept too: the model must not orphan a
    task asst is holding. A rephrase becomes a second row (new source) rather
    than a silent rewrite of the phone task.
    """
    existing = load_todos(session_dir)
    by_src = {t.src: t for t in existing if t.src}
    by_key = {t.key: t for t in existing}
    merged: list[Todo] = []
    seen: set[int] = set()
    for fresh in parse_action_items(summary_body):
        _with_src(session_dir, fresh)
        prior = by_src.get(fresh.src) or by_key.get(fresh.key)
        if prior is not None and id(prior) not in seen:
            fresh.done = prior.done
            fresh.done_at = prior.done_at
            fresh.uid = prior.uid
            fresh.src = prior.src
            seen.add(id(prior))
        merged.append(fresh)
    merged.extend(
        t for t in existing
        if id(t) not in seen and (t.done or t.uid or t.owner == "you")
    )
    save_todos(session_dir, merged)
    file_with_asst(session_dir, merged)
    return merged


def set_done(session_dir: Path, key: str, done: bool) -> list[Todo]:
    todos = load_todos(session_dir)
    for t in todos:
        if t.key == key:
            t.done = done
            t.done_at = time.time() if done else None
            if t.uid:
                asstlink.set_done(t.uid, done)
    save_todos(session_dir, todos)
    return todos


def file_with_asst(session_dir: Path, todos: list[Todo]) -> list[Todo]:
    """Push your own unfiled action items to asst, keeping the returned ids.

    Only `owner == "you"` goes across: what someone else promised is not a
    task on the user's phone. Idempotent via `--source`, so a re-summarize
    files nothing twice and keeps the uid it already had. Destination is the
    meeting's project list when asst has one, else the inbox.
    """
    if not asstlink.available():
        return todos
    pending = [t for t in todos if t.owner == "you" and not t.uid and not t.done]
    if not pending:
        return todos
    list_name = asstlink.target_list(session_dir)
    prefix = asstlink.source_prefix(session_dir)
    changed = False
    for t in pending:
        _with_src(session_dir, t)
        key = t.src[len(prefix):] if t.src.startswith(prefix) else t.key
        uid = asstlink.add_todo(session_dir, t.text, key, list_name)
        if uid:
            t.uid = uid
            t.list_name = list_name or "Inbox"
            changed = True
    if changed:
        save_todos(session_dir, todos)
    return todos


def pull_todos(session_dir: Path, todos: list[Todo] | None = None) -> list[Todo]:
    """Adopt asst's checked state for this meeting. Read-only toward asst.

    Remote wins when asst answers: a phone check-off has to show up here. A
    task deleted in asst is deleted here too — a row that has reached the
    phone and then vanished from it was removed on purpose, and keeping it
    would only re-file it on the next re-summarize. Rows that never filed
    (theirs, or yours not yet sent) are local and stay.
    """
    todos = load_todos(session_dir) if todos is None else todos
    if not asstlink.available():
        return todos
    remote = asstlink.fetch_session(session_dir)
    if remote is None:
        return todos
    by_src = remote
    prefix = asstlink.source_prefix(session_dir)
    by_key = {
        k[len(prefix):] if k.startswith(prefix) else k.rsplit("/", 1)[-1]: v
        for k, v in remote.items()
    }
    kept: list[Todo] = []
    changed = False
    for t in todos:
        _with_src(session_dir, t)
        hit = by_src.get(t.src) or by_key.get(t.key)
        if hit is None:
            if t.uid:
                changed = True
                continue  # asst deleted it; drop the row with it
            kept.append(t)
            continue
        done = bool(hit["completed"])
        uid = str(hit.get("id") or t.uid)
        list_name = str(hit.get("list") or t.list_name)
        if t.done != done or t.uid != uid or t.list_name != list_name:
            t.done = done
            t.done_at = time.time() if done else None
            t.uid = uid
            t.list_name = list_name
            changed = True
        kept.append(t)
    if changed:
        save_todos(session_dir, kept)
    return kept


def move_with_asst(session_dir: Path, todos: list[Todo], list_name: str | None) -> int:
    """Follow a project change into asst. Returns how many tasks moved."""
    if not asstlink.available():
        return 0
    uids = [t.uid for t in todos if t.owner == "you" and t.uid]
    if not uids:
        return 0
    moved = asstlink.move_todos(uids, list_name)
    if moved:
        for t in todos:
            if t.uid:
                t.list_name = list_name or "Inbox"
        save_todos(session_dir, todos)
    return moved


def forget_asst(session_dir: Path, todos: list[Todo] | None = None) -> int:
    """The meeting is going away: take its asst tasks with it.

    uids are cleared in `todos.json` before the folder is trashed, so a later
    restore from the trash looks like unfiled local rows rather than rows whose
    phone tasks were deleted behind the user's back.
    """
    todos = load_todos(session_dir) if todos is None else todos
    uids = [t.uid for t in todos if t.uid]
    removed = asstlink.rm_todos(uids) if (uids and asstlink.available()) else 0
    changed = False
    for t in todos:
        if t.uid:
            t.uid = ""
            t.list_name = ""
            changed = True
    if changed:
        save_todos(session_dir, todos)
    return removed


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
    """Open items that are yours.

    What someone else promised is not a to-do on this machine; counting it
    made the sidebar badge shout about other people's work.
    """
    return sum(1 for t in ensure_todos(session_dir) if not t.done and t.owner != "them")
