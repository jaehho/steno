"""Hand a meeting's action items to asst, and read their state back.

asst owns the task list; steno only files items and mirrors the checkbox. The
link is `asst add --source steno:<session>/<key>`, which is idempotent on that
key, so re-summarizing a meeting never duplicates its to-dos on the phone.

Without `asst` on PATH nothing here does anything — the local `todos.json` is
still the whole story, and a meeting must never fail because a task app is not
installed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

TIMEOUT_S = 15.0


def available() -> bool:
    return shutil.which("asst") is not None


def source_key(session_dir: Path, todo_key: str) -> str:
    """The `--source` that makes a re-run a no-op. Stable per session and text."""
    return f"steno:{session_dir.name}/{todo_key}"


def _run(args: list[str]) -> str | None:
    """`asst --json …`, or None if asst is missing or refuses.

    `--json` leads, so it cannot fall after a `--` and become title text.
    """
    if not available():
        return None
    try:
        proc = subprocess.run(
            ["asst", "--json", *args],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _parse_id(raw: str | None) -> str | None:
    """The short UID out of `asst add --json` (an `Added`/`TaskView`)."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and data.get("id"):
        return str(data["id"])
    return None


def _parse_completed(raw: str | None) -> bool | None:
    """Completed state out of `asst show --json`, or None if it cannot tell."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if "completed" in data:
        return data.get("completed") is not None
    if "status" in data:
        return str(data["status"]).lower() == "completed"
    return None


def add_todo(session_dir: Path, text: str, todo_key: str) -> str | None:
    """File one action item under the meeting's source. Returns asst's id."""
    raw = _run([
        "add",
        "--literal",
        "--source", source_key(session_dir, todo_key),
        "--", text,
    ])
    return _parse_id(raw)


def set_done(uid: str, done: bool) -> bool:
    """Mirror a checkbox into asst. False means it did not take."""
    cmd = "done" if done else "reopen"
    return _run([cmd, uid]) is not None


def completed(uid: str) -> bool | None:
    """Read one task's checked state back. None when asst cannot answer."""
    return _parse_completed(_run(["show", uid]))
