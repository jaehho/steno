"""Which checkout the running app was started from.

The launcher runs whatever tree `STENO_SRC` names. A build handed over
for a try is idle when it is swapped in, so nothing stops it from being
the one that records the next real meeting, and nothing said which
tree was live: on 2026-11-02 a worktree recorded a meeting and the
only trace was a traceback path in this log.

So the app says it once at startup, in the log, which outlives every
worktree, and in a lease file beside the log that the bar reads. The
lease carries the app's PID: a lease whose PID is not the running
app's says nothing about the running app.

No GUI or engine import: `steno bar` reads the lease every second.
"""
from __future__ import annotations

import contextlib
import json
import logging
import subprocess
from pathlib import Path
from typing import NamedTuple

from .log import log_path

LEASE_FILENAME = "live-src.json"
# What `make install` bakes into the launcher, which exports it.
DEFAULT_ROOT_ENV = "STENO_DEFAULT_ROOT"
MAIN_BRANCH = "main"
GIT_TIMEOUT_S = 2.0


class Head(NamedTuple):
    branch: str  # "" when detached or not a git checkout
    sha: str  # short; "" when not a git checkout


class Source(NamedTuple):
    tree: str
    branch: str
    sha: str
    default_root: str  # the installed checkout; "" when not launched

    @property
    def is_test_build(self) -> bool:
        """Not the installed checkout on the main branch."""
        moved = bool(self.default_root) and self.tree != self.default_root
        return moved or self.branch != MAIN_BRANCH

    @property
    def label(self) -> str:
        """`branch@sha`, the worktree's folder for a detached head."""
        name = self.branch or Path(self.tree).name
        return f"{name}@{self.sha}" if self.sha else name


def package_tree(package_file: str) -> Path:
    """The checkout holding `src/steno/__init__.py`."""
    return Path(package_file).resolve().parent.parent.parent


def git_head(tree: Path, *, timeout: float = GIT_TIMEOUT_S) -> Head:
    """The branch and short commit checked out in `tree`."""
    def run(*args: str) -> str:
        try:
            done = subprocess.run(
                ["git", "-C", str(tree), *args], capture_output=True,
                text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return done.stdout.strip() if done.returncode == 0 else ""

    return Head(run("symbolic-ref", "-q", "--short", "HEAD"),
                run("rev-parse", "--short", "HEAD"))


def describe(tree: Path, head: Head, default_root: str) -> Source:
    root = str(Path(default_root).resolve()) if default_root else ""
    return Source(str(tree.resolve()), head.branch, head.sha, root)


def lease_record(source: Source, *, pid: int, started_at: float) -> dict:
    """What the lease file holds: the source, whose it is, since when."""
    return {
        "tree": source.tree,
        "branch": source.branch,
        "sha": source.sha,
        "default_root": source.default_root,
        "test_build": source.is_test_build,
        "label": source.label,
        "pid": pid,
        "started_at": started_at,
    }


def log_line(source: Source) -> str:
    kind = "test build" if source.is_test_build else "installed"
    return (f"{kind} {source.label} from {source.tree}"
            f" (installed: {source.default_root or 'unknown'})")


def lease_path() -> Path:
    """Beside steno.log, in the state directory, not the runtime one:
    after a crash or a reboot it still says what ran last."""
    return log_path().with_name(LEASE_FILENAME)


def write_lease(path: Path, record: dict) -> None:
    """Best-effort and atomic, like the bar's state file."""
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record) + "\n")
        tmp.replace(path)


def read_lease(path: Path) -> dict:
    """The lease on disk, or `{}` when there is none to read."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def publish(*, pid: int, started_at: float, default_root: str) -> Source:
    """Log and lease the tree this process runs from, and return it.

    For the app's startup only: a CLI run from another tree must not
    take the running app's lease.
    """
    tree = package_tree(__file__)
    source = describe(tree, git_head(tree), default_root)
    logging.getLogger("steno.src").info("%s", log_line(source))
    write_lease(lease_path(),
                lease_record(source, pid=pid, started_at=started_at))
    return source
