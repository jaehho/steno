"""Erasing a meeting from every copy, for consent withdrawn.

Deleting a meeting moves it, and never removes it (#39): this device's
copy goes to the trash, the server's to `.trash/` beside the sessions,
and the backups keep it as long as their snapshots last. That guards
against losing a meeting by mistake. It has no answer for the opposite
case: someone who was recorded says no, or takes back a yes, and the
recording has to be gone from every copy.

Erase is that answer, for a meeting already deleted (so its project
pages and asst tasks went the usual way first). It finds every copy
steno can reach, says what it found, and removes each one: its trash
entries, the server's live and trashed copies, and what the server's
own trash bin holds of it. A Nextcloud server keeps whatever WebDAV
deletes (all `rclone purge` can send) in its trash bin, with the old
versions of each file, and by default empties that bin only when space
runs short; so erase removes the meeting's folders and project pages
from that bin as well, through Nextcloud's trash bin endpoint. Asked to,
it also takes the meeting and its project pages out of every restic
snapshot and prunes until none of their data is left.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import urllib.parse
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values

from . import sync
from .projectnotes import session_of

TRASH_STAMP = r"\d{8}-\d{6}"
SESSION_FORMAT = "%Y%m%d-%H%M%S"
# A project page is named for the minute its meeting began (projectnotes
# `page_stem`), its title after that, and a transcript page beside it.
PAGE_STAMP_FORMAT = "%Y-%m-%d-%H%M"
# Nextcloud names a deleted item `<name>.d<unix time of the delete>`.
BIN_SUFFIX = r"\.d\d+"
BIN_PAGE = re.compile(rf"\.md{BIN_SUFFIX}$")
# The backup steno takes its restic settings from when the shell has none.
RESTIC_UNIT_ENV = "STENO_RESTIC_UNIT"
RESTIC_UNIT = "restic-backup.service"


@dataclass(frozen=True)
class TrashEntry:
    """One meeting in the freedesktop trash: its files and its info."""

    files: Path
    info: Path


@dataclass(frozen=True)
class Copies:
    """Every copy of one meeting steno can reach.

    `server` holds rclone paths (the live folder and each `.trash/`
    entry). `bin` is the server's trash bin as an rclone path, "" when
    the server has none steno knows how to reach, and `in_bin` the
    items in it that are this meeting or one of its project pages.
    `backup_patterns` are restic patterns that match the meeting and its
    project pages in a snapshot.
    """

    name: str
    local: Path | None
    trashed: list[TrashEntry] = field(default_factory=list)
    server: list[str] = field(default_factory=list)
    bin: str = ""
    in_bin: list[str] = field(default_factory=list)
    backup_patterns: list[str] = field(default_factory=list)

    @property
    def anywhere(self) -> bool:
        return bool(self.local or self.trashed or self.server or self.in_bin)


# ------------------------------------------------------------ names

def page_stamp(name: str) -> str | None:
    """`2026-11-05-1000` for meeting `20261105-100000`: how its project
    pages begin. None for a name that is not a meeting's."""
    try:
        began = datetime.strptime(name[:15], SESSION_FORMAT)
    except ValueError:
        return None
    return began.strftime(PAGE_STAMP_FORMAT)


def backup_patterns(
    name: str, *, sessions: Path, projects: Path, vault: Path
) -> list[str]:
    """restic patterns for meeting `name`: its folder, and the project
    pages of the minute it began, in a project's notes and in a vault
    folder of its own. Another meeting begun in the same minute matches
    too; `steno erase --dry-run --restic` lists what they find."""
    patterns = [str(sessions / name)]
    if stamp := page_stamp(name):
        patterns += [
            str(projects / "*" / "notes" / "meetings" / f"{stamp}*"),
            str(vault / "*" / "meetings" / f"{stamp}*"),
        ]
    return patterns


def trashed_on_server(entries: list[str], name: str) -> list[str]:
    """The `.trash/` folders sync made for meeting `name`
    (`<name>-<YYYYmmdd-HHMMSS>`), from a listing of `.trash/`."""
    pattern = re.compile(rf"{re.escape(name)}-{TRASH_STAMP}")
    return [e.rstrip("/") for e in entries if pattern.fullmatch(e.rstrip("/"))]


def bin_folders(entries: list[str], name: str) -> list[str]:
    """The trash bin items that are meeting `name`'s folder, live or
    from `.trash/`, from a listing of the bin (folders end in `/`)."""
    pattern = re.compile(
        rf"{re.escape(name)}(-{TRASH_STAMP})?{BIN_SUFFIX}/"
    )
    return [e.rstrip("/") for e in entries if pattern.fullmatch(e)]


def bin_pages(entries: list[str], stamp: str) -> list[str]:
    """The trash bin items that may be a project page of a meeting begun
    at `stamp`: a summary or transcript page of that minute."""
    pattern = re.compile(rf"{re.escape(stamp)}[^/]*\.md{BIN_SUFFIX}")
    return [e for e in entries if pattern.fullmatch(e)]


def trash_bin_url(config: Mapping[str, str]) -> str | None:
    """The WebDAV address of the trash bin of the account an rclone
    remote `config` logs in to, when it is a Nextcloud; None when it is
    not, or when its address does not say whose files it holds."""
    if config.get("type") != "webdav" or config.get("vendor") != "nextcloud":
        return None
    url = config.get("url", "")
    files = re.fullmatch(r"(.*/remote\.php/dav)/files/([^/]+)/?", url)
    if files:
        return f"{files.group(1)}/trashbin/{files.group(2)}/trash/"
    legacy = re.fullmatch(r"(.*/remote\.php)/webdav/?", url)
    user = config.get("user", "")
    if legacy and user:
        quoted = urllib.parse.quote(user, safe="")
        return f"{legacy.group(1)}/dav/trashbin/{quoted}/trash/"
    return None


def remote_name(target_root: str) -> str | None:
    """`nextcloud` for `nextcloud:steno`; None for a local folder or an
    on-the-fly remote."""
    name, colon, _rest = target_root.partition(":")
    if not colon or not name or name.startswith(":") or "/" in name:
        return None
    return name.split(",", 1)[0]


def bin_path(remote: str, url: str) -> str:
    """An rclone path for the trash bin at `url`, logged in as `remote`.
    rclone's nextcloud vendor accepts only a `/dav/files/` address, and
    the bin needs none of what it adds, so it is reached as plain
    WebDAV."""
    quoted = "'" + url.replace("'", "''") + "'"
    return f"{remote},vendor=other,url={quoted}:"


# ------------------------------------------------------------ restic

def restic_commands(patterns: list[str]) -> list[list[str]]:
    """What takes everything `patterns` match out of every snapshot.

    `rewrite --forget` replaces each snapshot that holds any of it with
    one that does not. Its data stays in the repository until a prune,
    and a default prune leaves up to 5% of the repository unused, which
    can be this meeting's transcript and part of its audio;
    `--max-unused 0` repacks until none is.
    """
    excludes = [arg for p in patterns for arg in ("--exclude", p)]
    return [
        ["restic", "rewrite", "--forget", *excludes],
        ["restic", "prune", "--max-unused", "0"],
    ]


def restic_preview_commands(patterns: list[str]) -> list[list[str]]:
    """What says, changing nothing, which snapshots hold it and how much
    of the repository a full prune would repack."""
    return [
        ["restic", "find", "--json", *patterns],
        ["restic", "prune", "--dry-run", "--max-unused", "0"],
    ]


@dataclass(frozen=True)
class BackupHolds:
    """What the backups hold of a meeting, and what taking it out costs.

    `paths` are the distinct paths found, `snapshots` how many snapshots
    hold any, `repack` restic's own `to repack:` figure for a full prune
    as things stand ("" when it gave none).
    """

    paths: list[str]
    snapshots: int
    repack: str


def backup_holds(find_json: str, prune_text: str) -> BackupHolds:
    """Read `restic find --json` and `restic prune --dry-run` output."""
    found = json.loads(find_json or "[]") or []
    paths = sorted({m["path"] for s in found for m in s.get("matches") or []})
    snapshots = sum(1 for s in found if s.get("matches"))
    repack = next(
        (line.split(":", 1)[1].strip() for line in prune_text.splitlines()
         if line.startswith("to repack:")),
        "",
    )
    return BackupHolds(paths, snapshots, repack)


def unit_environment(
    show_text: str, files: Mapping[str, Mapping[str, str | None]]
) -> dict[str, str]:
    """The environment a systemd unit gives its process, from
    `systemctl show -p Environment -p EnvironmentFiles` and the contents
    of each file it names (`files`, by path). File settings win, as in
    systemd. A file that was not read adds nothing."""
    env: dict[str, str] = {}
    for line in show_text.splitlines():
        key, _eq, value = line.partition("=")
        if key == "Environment":
            for word in shlex.split(value):
                name, eq, setting = word.partition("=")
                if eq:
                    env[name] = setting
    for path in unit_files(show_text):
        env.update({k: v for k, v in files.get(path, {}).items()
                    if v is not None})
    return env


def unit_files(show_text: str) -> list[str]:
    """The environment files `systemctl show -p EnvironmentFiles`
    names, in order."""
    return [
        line.partition("=")[2].rsplit(" (", 1)[0].lstrip("-")
        for line in show_text.splitlines()
        if line.startswith("EnvironmentFiles=") and line.partition("=")[2]
    ]


# ------------------------------------------------------------ the shell

def trashinfo_path(info: Path) -> Path | None:
    """The original path a `.trashinfo` file records, if it reads."""
    try:
        text = info.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("Path="):
            return Path(urllib.parse.unquote(line.removeprefix("Path=")))
    return None


def trash_entries(trash: Path, original: Path) -> list[TrashEntry]:
    """Entries in the trash at `trash` that were once at `original`."""
    info_dir = trash / "info"
    if not info_dir.is_dir():
        return []
    return [
        TrashEntry(trash / "files" / info.name.removesuffix(".trashinfo"),
                   info)
        for info in sorted(info_dir.glob("*.trashinfo"))
        if trashinfo_path(info) == original
    ]


def list_names(target: str) -> list[str]:
    """The names directly under an rclone path, folders ending in `/`;
    none if it is absent."""
    try:
        out = sync._rclone(["lsf", target], *sync.LIST_FLAGS)
    except sync.SyncError as exc:
        if exc.code != sync.RCLONE_NOT_FOUND:
            raise
        return []
    return [line for line in out.splitlines() if line]


def server_copies(target_root: str, name: str) -> list[str]:
    """The server's live copy of `name` and every trashed one."""
    live = [f"{target_root}/{name}"] if f"{name}/" in list_names(
        target_root) else []
    trash = f"{target_root}/{sync.TRASH_DIR}"
    return live + [
        f"{trash}/{entry}"
        for entry in trashed_on_server(
            [e for e in list_names(trash) if e.endswith("/")], name)
    ]


def rclone_remote_config(name: str) -> dict[str, str]:
    """An rclone remote's settings, {} when there is no such remote.
    Raises SyncError when rclone cannot say."""
    dump = json.loads(sync._rclone(["config", "dump"]) or "{}")
    config = dump.get(name, {})
    return {k: str(v) for k, v in config.items()}


def server_bin(target_root: str) -> str:
    """The sync server's trash bin as an rclone path, "" when the server
    is not a Nextcloud reached through an rclone remote."""
    name = remote_name(target_root)
    if name is None:
        return ""
    url = trash_bin_url(rclone_remote_config(name))
    return bin_path(name, url) if url else ""


def page_of(item: str, name: str) -> bool:
    """Whether trash bin `item` is a project page steno wrote for
    meeting `name`, by what the page says of itself."""
    try:
        text = sync._rclone(["cat", item], *sync.LIST_FLAGS)
    except sync.SyncError:
        return False
    return session_of(text) == name


def in_server_bin(bin_root: str, name: str) -> list[str]:
    """The items in the trash bin at `bin_root` that are meeting `name`:
    its folders, and the project pages that say they are its."""
    entries = list_names(bin_root)
    found = [f"{bin_root}{item}" for item in bin_folders(entries, name)]
    if stamp := page_stamp(name):
        found += [
            f"{bin_root}{item}" for item in bin_pages(entries, stamp)
            if page_of(f"{bin_root}{item}", name)
        ]
    return found


def find_copies(  # noqa: PLR0913 - every place a meeting can be
    name: str, *, sessions: Path, trash: Path, target_root: str,
    projects: Path, vault: Path,
) -> Copies:
    """Every copy of meeting `name`: here, in the trash at `trash`, on the
    server at `target_root` ("" when sync is off) and in its trash bin,
    and the patterns that find it and its pages in a backup."""
    original = (sessions / name).resolve()
    bin_root = server_bin(target_root) if target_root else ""
    return Copies(
        name=name,
        local=original if original.is_dir() else None,
        trashed=trash_entries(trash, original),
        server=server_copies(target_root, name) if target_root else [],
        bin=bin_root,
        in_bin=in_server_bin(bin_root, name) if bin_root else [],
        backup_patterns=backup_patterns(
            name, sessions=sessions.resolve(),
            projects=projects.expanduser().absolute(),
            vault=vault.expanduser().absolute(),
        ),
    )


def remove_local(copies: Copies) -> None:
    """The folder and its trash entries, outright."""
    if copies.local is not None:
        shutil.rmtree(copies.local)
    for entry in copies.trashed:
        if entry.files.is_dir():
            shutil.rmtree(entry.files)
        else:
            entry.files.unlink(missing_ok=True)
        entry.info.unlink(missing_ok=True)


def remove_from_bin(items: list[str]) -> None:
    """Each trash bin item for good, the old versions it holds with it:
    a page is a file, anything else one of the meeting's folders."""
    for item in items:
        verb = "deletefile" if BIN_PAGE.search(item) else "purge"
        sync._rclone([verb, item], *sync.MOVE_FLAGS)


def remove_on_server(copies: Copies) -> list[str]:
    """Each server copy, then what the trash bin took of them and holds
    of the meeting's pages, all outright. The meeting goes from what
    sync remembers too, so no later round reads its absence as news.
    Returns what was removed from the trash bin."""
    for path in copies.server:
        sync._rclone(["purge", path], *sync.MOVE_FLAGS)
    in_bin = in_server_bin(copies.bin, copies.name) if copies.bin else []
    remove_from_bin(in_bin)
    base = sync.load_base()
    if base.pop(copies.name, None) is not None:
        sync.save_state(base, sync.load_last())
    return in_bin


def restic_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Where restic finds the backups: the shell's own settings when they
    name a repository, else those of the backup's systemd unit
    (`STENO_RESTIC_UNIT`, `restic-backup.service` by default). Empty
    when neither does."""
    if environ.get("RESTIC_REPOSITORY") or environ.get(
        "RESTIC_REPOSITORY_FILE"
    ):
        return dict(environ)
    unit = environ.get(RESTIC_UNIT_ENV) or RESTIC_UNIT
    try:
        done = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "-p", "Environment", "-p", "EnvironmentFiles"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    files = {
        path: dotenv_values(path) for path in unit_files(done.stdout)
        if os.access(path, os.R_OK)
    }
    unit_env = unit_environment(done.stdout, files)
    if not (unit_env.get("RESTIC_REPOSITORY")
            or unit_env.get("RESTIC_REPOSITORY_FILE")):
        return {}
    return {**environ, **unit_env}


def run_restic(commands: list[list[str]], env: Mapping[str, str]) -> None:
    """Run each command with `env`. Raises on the first failure."""
    for command in commands:
        subprocess.run(command, check=True, env=dict(env))


def backups_holding(
    patterns: list[str], env: Mapping[str, str]
) -> BackupHolds:
    """What the backups hold of `patterns`, and what a full prune would
    repack, changing nothing. Raises CalledProcessError if restic
    fails."""
    find, prune = restic_preview_commands(patterns)
    found = subprocess.run(find, check=True, env=dict(env),
                           capture_output=True, text=True)
    pruned = subprocess.run(prune, check=True, env=dict(env),
                            capture_output=True, text=True)
    return backup_holds(found.stdout, pruned.stdout)


@contextlib.contextmanager
def sync_lock(*, wait: bool = False) -> Iterator[None]:
    """Hold sync's lock, so no round runs while the copies go. Without
    `wait`, raises BlockingIOError while a round is running."""
    sync.state_dir().mkdir(parents=True, exist_ok=True)
    with (sync.state_dir() / sync.LOCK_FILENAME).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        yield


# ------------------------------------------------------------ all of it

@dataclass
class Erased:
    """What one erase did. `error` says where it stopped, "" when it
    went through; `backups` is False when it left the backups alone."""

    name: str
    copies: Copies | None = None
    from_bin: list[str] = field(default_factory=list)
    backups: bool = False
    error: str = ""


def erase_everywhere(  # noqa: PLR0913 - every place a meeting can be
    name: str, *, sessions: Path, trash: Path, target_root: str,
    projects: Path, vault: Path, restic_env: Mapping[str, str] | None,
) -> Erased:
    """Remove every copy of meeting `name`, which is no longer in the
    list, and with `restic_env` take it out of the backups too. Waits
    for a sync round that is running. Never raises: `Erased.error` says
    what stopped it, and running it again carries on from there."""
    erased = Erased(name)
    try:
        with sync_lock(wait=True):
            copies = find_copies(
                name, sessions=sessions, trash=trash,
                target_root=target_root, projects=projects, vault=vault,
            )
            erased.copies = copies
            if copies.local is not None:
                erased.error = "it is still in the list"
                return erased
            remove_local(copies)
            erased.from_bin = remove_on_server(copies)
    except (OSError, sync.SyncError) as exc:
        erased.error = f"the server: {exc}"
        return erased
    if restic_env is not None:
        try:
            run_restic(restic_commands(copies.backup_patterns), restic_env)
        except (OSError, subprocess.CalledProcessError) as exc:
            erased.error = f"the backups: {exc}"
            return erased
        erased.backups = True
    return erased
