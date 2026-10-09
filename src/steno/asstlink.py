"""Hand a meeting's action items to asst, and read their state back.

asst owns the task list; steno only files items and mirrors the checkbox. The
link is `asst add --source steno:<session>/<key>`, which is idempotent on that
key, so re-summarizing a meeting never duplicates its to-dos on the phone.

Your items file into the asst list named after the meeting's project when that
list exists; otherwise the inbox. A list tied to a GitHub repo will open issues
for them, which is why an unset project stays out of those lists.

Without `asst` on PATH nothing here does anything — the local `todos.json` is
still the whole story, and a meeting must never fail because a task app is not
installed. The CLI is only a client: writes need **asstd** (the daemon) on the
session bus. The GTK window is not required.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

TIMEOUT_S = 15.0
COMPLETED_LIMIT = "9999"


def available() -> bool:
    return shutil.which("asst") is not None


def source_key(session_dir: Path, todo_key: str) -> str:
    """The `--source` that makes a re-run a no-op. Stable per session and text."""
    return f"steno:{session_dir.name}/{todo_key}"


def source_prefix(session_dir: Path) -> str:
    return f"steno:{session_dir.name}/"


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


def _parse_tasks(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return (
        [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
    )


def list_names() -> list[str]:
    """Every asst list name. Empty when asst cannot answer."""
    rows = _parse_tasks(_run(["lists"]))
    return [str(r["name"]) for r in rows if r.get("name")]


def target_list(session_dir: Path, project: str | None = None) -> str | None:
    """asst list for this meeting's project, or None for the inbox.

    A project with no matching list is not an error: meeting items still file,
    just into the inbox. asst accepts an unambiguous case-insensitive prefix,
    so match that way here and fall back rather than let `add` fail.
    """
    if project is None:
        project = _project_of(session_dir)
    if not project:
        return None
    names = list_names()
    if project in names:
        return project
    low = project.lower()
    hits = [n for n in names if n.lower().startswith(low)]
    return hits[0] if len(hits) == 1 else None


def _project_of(session_dir: Path) -> str | None:
    try:
        value = json.loads((session_dir / "meta.json").read_text()).get("project")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return str(value) if value else None


def add_todo(
    session_dir: Path,
    text: str,
    todo_key: str,
    list_name: str | None = None,
) -> str | None:
    """File one action item under the meeting's source. Returns asst's id.

    Only a recorded meeting may create tasks. A scratch directory (the test
    suite's `mkdtemp`, a half-made export, anything without `meta.json`) must
    never reach the phone — that is how fixture to-dos filled the Inbox twice
    (gotcha #13).
    """
    if not (session_dir / "meta.json").is_file():
        return None
    args = ["add", "--literal", "--source", source_key(session_dir, todo_key)]
    if list_name:
        args.extend(["--list", list_name])
    raw = _run([*args, "--", text])
    return _parse_id(raw)


def set_done(uid: str, done: bool) -> bool:
    """Mirror a checkbox into asst. False means it did not take."""
    cmd = "done" if done else "reopen"
    return _run([cmd, uid]) is not None


def completed(uid: str) -> bool | None:
    """Read one task's checked state back. None when asst cannot answer."""
    return _parse_completed(_run(["show", uid]))


def move_todos(uids: list[str], list_name: str | None) -> int:
    """Move filed tasks to `list_name` (None = the inbox). Returns how many moved.

    Only touches rows that already have an asst id. A missing id is left alone
    rather than treated as a failure the meeting should care about.
    """
    target = list_name or "inbox"
    moved = 0
    for uid in uids:
        if uid and _run(["edit", uid, "--list", target]) is not None:
            moved += 1
    return moved


def rm_todos(uids: list[str]) -> int:
    """Delete filed tasks. Returns how many asst accepted.

    Used when the meeting itself goes away. asst is the task store: if the
    meeting is gone, the tasks it filed should not outlive it on the phone.
    """
    removed = 0
    for uid in uids:
        if uid and _run(["rm", uid]) is not None:
            removed += 1
    return removed


def fetch_session(session_dir: Path) -> dict[str, dict] | None:
    """This meeting's asst tasks, keyed by their `--source` string.

    Two process calls cover open and completed; `completed` defaults to 20
    rows, so the limit has to be explicit or old check-offs look open again.
    Each value is `{"id", "completed", "list"}`. Pull is read-only: nothing is
    filed or deleted here. None when asst cannot answer — that is not the same
    as "no tasks", and must not clear local uids.
    """
    open_raw = _run(["ls"])
    done_raw = _run(["completed", "-n", COMPLETED_LIMIT])
    if open_raw is None and done_raw is None:
        return None
    prefix = source_prefix(session_dir)
    out: dict[str, dict] = {}
    for raw in (open_raw, done_raw):
        for row in _parse_tasks(raw):
            source = row.get("source")
            if not isinstance(source, str) or not source.startswith(prefix):
                continue
            out[source] = {
                "id": str(row.get("id") or ""),
                "completed": str(row.get("status") or "").lower() == "completed"
                or row.get("completed") is not None,
                "list": str(row.get("list_name") or ""),
            }
    return out
