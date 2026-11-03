"""Keeping this device's meetings in step with a main store on a server.

The server folder (`STENO_SYNC_REMOTE`, an rclone remote and folder such as
`nextcloud:Steno/sessions`) is the main store. This device holds a full replica
in the sessions folder and reads and writes only that, so nothing waits on the
network and everything works offline. `sync` reconciles the two. Unset, nothing
here touches the network. The rules are issue #39.

The work is split so the rules can be read and tested without a server:
`plan` is pure (what each side holds, what held at the last sync, which
meetings are settled, and it returns the actions), and the functions below it
carry the actions out through rclone, which also accepts a plain folder as a
remote.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from .retention import AUDIO_FILES, delete_session
from .state import read_state
from .summarize import iter_sessions, sessions_root

REMOTE_ENV = "STENO_SYNC_REMOTE"
AUDIO_DAYS_ENV = "STENO_SYNC_AUDIO_DAYS"
DEFAULT_AUDIO_DAYS = 30
SECONDS_PER_DAY = 86400

# A meeting written to this recently may still be growing or half-edited.
QUIET_S = 300

# Uploaded last, so a reader on the far side can take its presence to mean the
# meeting is complete.
LAST_FILE = "meta.json"

TRASH_DIR = ".trash"
STATE_FILENAME = "sync.json"
LOCK_FILENAME = "sync.lock"
STATE_VERSION = 2

# Never synced: ours while pulling, rclone's, and the copy kept aside when both
# sides changed a file (the server's version took the name).
SCRATCH_SUFFIXES = (".tmp", ".partial")
CONFLICT_MARK = ".conflict-"

# A round that would trash more meetings than this share of the known ones is
# held back (never fewer than MIN_HELD).
HELD_SHARE = 4
MIN_HELD = 2

LIST_FLAGS = ("--retries", "1", "--contimeout", "10s", "--timeout", "30s")
MOVE_FLAGS = ("--retries", "2", "--contimeout", "30s", "--timeout", "5m",
              "--ignore-times")
RCLONE_TIMEOUT_S = 6 * 3600
RCLONE_NOT_FOUND = 3

PUSH, PULL, CONFLICT = "push", "pull", "conflict"
TRASH_REMOTE, TRASH_LOCAL = "trash-remote", "trash-local"

# {meeting: {file: fingerprint}}; a text file's fingerprint is its sha1, an
# audio file's is its size, because audio is written once and never edited.
Files = dict[str, dict[str, str]]
PATH_PARTS = 2  # a server path is meeting/file


class Snapshot(NamedTuple):
    """What each side holds now, and what both held at the last sync."""

    local: Files
    server: Files
    base: Files


class Sides(NamedTuple):
    """One meeting's files on each side and at the last sync; None where the
    meeting is not there."""

    here: dict[str, str] | None
    there: dict[str, str] | None
    known: dict[str, str] | None


@dataclass(frozen=True)
class Action:
    kind: str
    session: str
    rel: str = ""

    def __str__(self) -> str:
        return f"{self.session}/{self.rel}" if self.rel else self.session


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    agreed: Files = field(default_factory=dict)
    forgotten: list[str] = field(default_factory=list)
    busy: list[str] = field(default_factory=list)
    held: list[Action] = field(default_factory=list)


@dataclass
class Result:
    """How a round ended. `status` is ok, off, locked, unreachable, held or
    error; the lists name `meeting/file` (a meeting alone for a trashing)."""

    status: str = "ok"
    detail: str = ""
    pushed: list[str] = field(default_factory=list)
    pulled: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    trashed_remote: list[str] = field(default_factory=list)
    trashed_local: list[str] = field(default_factory=list)
    busy: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    unchanged: int = 0


class SyncError(Exception):
    """A step failed; the message says why. Retried next round."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class StaleError(Exception):
    """A file changed on this device after the plan was made: wait."""


# ------------------------------------------------------------ what is on disk

def remote() -> str:
    """Where meetings go, or "" when syncing is off."""
    return os.environ.get(REMOTE_ENV, "").strip().rstrip("/")


def audio_days() -> int:
    """Days back to pull other devices' audio for; 0 is text only."""
    try:
        return max(0, int(os.environ.get(AUDIO_DAYS_ENV, DEFAULT_AUDIO_DAYS)))
    except ValueError:
        return DEFAULT_AUDIO_DAYS


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "steno"


def is_audio(rel: str) -> bool:
    return rel in AUDIO_FILES


def is_unsynced(name: str) -> bool:
    return name.endswith(SCRATCH_SUFFIXES) or CONFLICT_MARK in name


def sha1_of(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(path: Path, rel: str | None = None) -> str:
    """What to compare a file by: sha1 for text, size for audio. `rel` is the
    file's name in its meeting when `path` is a scratch copy of it."""
    if is_audio(rel or path.name):
        return str(path.stat().st_size)
    return sha1_of(path)


def session_dirs(root: Path | None) -> list[Path]:
    base = sessions_root(root)
    if not base.is_dir():
        return []
    return sorted(
        p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")
    )


def local_files(root: Path | None = None) -> Files:
    """Every file of every meeting in this device's replica."""
    return {
        session_dir.name: {
            path.name: fingerprint(path)
            for path in sorted(session_dir.iterdir())
            if path.is_file() and not is_unsynced(path.name)
        }
        for session_dir in session_dirs(root)
    }


def newest_mtime(session_dir: Path) -> float:
    """When anything in the meeting was last written."""
    newest = 0.0
    for path in session_dir.rglob("*"):
        with contextlib.suppress(OSError):
            newest = max(newest, path.stat().st_mtime)
    return newest


def recording_session(root: Path | None = None) -> Path | None:
    """The meeting being recorded right now, if there is one."""
    sessions = iter_sessions(root)
    if read_state().get("status") == "recording" and sessions:
        return sessions[0]
    return None


def busy_sessions(root: Path | None, now: float) -> set[str]:
    """Meetings that may be growing or half-edited: left alone both ways."""
    recording = recording_session(root)
    busy = {recording.name} if recording is not None else set()
    busy.update(
        p.name for p in session_dirs(root) if now - newest_mtime(p) < QUIET_S
    )
    return busy


def audio_window(sessions: list[str], now: float, days: int) -> set[str]:
    """The meetings recent enough that their audio is pulled."""
    cutoff = now - days * SECONDS_PER_DAY
    recent = set()
    for name in sessions:
        with contextlib.suppress(ValueError):
            started = datetime.strptime(name[:15], "%Y%m%d-%H%M%S")
            if started.timestamp() >= cutoff:
                recent.add(name)
    return recent if days > 0 else set()


# ------------------------------------------------------------- what held last

def _read_state_file() -> dict:
    try:
        data = json.loads((state_dir() / STATE_FILENAME).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    ours = isinstance(data, dict) and data.get("version") == STATE_VERSION
    return data if ours else {}


def load_base() -> Files:
    """What both sides held, per file, after the last successful sync."""
    sessions = _read_state_file().get("sessions")
    return sessions if isinstance(sessions, dict) else {}


def load_last() -> dict:
    """How the last round ended: {"at", "status", "detail"}, or {}."""
    last = _read_state_file().get("last")
    return last if isinstance(last, dict) else {}


def save_state(base: Files, last: dict) -> None:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / STATE_FILENAME
    scratch = path.with_suffix(".tmp")
    scratch.write_text(json.dumps(
        {"version": STATE_VERSION, "sessions": base, "last": last}, indent=2,
    ) + "\n")
    scratch.replace(path)


# ------------------------------------------------------------------- planning

def plan(snapshot: Snapshot, *, busy: set[str], audio_pull: set[str],
         allow_deletes: bool = False) -> Plan:
    """What to do so both sides hold the same meetings.

    `busy` are the meetings left alone this round, `audio_pull` the meetings
    whose audio is fetched from the server when missing here.
    """
    result = Plan()
    names = set(snapshot.local) | set(snapshot.server) | set(snapshot.base)
    for name in sorted(names):
        sides = Sides(snapshot.local.get(name), snapshot.server.get(name),
                      snapshot.base.get(name))
        if sides.here is None and sides.there is None:
            result.forgotten.append(name)
        elif name in busy and sides.here is not None:
            result.busy.append(name)
        elif sides.here is None:
            _plan_server_only(result, name, sides, audio_pull)
        elif sides.there is None:
            _plan_local_only(result, name, sides)
        else:
            _plan_both(result, name, sides, audio_pull)
    _hold_deletes(result, snapshot, allow_deletes)
    return result


def _plan_server_only(result: Plan, name: str, sides: Sides,
                      audio_pull: set[str]) -> None:
    """A meeting only the server holds: new from elsewhere, or trashed here."""
    there = sides.there or {}
    if sides.known is not None:
        result.actions.append(Action(TRASH_REMOTE, name))
    elif LAST_FILE in there:
        result.actions.extend(
            Action(PULL, name, rel) for rel in there
            if not is_audio(rel) or name in audio_pull
        )


def _plan_local_only(result: Plan, name: str, sides: Sides) -> None:
    """A meeting only this device holds: new here, or trashed elsewhere."""
    if sides.known is not None:
        result.actions.append(Action(TRASH_LOCAL, name))
    else:
        result.actions.extend(
            Action(PUSH, name, rel) for rel in sides.here or {}
        )


def _plan_both(result: Plan, name: str, sides: Sides,
               audio_pull: set[str]) -> None:
    """A meeting both sides hold: each file, against what held at the last
    sync. A file missing on one side is restored from the other, not deleted;
    audio missing here after it was held is an eviction and stays missing."""
    here, there, known = sides.here or {}, sides.there or {}, sides.known or {}
    for rel in sorted(set(here) | set(there)):
        ours, theirs, before = here.get(rel), there.get(rel), known.get(rel)
        if ours == theirs:
            result.agreed.setdefault(name, {})[rel] = ours or ""
        elif ours is None:
            if not is_audio(rel) or (before is None and name in audio_pull):
                result.actions.append(Action(PULL, name, rel))
        elif theirs is None or theirs == before:
            result.actions.append(Action(PUSH, name, rel))
        elif ours == before:
            result.actions.append(Action(PULL, name, rel))
        else:
            result.actions.append(Action(CONFLICT, name, rel))


def _hold_deletes(result: Plan, snapshot: Snapshot,
                  allow_deletes: bool) -> None:
    """Hold back trashing that looks like an outage or a mistake: an empty
    server, or more meetings at once than a few. (An empty replica is not
    held: the server's copy only moves to `.trash`, and the count rule
    covers a replica wiped of many meetings.)"""
    deletes = [
        a for a in result.actions if a.kind in (TRASH_REMOTE, TRASH_LOCAL)
    ]
    if allow_deletes or not deletes:
        return
    emptied = not snapshot.server and any(a.kind == TRASH_LOCAL for a in deletes)
    too_many = len(deletes) > max(MIN_HELD, len(snapshot.base) // HELD_SHARE)
    if emptied or too_many:
        result.held = deletes
        result.actions = [a for a in result.actions if a not in deletes]


def order_actions(actions: list[Action]) -> list[Action]:
    """Text before audio, and `meta.json` last of an upload, so the far side
    sees a meeting complete only when it is. A download takes `meta.json`
    before the audio: the audio is optional and large."""
    def rank(action: Action) -> int:
        text, audio, last = 0, 2, 1
        if action.kind == PULL:
            return audio if is_audio(action.rel) else (
                last if action.rel == LAST_FILE else text)
        if action.rel == LAST_FILE:
            return audio + 1
        return audio if is_audio(action.rel) else text
    return sorted(actions, key=lambda a: (a.session, rank(a), a.rel))


# ------------------------------------------------------------------- the shell

def _rclone(args: list[str], *flags: str) -> str:
    try:
        done = subprocess.run(
            ["rclone", *args, *flags], capture_output=True, text=True,
            timeout=RCLONE_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncError(str(exc)) from exc
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines() or [f"exit {done.returncode}"]
        raise SyncError(lines[-1], code=done.returncode)
    return done.stdout


def list_server(target_root: str) -> Files:
    """What the server holds. An absent folder is an empty server."""
    try:
        out = _rclone(["lsjson", "-R", "--files-only", "--hash",
                       "--hash-type", "sha1", target_root], *LIST_FLAGS)
    except SyncError as exc:
        if exc.code != RCLONE_NOT_FOUND:
            raise
        return {}
    found: Files = {}
    for entry in json.loads(out):
        parts = entry["Path"].split("/")
        if len(parts) != PATH_PARTS or parts[0].startswith("."):
            continue
        session, rel = parts
        if is_unsynced(rel):
            continue
        sha1 = (entry.get("Hashes") or {}).get("sha1")
        if not is_audio(rel) and sha1 is None:
            raise SyncError(  # noqa: TRY003 - the message is the point
                f"the server gave no sha1 for {entry['Path']}")
        found.setdefault(session, {})[rel] = (
            str(entry["Size"]) if is_audio(rel) else sha1
        )
    return found


def copy_file(source: str, target: str) -> None:
    _rclone(["copyto", source, target], *MOVE_FLAGS)


def conflict_name(rel: str, host: str, now: float) -> str:
    stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
    return f"{rel}{CONFLICT_MARK}{host}-{stamp}"


def download(target_root: str, session_dir: Path, action: Action,
             expected: str) -> Path:
    """Download into a scratch file next to the destination and check it is
    the version the plan saw. The caller puts it in place, or deletes it."""
    destination = session_dir / action.rel
    session_dir.mkdir(parents=True, exist_ok=True)
    scratch = destination.with_name(destination.name + ".sync.tmp")
    try:
        copy_file(f"{target_root}/{action.session}/{action.rel}", str(scratch))
        intact = fingerprint(scratch, action.rel) == expected
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise
    if not intact:
        scratch.unlink(missing_ok=True)
        raise SyncError(  # noqa: TRY003
            f"{action.rel} changed on the server mid-copy")
    return scratch


def trash_on_server(target_root: str, session: str, now: float) -> None:
    stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
    _rclone(["moveto", f"{target_root}/{session}",
             f"{target_root}/{TRASH_DIR}/{session}-{stamp}"], *MOVE_FLAGS)


@dataclass
class Round:
    """One reconciliation: what is being synced, and what held before."""

    target_root: str
    root: Path | None
    base: Files
    local: Files
    server: Files
    now: float
    host: str
    result: Result = field(default_factory=Result)

    def settle(self, name: str, rel: str, fp: str) -> None:
        self.base.setdefault(name, {})[rel] = fp

    def session_dir(self, name: str) -> Path:
        return sessions_root(self.root) / name

    def planned(self, action: Action) -> tuple[str | None, str | None]:
        """What this device and the server held for the action's file."""
        return (self.local.get(action.session, {}).get(action.rel),
                self.server.get(action.session, {}).get(action.rel))


def carry_out(rnd: Round, action: Action) -> None:
    """Do one action, and record what both sides now hold."""
    here, there = rnd.planned(action)
    if action.kind == PUSH:
        copy_file(str(rnd.session_dir(action.session) / action.rel),
                  f"{rnd.target_root}/{action.session}/{action.rel}")
        rnd.settle(action.session, action.rel, here or "")
        rnd.result.pushed.append(str(action))
    elif action.kind in (PULL, CONFLICT):
        _pull_or_keep_both(rnd, action, here, there or "")
    elif action.kind == TRASH_REMOTE:
        trash_on_server(rnd.target_root, action.session, rnd.now)
        rnd.base.pop(action.session, None)
        rnd.result.trashed_remote.append(action.session)
    elif action.kind == TRASH_LOCAL:
        outcome, _where = delete_session(rnd.session_dir(action.session))
        if outcome != "trashed":
            raise SyncError(  # noqa: TRY003
                f"could not move {action.session} to the trash")
        rnd.base.pop(action.session, None)
        rnd.result.trashed_local.append(action.session)


def _pull_or_keep_both(rnd: Round, action: Action, here: str | None,
                       there: str) -> None:
    session_dir = rnd.session_dir(action.session)
    path = session_dir / action.rel
    if action.kind == CONFLICT and is_audio(action.rel):
        rnd.result.conflicts.append(str(action))
        return
    scratch = download(rnd.target_root, session_dir, action, there)
    try:
        if here is not None and fingerprint(path) != here:
            raise StaleError(str(action))
        if action.kind == CONFLICT:
            aside = path.with_name(conflict_name(action.rel, rnd.host, rnd.now))
            shutil.copy2(path, aside)
            rnd.result.conflicts.append(
                f"{action} (yours kept as {aside.name})")
        scratch.replace(path)
    finally:
        scratch.unlink(missing_ok=True)
    rnd.settle(action.session, action.rel, there)
    if action.kind == PULL:
        rnd.result.pulled.append(str(action))


# ------------------------------------------------------------------ the round

def sync(root: Path | None = None, *, dry_run: bool = False,
         allow_deletes: bool = False, now: float | None = None) -> Result:
    """Reconcile this device's meetings with the server, both ways."""
    target_root = remote()
    if not target_root:
        return Result(status="off")
    if shutil.which("rclone") is None:
        return Result(status="error", detail="rclone is not on PATH",
                      failed={"rclone": "not on PATH"})
    state_dir().mkdir(parents=True, exist_ok=True)
    with (state_dir() / LOCK_FILENAME).open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return Result(status="locked")
        return _run_round(root, target_root, dry_run=dry_run,
                          allow_deletes=allow_deletes,
                          now=time.time() if now is None else now)


def _run_round(root: Path | None, target_root: str, *, dry_run: bool,
               allow_deletes: bool, now: float) -> Result:
    try:
        server = list_server(target_root)
    except (SyncError, json.JSONDecodeError) as exc:
        result = Result(status="unreachable", detail=str(exc))
        if not dry_run:
            save_state(load_base(), _last(result, now))
        return result
    rnd = Round(target_root, root, load_base(), local_files(root), server, now,
                socket.gethostname())
    found = plan(
        Snapshot(rnd.local, server, rnd.base),
        busy=busy_sessions(root, now),
        audio_pull=audio_window(
            sorted(set(rnd.local) | set(server)), now, audio_days()),
        allow_deletes=allow_deletes,
    )
    rnd.result.busy = found.busy
    rnd.result.held = [str(a) for a in found.held]
    rnd.result.unchanged = sum(len(files) for files in found.agreed.values())
    if dry_run:
        _describe(rnd.result, found.actions)
        return rnd.result
    _apply(rnd, found)
    save_state(rnd.base, _last(rnd.result, now))
    return rnd.result


def _describe(result: Result, actions: list[Action]) -> None:
    """A dry run reports what it would do, in the lists a real run fills."""
    lists = {
        PUSH: result.pushed, PULL: result.pulled, CONFLICT: result.conflicts,
        TRASH_REMOTE: result.trashed_remote, TRASH_LOCAL: result.trashed_local,
    }
    for action in order_actions(actions):
        lists[action.kind].append(str(action))


def _apply(rnd: Round, found: Plan) -> None:
    for name, files in found.agreed.items():
        rnd.base.setdefault(name, {}).update(files)
    for name in found.forgotten:
        rnd.base.pop(name, None)
    for action in order_actions(found.actions):
        try:
            carry_out(rnd, action)
        except StaleError:
            rnd.result.busy.append(str(action))
        except (SyncError, OSError) as exc:
            rnd.result.failed[str(action)] = str(exc)
        else:
            save_state(rnd.base, load_last())
    if rnd.result.failed:
        rnd.result.status = "error"
        rnd.result.detail = f"{len(rnd.result.failed)} failed"
    elif rnd.result.held:
        rnd.result.status = "held"
        rnd.result.detail = f"{len(rnd.result.held)} deletions held back"


def _last(result: Result, now: float) -> dict:
    return {"at": now, "status": result.status, "detail": result.detail}


# ----------------------------------------------------------------- on demand

def fetch_audio(name: str, root: Path | None = None, *,
                now: float | None = None) -> Result:
    """Pull one meeting's audio from the server, whatever the window and
    whatever was trimmed here. It also clears the "audio removed" marks."""
    target_root = remote()
    if not target_root:
        return Result(status="off")
    stamp = time.time() if now is None else now
    try:
        server = list_server(target_root)
    except (SyncError, json.JSONDecodeError) as exc:
        return Result(status="unreachable", detail=str(exc))
    if name not in server:
        return Result(status="error", detail=f"{name} is not on the server")
    rnd = Round(target_root, root, load_base(), local_files(root), server,
                stamp, socket.gethostname())
    for rel in filter(is_audio, server[name]):
        rnd.base.get(name, {}).pop(rel, None)
    found = plan(Snapshot(rnd.local, server, rnd.base), busy=set(),
                 audio_pull={name})
    audio = [a for a in found.actions if a.session == name and is_audio(a.rel)]
    _apply(rnd, Plan(actions=audio))
    if rnd.result.pulled:
        _clear_audio_marks(rnd.session_dir(name) / LAST_FILE)
    return rnd.result


def _clear_audio_marks(meta_path: Path) -> None:
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    marks = [k for k in ("audio_pruned_at", "audio_freed_bytes") if k in meta]
    if not marks:
        return
    for key in marks:
        del meta[key]
    scratch = meta_path.with_name(meta_path.name + ".sync.tmp")
    scratch.write_text(json.dumps(meta, indent=2) + "\n")
    scratch.replace(meta_path)


def conflicts_on_disk(root: Path | None = None) -> list[str]:
    """Copies kept aside after a conflict, until someone deletes them."""
    return sorted(
        f"{p.parent.name}/{p.name}"
        for session_dir in session_dirs(root)
        for p in session_dir.glob(f"*{CONFLICT_MARK}*")
    )
