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
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import NamedTuple

from . import asstlink, citations

NOTES_FILENAME = "notes.md"
TODOS_FILENAME = "todos.json"

# Models vary: a bullet as `- x`, `* x` or `1. x` (`citations.BULLET_RE`),
# and an owner as `**You**`, `**You:**`, `**You**:` or a `### You` heading.
# Dropping any of these loses a promise.
_OWNER_RE = re.compile(
    r"^\s*(?:#{3,}\s+)?(?:\*\*)?(You|Them|Unassigned)\s*:?\s*(?:\*\*)?\s*:?\s*$",
    re.IGNORECASE,
)
_ACTION_HEADING_RE = re.compile(r"^\s*#+\s*action\s+items?\s*$", re.IGNORECASE)
_HEADING_RE = re.compile(r"^\s*#+\s+")
# A bullet that says there are no action items ("No commitments were
# made.") is a model padding a section it was told to leave out. It is
# not a promise, and would come up as an open to-do nobody owes.
_NONE_RE = re.compile(
    r"(?:none|n/a|nothing\s+(?:was|were|is|came|specific|concrete)"
    r"|no(?:\s+\w+){0,2}?\s+(?:commitments?|action\s+items?|tasks?"
    r"|follow-?\s?ups?|next\s+steps?|deadlines?|promises?))\b",
    re.IGNORECASE,
)

# A re-summarize can word one promise differently. An item that matches
# no row by source or text is still taken for an old row of the same
# owner when the two read this alike (difflib's ratio on the normalized
# text) and say the same thing: apart from small words (`_FILLER`) and a
# plural s they use the same words, or one adds only a name, day, month
# or number to the other. A word swapped for another ("first" for
# "final", "client" for "team") makes a different promise. Below all
# that a pair stays two promises: a duplicate row is a nuisance, but a
# new promise that comes up already ticked is a lost one.
SAME_ITEM_RATIO = 0.8
_WORD_RE = re.compile(r"[\w'\u2019-]+")
_CALENDAR = frozenset((
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
))
# Words a model adds, drops or trades when it rewords a promise, and
# that do not change which promise it is. Not "by", "before", "from" or
# "up": those do.
_FILLER = frozenset((
    "a", "an", "the", "to", "of", "for", "on", "in", "at", "with",
    "about", "regarding", "please", "also", "will", "would", "shall",
    "should", "can", "could", "must", "need", "needs",
))
# A word this long or longer loses a plural s ("was" and "bus" keep it).
_PLURAL_FROM = 4


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
    # A checkbox that did not reach asst (daemon down). Kept until it lands,
    # so a later pull cannot un-tick what the user just ticked.
    dirty: bool = False
    # Closed by putting the meeting away: not open, not counted, not
    # pulled, not filed, and it waits for Unarchive. Its text and src
    # stay, so a re-summarize still finds the row and Unarchive can file
    # it again. Its uid stays only until asst has been seen to drop the
    # task (`drop_open_todos`): a dropped row that names a task is one
    # whose removal is still to do.
    dropped: bool = False
    # Where in the meeting this was said, as seconds on the transcript's
    # timeline. Comes from the summary's markers, never from the text.
    cites: list[citations.Cite] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Wording, normalized — so a re-summarize that rephrases nothing keeps
        your checkmarks, and one that rewrites a line loses only that line."""
        return " ".join(self.text.lower().split())

    @property
    def open(self) -> bool:
        """Still to do: not ticked, and not closed with the meeting."""
        return not self.done and not self.dropped

    @property
    def unclosed(self) -> bool:
        """Open, or closed with its asst task not yet seen to go: what
        archiving the meeting again still has to close."""
        return self.open or (self.dropped and bool(self.uid))


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
    asks for; bullets before any owner header are unassigned. A citation
    marker is not part of the wording: it becomes `cites`, so the text, and
    the key and asst source made from it, are the same with or without one.
    """
    todos: list[Todo] = []
    in_section = False
    owner = "unassigned"
    for raw in summary_body.splitlines():
        line = citations.strip(raw)
        if _ACTION_HEADING_RE.match(line):
            in_section = True
            owner = "unassigned"
            continue
        if not in_section:
            continue
        m = _OWNER_RE.match(line)
        if m:
            owner = m.group(1).lower()
            continue
        if _HEADING_RE.match(line):
            break  # the next section ends the list
        m = citations.BULLET_RE.match(line)
        if m:
            text = m.group(1).strip()
            if text and not _NONE_RE.match(text):
                todos.append(
                    Todo(text=text, owner=owner, cites=citations.parse(raw))
                )
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
            dirty=bool(row.get("dirty", False)),
            dropped=bool(row.get("dropped", False)),
            cites=citations.coerce(row.get("cites")),
        )
        for row in raw if isinstance(raw, list) and isinstance(row, dict) and row.get("text")
    ]


def save_todos(session_dir: Path, todos: list[Todo]) -> None:
    """Replace todos.json whole. Atomic, so a reader never sees half of it."""
    path = todos_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps([asdict(t) for t in todos], indent=2) + "\n")
    tmp.replace(path)


# The window ticks boxes, a pull thread adopts what the phone says, and a
# summary lands, all against one file. Each of those reads, changes and writes
# it back, and a slow `asst` call sits between reading and writing in every
# one. So: slow calls are made with nothing held, and the write is a patch to
# the file as it is *then* (`_edit`), never the list as it was when the call
# began. Otherwise a tick made during a pull is overwritten by the older list.
_LOCK = threading.RLock()


def _edit(session_dir: Path, patch: Callable[[list[Todo]], bool]) -> list[Todo]:
    """Re-read todos.json, let `patch` change it, save when it says it did."""
    with _LOCK:
        todos = load_todos(session_dir)
        if patch(todos):
            save_todos(session_dir, todos)
        return todos


def _with_src(session_dir: Path, todo: Todo) -> None:
    """Freeze the asst source on first save. A later rephrase gets a new one."""
    if not todo.src:
        todo.src = asstlink.source_key(session_dir, todo.key)


def _singular(word: str) -> str:
    """Plural s off a lower-case word: "figures" is "figure"."""
    if (
        len(word) >= _PLURAL_FROM
        and word.endswith("s")
        and not word.endswith("ss")
    ):
        return word[:-1]
    return word


def _words(text: str) -> tuple[set[str], set[str]]:
    """The words of an item that make it the promise it is, and which of
    them are facts.

    Facts tell two promises apart that otherwise read the same: numbers,
    days and months, and names (a capitalized word that does not open the
    line). Small words are left out and a plural s is folded, so "Send
    the usage figures" and "Send usage figure" come out alike.
    """
    words: set[str] = set()
    facts: set[str] = set()
    for i, raw in enumerate(_WORD_RE.findall(text)):
        low = raw.lower()
        if low in _FILLER:
            continue
        word = _singular(low)
        words.add(word)
        if (
            any(c.isdigit() for c in raw)
            or low in _CALENDAR
            or (i > 0 and len(raw) > 1 and raw[0].isupper())
        ):
            facts.add(word)
    return words, facts


def _likeness(a: Todo, b: Todo) -> float:
    """How alike two action items read, 0.0 when they cannot be one."""
    if a.owner != b.owner:
        return 0.0
    words_a, facts_a = _words(a.text)
    words_b, facts_b = _words(b.text)
    only_a, only_b = words_a - words_b, words_b - words_a
    if not (words_a and words_b) or (only_a and only_b):
        return 0.0  # nothing to compare, or a word was swapped for another
    if not (only_a | only_b) <= (facts_a | facts_b):
        return 0.0  # one adds more than a name, day or number
    return SequenceMatcher(None, a.key, b.key).ratio()


def _reworded_pairs(
    fresh: Sequence[Todo], prior: Sequence[Todo]
) -> list[tuple[Todo, Todo]]:
    """Pair each new item with the old row it rewords, best match first.

    One old row goes to one new item at most: of two look-alikes, the
    closer one inherits and the other is new.
    """
    scored = sorted(
        (
            (-_likeness(f, p), i, j)
            for i, f in enumerate(fresh) for j, p in enumerate(prior)
        )
    )
    pairs: list[tuple[Todo, Todo]] = []
    used_fresh: set[int] = set()
    used_prior: set[int] = set()
    for negative, i, j in scored:
        if -negative < SAME_ITEM_RATIO:
            break
        if i in used_fresh or j in used_prior:
            continue
        used_fresh.add(i)
        used_prior.add(j)
        pairs.append((fresh[i], prior[j]))
    return pairs


def _carry_state(fresh: Todo, prior: Todo) -> None:
    """Hand the user's side of a row to the item that replaces it."""
    fresh.done = prior.done
    fresh.done_at = prior.done_at
    fresh.uid = prior.uid
    fresh.src = prior.src
    # A tick that has not reached asst yet must stay marked, or the
    # next pull sees the task open there and undoes it.
    fresh.dirty = prior.dirty
    fresh.dropped = prior.dropped
    fresh.list_name = prior.list_name


def sync_todos(session_dir: Path, summary_body: str, file: bool = True) -> list[Todo]:
    """Reconcile the summary's action items with what's already on disk.

    Checked-off state is preserved by source, then by text, then by likeness:
    an item the summary now words differently (`SAME_ITEM_RATIO`, and no
    word swapped for another) is the same row, with the new wording and
    the old state, uid and source, so the task asst holds is neither
    duplicated nor orphaned. Items the user has completed are kept even if
    a re-summarize drops them — deleting the record of finished work would
    be the one unrecoverable thing this file could do. An open item already
    on the phone is kept too: the model must not orphan a task asst is
    holding. An item that matches nothing becomes a new row (new source).

    `file=False` only writes the local list: listing meetings must not push
    anything into a task app.
    """
    with _LOCK:
        existing = load_todos(session_dir)
        by_src = {t.src: t for t in existing if t.src}
        by_key = {t.key: t for t in existing}
        fresh_items = parse_action_items(summary_body)
        matched: list[tuple[Todo, Todo]] = []
        loose: list[Todo] = []
        taken: set[int] = set()
        for fresh in fresh_items:
            _with_src(session_dir, fresh)
            prior = by_src.get(fresh.src) or by_key.get(fresh.key)
            if prior is not None and id(prior) not in taken:
                matched.append((fresh, prior))
                taken.add(id(prior))
            else:
                loose.append(fresh)
        # Only after every exact match is settled, so a look-alike cannot
        # take the row an item of the same wording is owed.
        matched += _reworded_pairs(
            loose, [t for t in existing if id(t) not in taken]
        )
        for fresh, prior in matched:
            _carry_state(fresh, prior)
            taken.add(id(prior))
        merged = fresh_items + [
            t for t in existing
            if id(t) not in taken and (t.done or t.uid or t.owner == "you")
        ]
        save_todos(session_dir, merged)
    return file_with_asst(session_dir, merged) if file else merged


def set_done_local(session_dir: Path, key: str, done: bool) -> list[Todo]:
    """Tick or untick in todos.json, fast. A row on the phone is marked dirty
    until `deliver_done` has told asst; a pull in between keeps your tick."""
    def patch(todos: list[Todo]) -> bool:
        changed = False
        for t in todos:
            if t.key == key:
                t.done = done
                t.done_at = time.time() if done else None
                if t.uid:
                    t.dirty = True
                changed = True
        return changed

    return _edit(session_dir, patch)


def deliver_done(session_dir: Path, key: str, done: bool) -> None:
    """Tell asst about a tick. Slow (a subprocess): not for the GTK thread."""
    owed = [t.uid for t in load_todos(session_dir) if t.key == key and t.uid and t.dirty]
    for uid in owed:
        if not asstlink.set_done(uid, done):
            continue

        def landed(todos: list[Todo], uid: str = uid) -> bool:
            changed = False
            for t in todos:
                if t.uid == uid and t.done == done and t.dirty:
                    t.dirty = False
                    changed = True
            return changed

        _edit(session_dir, landed)


def set_done(session_dir: Path, key: str, done: bool) -> list[Todo]:
    set_done_local(session_dir, key, done)
    deliver_done(session_dir, key, done)
    return load_todos(session_dir)


def file_with_asst(session_dir: Path, todos: list[Todo]) -> list[Todo]:
    """Push your own unfiled action items to asst, keeping the returned ids.

    Only `owner == "you"` goes across: what someone else promised is not a
    task on the user's phone. Idempotent via `--source`, so a re-summarize
    files nothing twice and keeps the uid it already had. Destination is the
    meeting's project list when asst has one, else the inbox. A meeting
    that is put away files nothing: its to-dos are closed.
    """
    if not asstlink.available() or asstlink.archived(session_dir):
        return todos
    pending = [t for t in todos if t.owner == "you" and not t.uid and t.open]
    if not pending:
        return todos
    list_name = asstlink.target_list(session_dir)
    prefix = asstlink.source_prefix(session_dir)
    filed: dict[str, tuple[str, str]] = {}
    for t in pending:
        _with_src(session_dir, t)
        key = t.src[len(prefix):] if t.src.startswith(prefix) else t.key
        uid = asstlink.add_todo(session_dir, t.text, key, list_name)
        if uid:
            t.uid = uid
            t.list_name = list_name or "Inbox"
            filed[t.src] = (t.uid, t.list_name)
    if filed:
        def landed(current: list[Todo]) -> bool:
            changed = False
            for row in current:
                _with_src(session_dir, row)
                hit = filed.get(row.src)
                if hit and not row.uid:
                    row.uid, row.list_name = hit
                    # It was filed open. If it was ticked while that call was
                    # in flight, asst has not heard, and the next pull would
                    # read "open there" and undo the tick.
                    row.dirty = row.done
                    changed = True
            return changed

        _edit(session_dir, landed)
    return todos


def flush_asst(session_dir: Path, todos: list[Todo] | None = None) -> list[Todo]:
    """Retry a tick that failed to reach asst.

    asstd can be down for a minute; a tick must not be lost, and when the
    daemon returns the next pull must not undo what the user already did.
    Call this before reading remote state.

    Only ticks go out. Filing an item is `file_with_asst`, run when a summary
    lands: this runs on every pull, which is every time a meeting is opened,
    and opening a meeting must not write a task into a task app.
    """
    todos = load_todos(session_dir) if todos is None else todos
    if not asstlink.available():
        return todos
    delivered: list[tuple[str, bool]] = []
    for t in todos:
        if (
            t.dirty and t.uid and not t.dropped
            and asstlink.set_done(t.uid, t.done)
        ):
            t.dirty = False
            delivered.append((t.uid, t.done))
    if delivered:
        def landed(current: list[Todo]) -> bool:
            changed = False
            for row in current:
                if row.dirty and (row.uid, row.done) in delivered:
                    row.dirty = False
                    changed = True
            return changed

        _edit(session_dir, landed)
    return todos


def pull_todos(session_dir: Path, todos: list[Todo] | None = None) -> list[Todo]:
    """Adopt asst's checked state for this meeting.

    First push whatever tick is still dirty (asstd may have just come back).
    Then remote wins when asst answers: a phone check-off has to show up
    here. A row still dirty keeps its local tick. A task deleted in asst is
    deleted here too — a row that has reached the phone and then vanished
    from it was removed on purpose. Rows that never filed (theirs, or yours
    not yet sent) are local and stay: a pull files nothing. A dropped
    row (`drop_open_todos`) is not looked for: its task was removed on
    purpose, by us. It only loses its uid once asst no longer holds it.

    The comparison is made against todos.json as it is after the (slow)
    fetch, so a tick made while this ran is a dirty row, not a lost one.
    """
    todos = flush_asst(session_dir, todos)
    if not asstlink.available():
        return todos
    # Rows already on the phone before the fetch. A row filed *during* it
    # (a summary landing) is absent from the snapshot without having been
    # deleted, and must not be dropped as if it had.
    had_uid = {t.key for t in load_todos(session_dir) if t.uid}
    remote = asstlink.fetch_session(session_dir)
    if remote is None:
        return load_todos(session_dir)
    prefix = asstlink.source_prefix(session_dir)
    by_key = {
        k[len(prefix):] if k.startswith(prefix) else k.rsplit("/", 1)[-1]: v
        for k, v in remote.items()
    }

    def reconcile(current: list[Todo]) -> bool:
        kept: list[Todo] = []
        changed = False
        for t in current:
            if t.dropped:
                # Removed on purpose, by us. A task asst no longer
                # holds is a removal done, whoever finished it.
                if t.uid and not (remote.get(t.src) or by_key.get(t.key)):
                    t.uid = ""
                    t.list_name = ""
                    changed = True
                kept.append(t)
                continue
            _with_src(session_dir, t)
            hit = remote.get(t.src) or by_key.get(t.key)
            if hit is None:
                if t.uid and not t.dirty and t.key in had_uid:
                    changed = True
                    continue  # asst deleted it; drop the row with it
                kept.append(t)
                continue
            if t.dirty:
                # Our tick has not landed; a stale remote must not undo it.
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
        current[:] = kept
        return changed

    return _edit(session_dir, reconcile)


def move_with_asst(session_dir: Path, todos: list[Todo], list_name: str | None) -> int:
    """Follow a project change into asst. Returns how many tasks moved."""
    if not asstlink.available():
        return 0
    uids = [
        t.uid for t in todos
        if t.owner == "you" and t.uid and not t.dropped
    ]
    if not uids:
        return 0
    moved = asstlink.move_todos(uids, list_name)
    if moved:
        def relist(current: list[Todo]) -> bool:
            for t in current:
                if t.uid:
                    t.list_name = list_name or "Inbox"
            return True

        _edit(session_dir, relist)
        for t in todos:
            if t.uid:
                t.list_name = list_name or "Inbox"
    return moved


def forget_asst(session_dir: Path, todos: list[Todo] | None = None) -> int:
    """The meeting is going away: take its asst tasks with it.

    uids are cleared in `todos.json` before the folder is trashed, so a later
    restore from the trash looks like unfiled local rows rather than rows whose
    phone tasks were deleted behind the user's back.
    """
    todos = load_todos(session_dir) if todos is None else todos
    # A dropped row names a task only if its removal never finished.
    uids = [t.uid for t in todos if t.uid]
    removed = asstlink.rm_todos(uids) if (uids and asstlink.available()) else 0

    def unfile(current: list[Todo]) -> bool:
        changed = False
        for t in current:
            if t.uid:
                t.uid = ""
                t.list_name = ""
                changed = True
        return changed

    _edit(session_dir, unfile)
    for t in todos:
        t.uid = ""
        t.list_name = ""
    return removed


class Dropped(NamedTuple):
    """What putting a meeting away did to the to-dos it had open."""

    removed: int  # asst tasks deleted
    stuck: int  # asst tasks it would not delete: they stay open


class Restored(NamedTuple):
    """What bringing a meeting back did to the to-dos it had closed."""

    filed: int  # yours, filed in asst again
    failed: int  # yours, which asst did not take: open here only


# Putting a meeting away and bringing it back each make slow asst calls
# between their writes. Run at once, an Undo would file a task that the
# removal then deleted, and leave a row naming a task that is gone.
_AWAY = threading.Lock()


def drop_open_todos(session_dir: Path) -> Dropped:
    """Close a meeting's open to-dos, because it is being put away.

    Each open row is marked `dropped`, and its asst task is removed
    (asst closes the issue behind a GitHub-linked list as not planned).
    A row that is done is left alone, and so is its task. A row that
    never reached asst is dropped here alone, so a meeting with no asst
    works.

    The mark is written before the removal, so a pull that runs
    meanwhile finds a dropped row and not a task "deleted in asst",
    which it would take out of todos.json. Each row gives up its uid as
    its removal is confirmed, so a dropped row that still has one is
    the list of tasks left to remove: a call cut short (killed, the
    window quit) is finished by calling this again, which also removes
    what an earlier call marked. A task asst will not remove (asstd
    down) keeps its row open: dropping it would leave an open task in
    asst that nothing here would look at again. Calling this again
    retries those.
    """
    with _AWAY:
        pull_todos(session_dir)  # what was ticked in asst since

        def mark(current: list[Todo]) -> bool:
            opened = [t for t in current if t.open]
            for t in opened:
                t.dropped = True
            return bool(opened)

        _edit(session_dir, mark)
        owed = [t for t in load_todos(session_dir) if t.dropped and t.uid]
        stuck: set[str] = set()
        for uid in dict.fromkeys(t.uid for t in owed):
            if asstlink.rm_todo(uid):
                _task_gone(session_dir, uid)
            else:
                stuck.add(uid)
        if stuck:
            def keep_open(current: list[Todo]) -> bool:
                changed = False
                for t in current:
                    if t.dropped and t.uid in stuck:
                        t.dropped = False
                        changed = True
                return changed

            _edit(session_dir, keep_open)
    return Dropped(
        removed=sum(1 for t in owed if t.uid not in stuck),
        stuck=sum(1 for t in owed if t.uid in stuck),
    )


def _task_gone(session_dir: Path, uid: str) -> None:
    """The task `uid` is gone from asst: the rows that closed over it
    stop naming it."""
    def unfile(current: list[Todo]) -> bool:
        changed = False
        for t in current:
            if t.dropped and t.uid == uid:
                t.uid = ""
                t.list_name = ""
                changed = True
        return changed

    _edit(session_dir, unfile)


def restore_dropped_todos(session_dir: Path) -> Restored:
    """Open again what putting the meeting away closed, and file yours
    in asst once more.

    Filing goes by the row's source, so a task asst still holds is
    found and not duplicated. Call it once the archive flag is cleared:
    a meeting that is put away files nothing.
    """
    with _AWAY:
        woken: list[str] = []

        def reopen(current: list[Todo]) -> bool:
            for t in current:
                if t.dropped:
                    t.dropped = False
                    # The task it named was removed.
                    t.uid = ""
                    t.list_name = ""
                    t.dirty = False
                    woken.append(t.key)
            return bool(woken)

        todos = _edit(session_dir, reopen)
        if not woken:
            return Restored(0, 0)
        todos = file_with_asst(session_dir, todos)
    mine = [t for t in todos if t.key in woken and t.owner == "you"]
    filed = sum(1 for t in mine if t.uid)
    # With no asst at all there is nothing it failed to take.
    failed = len(mine) - filed if asstlink.available() else 0
    return Restored(filed, failed)


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
        # Local only: this runs for every meeting when the list is built, and
        # opening the window must not write to a task app.
        return sync_todos(session_dir, summary.read_text(), file=False)
    except OSError:
        return []


def open_count(session_dir: Path) -> int:
    """Open items that are yours.

    What someone else promised is not a to-do on this machine; counting it
    made the sidebar badge shout about other people's work. A meeting
    put away has none: its to-dos were closed with it.
    """
    if asstlink.archived(session_dir):
        return 0
    return sum(
        1 for t in ensure_todos(session_dir)
        if t.open and t.owner != "them"
    )
