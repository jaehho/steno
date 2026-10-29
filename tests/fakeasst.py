"""An `asst` for tests that file, remove and re-file tasks.

A script on a PATH of its own, so the code under test runs the same
subprocess path as against the real one: exit status, what it prints
and what it leaves unsaid (`rm` says nothing when it works). The real
`asst` is not on that PATH, and its D-Bus address is dead besides
(tests/__init__). Every call is logged. Not a test module: discovery
skips it.

What it prints is asst's own samples (`asst-contract/`, a copy of asst's
`contract/`) with the fields this fake tracks written in, and it takes
only the calls asst promises steno there (`consumers.json`). Any other
call fails the test that made it, in `stop`, so steno cannot start
relying on a call or a field asst never agreed to.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

from steno import asstlink
from tests import real_available

CONTRACT = Path(__file__).resolve().parent / "asst-contract"

SHIM = r'''#!@PYTHON@
import json
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
args = sys.argv[1:]
with (here / "calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
state = json.loads((here / "state.json").read_text())
tasks = state["tasks"]
contract = Path(state["contract"])


def sample(name):
    return json.loads((contract / name).read_text())


def shape(argv):
    """The command and its options, without their values."""
    flags = set()
    for arg in argv:
        if arg == "--":
            flags.add(arg)
            break
        if arg.startswith("-"):
            flags.add(arg.split("=", 1)[0])
    words = [a for a in argv if a != "--json"]
    return [words[0] if words else "", sorted(flags)]


def fail(why, code=1):
    print("asst: " + why, file=sys.stderr)
    sys.exit(code)


promised = [shape(c) for c in sample("consumers.json")["steno"]["calls"]]
if shape(args) not in promised:
    with (here / "unpromised.jsonl").open("a") as log:
        log.write(json.dumps(args) + "\n")
    fail("not a call asst promises steno (contract/consumers.json)", 2)


def find(uid):
    live = (t for t in tasks if not t["deleted"])
    return next((t for t in live if t["id"] == uid), None)


def view(task, template=None):
    """The task as asst prints it: a sample row with this task's fields."""
    done = task["status"] == "completed"
    if template is None:
        template = sample("completed.json" if done else "ls.json")[0]
    row = dict(template)
    row.update(
        id=task["id"], source=task["source"], status=task["status"],
        list_name=task["list_name"], summary=task["title"],
    )
    if done:
        row.setdefault("completed", sample("completed.json")[0]["completed"])
    else:
        row.pop("completed", None)
    return row


def save():
    (here / "state.json").write_text(json.dumps(state))


if state["down"]:
    fail("no daemon on the bus")
if args[:1] == ["--json"]:
    args = args[1:]
cmd, rest = args[0], args[1:]
if cmd == "lists":
    shown = next(r for r in sample("lists.json") if not r["inbox"])
    print(json.dumps([dict(shown, name=n) for n in state["lists"]]))
elif cmd == "add":
    source = rest[rest.index("--source") + 1]
    task = next(
        (t for t in tasks if t["source"] == source and not t["deleted"]), None
    )
    existed = task is not None
    if task is None:
        task = {
            "id": f"T{len(tasks) + 1:03d}",
            "source": source,
            "status": "needs-action",
            "title": rest[-1],
            "list_name": rest[rest.index("--list") + 1]
            if "--list" in rest else "Inbox",
            "deleted": False,
        }
        tasks.append(task)
        save()
    print(json.dumps(dict(view(task, sample("add.json")), existed=existed)))
elif cmd in ("ls", "completed"):
    want = "needs-action" if cmd == "ls" else "completed"
    print(json.dumps([
        view(t) for t in tasks if not t["deleted"] and t["status"] == want
    ]))
elif cmd in ("done", "reopen", "show", "edit"):
    task = find(rest[0]) or fail("no such task")
    if cmd == "done":
        task["status"] = "completed"
    elif cmd == "reopen":
        task["status"] = "needs-action"
    elif cmd == "edit":
        if "--list" in rest:
            task["list_name"] = rest[rest.index("--list") + 1]
        for arg in rest[1:]:
            if arg.startswith("--title="):
                task["title"] = arg[len("--title="):]
    save()
    print(json.dumps(view(task)))
elif cmd == "open":
    task = find(rest[0]) or fail("no such task")
elif cmd == "rm":
    task = find(rest[0]) or fail("no such task")
    if rest[0] in state["refuse_rm"]:
        fail("the daemon would not delete it")
    task["deleted"] = True
    save()
else:
    fail("not faked: " + cmd)
'''


class FakeAsst:
    """`with FakeAsst() as asst:` makes `asst` a script that keeps its
    tasks in a file, and turns the switch that keeps scratch runs off
    asst off for the block. `down` makes every call fail, as asstd not
    running does."""

    def __init__(self, lists: tuple[str, ...] = ("thesis", "lab")) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._state = {
            "down": False, "refuse_rm": [], "lists": list(lists), "tasks": [],
            "contract": str(CONTRACT),
        }
        self._write()
        script = self.dir / "asst"
        script.write_text(SHIM.replace("@PYTHON@", sys.executable))
        script.chmod(0o755)
        self._patches: list = []

    def _write(self) -> None:
        (self.dir / "state.json").write_text(json.dumps(self._state))

    def _read(self) -> dict:
        return json.loads((self.dir / "state.json").read_text())

    def start(self) -> FakeAsst:
        # Only this PATH, so no other `asst` can be the one that runs.
        env = mock.patch.dict(os.environ, {"PATH": str(self.dir)})
        env.start()
        os.environ.pop(asstlink.OFF_ENV, None)
        available = mock.patch.object(asstlink, "available", real_available)
        available.start()
        self._patches = [available, env]
        return self

    def stop(self) -> None:
        for patch in self._patches:
            patch.stop()
        self._patches = []
        unpromised = self.dir / "unpromised.jsonl"
        calls = unpromised.read_text() if unpromised.is_file() else ""
        self._tmp.cleanup()
        if calls:
            msg = (
                "steno made calls asst does not promise it "
                f"(tests/asst-contract/consumers.json):\n{calls}"
            )
            raise AssertionError(msg)

    def __enter__(self) -> FakeAsst:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def down(self) -> bool:
        return self._read()["down"]

    @down.setter
    def down(self, value: bool) -> None:
        state = self._read()
        state["down"] = value
        self._state = state
        self._write()

    def refuse_rm(self, *uids: str) -> None:
        """Only these tasks cannot be deleted: `rm` exits 1."""
        state = self._read()
        state["refuse_rm"] = list(uids)
        self._state = state
        self._write()

    def tasks(self, *, status: str | None = None) -> list[dict]:
        """The tasks that still exist (a removed one is not listed)."""
        return [
            t for t in self._read()["tasks"]
            if not t["deleted"] and status in (None, t["status"])
        ]

    def complete(self, uid: str) -> None:
        """Tick a task the way the phone would."""
        state = self._read()
        for t in state["tasks"]:
            if t["id"] == uid:
                t["status"] = "completed"
        self._state = state
        self._write()

    def remove(self, uid: str) -> None:
        """Delete a task the way the user would in asst's own window."""
        state = self._read()
        for t in state["tasks"]:
            if t["id"] == uid:
                t["deleted"] = True
        self._state = state
        self._write()

    def calls(self, command: str | None = None) -> list[list[str]]:
        """What was run, minus the leading --json."""
        path = self.dir / "calls.jsonl"
        if not path.is_file():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows = [r[1:] if r[:1] == ["--json"] else r for r in rows]
        return [r for r in rows if command in (None, r[0])]
