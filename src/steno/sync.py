"""Pushing finished meetings to a WebDAV remote, such as a Nextcloud.

Off unless `STENO_SYNC_REMOTE` names an rclone remote and folder
(`nextcloud:Steno/sessions`). With it unset nothing here touches the network.

The remote is an archive, not a mirror. `rclone copy --update` only adds and
replaces; it never deletes, so trimming audio (`STENO_KEEP_AUDIO_DAYS`) or
trashing a meeting stays local. A meeting is pushed once it is finished, and
again when a later edit (a ticked to-do, a new summary) changes it. See the
decision in issue #37.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .state import read_state
from .summarize import iter_sessions

REMOTE_ENV = "STENO_SYNC_REMOTE"

# A meeting written to this recently may still be growing or half-edited.
QUIET_S = 300

# The file a reader on the far side treats as "this meeting is complete", so
# it goes last.
LAST_FILE = "meta.json"

STATE_FILENAME = "sync.json"
LOCK_FILENAME = "sync.lock"
RCLONE_TIMEOUT_S = 6 * 3600


def remote() -> str:
    """Where meetings go, or "" when syncing is off."""
    return os.environ.get(REMOTE_ENV, "").strip().rstrip("/")


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "steno"


def load_state() -> dict[str, float]:
    """Per meeting, the newest file time that is already on the remote."""
    try:
        data = json.loads((state_dir() / STATE_FILENAME).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    pushed = data.get("pushed") if isinstance(data, dict) else None
    return pushed if isinstance(pushed, dict) else {}


def save_state(pushed: dict[str, float]) -> None:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / STATE_FILENAME
    scratch = path.with_suffix(".tmp")
    scratch.write_text(json.dumps({"pushed": pushed}, indent=2) + "\n")
    scratch.replace(path)


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


@dataclass
class Result:
    pushed: list[str] = field(default_factory=list)
    unchanged: int = 0
    busy: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    off: bool = False
    locked: bool = False


def _rclone_copy(source: Path, target: str, *filters: str) -> str:
    """Run one `rclone copy`. Returns "" on success, else why not."""
    command = [
        "rclone", "copy", str(source), target, "--update",
        "--retries", "2", "--timeout", "5m", "--contimeout", "30s",
        "--exclude", "*.tmp", *filters,
    ]
    try:
        done = subprocess.run(
            command, capture_output=True, text=True,
            timeout=RCLONE_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if done.returncode == 0:
        return ""
    return (done.stderr.strip().splitlines() or [f"exit {done.returncode}"])[-1]


def push_session(session_dir: Path, target_root: str) -> str:
    """Copy one meeting, `meta.json` last. "" on success, else why not."""
    target = f"{target_root}/{session_dir.name}"
    problem = _rclone_copy(session_dir, target, "--exclude", LAST_FILE)
    return problem or _rclone_copy(session_dir, target, "--include", LAST_FILE)


def sync(root: Path | None = None, *, dry_run: bool = False,
         now: float | None = None) -> Result:
    """Push every finished meeting that is new or changed since last time."""
    target_root = remote()
    result = Result()
    if not target_root:
        result.off = True
        return result
    if not dry_run and shutil.which("rclone") is None:
        result.failed["rclone"] = "rclone is not on PATH"
        return result
    state_dir().mkdir(parents=True, exist_ok=True)
    with (state_dir() / LOCK_FILENAME).open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            result.locked = True
            return result
        return _push_all(root, target_root, result, dry_run=dry_run,
                         now=time.time() if now is None else now)


def _push_all(root: Path | None, target_root: str, result: Result, *,
              dry_run: bool, now: float) -> Result:
    pushed = load_state()
    recording = recording_session(root)
    for session_dir in sorted(iter_sessions(root), key=lambda p: p.name):
        name = session_dir.name
        newest = newest_mtime(session_dir)
        if pushed.get(name, 0.0) >= newest:
            result.unchanged += 1
        elif session_dir == recording or now - newest < QUIET_S:
            result.busy.append(name)
        elif dry_run:
            result.pushed.append(name)
        elif problem := push_session(session_dir, target_root):
            result.failed[name] = problem
        else:
            pushed[name] = newest
            result.pushed.append(name)
            save_state(pushed)
    return result

