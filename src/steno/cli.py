"""steno CLI: argparse, env loading, preflight, subcommands.

`gui` is the default and does the real work. Everything else here is for the
times you are not in front of the window: re-summarizing an old session over
SSH, or working out why detection did or didn't fire.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as _dt
import json as _json
import logging
import math
import os
import shutil
import subprocess
import sys
import textwrap
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

from . import (
    __version__,
    config_dir,
    data_dir,
    migrate_legacy,
    model_default,
    projectnotes,
)
from .brief import BRIEF_FILENAME, brief_age_days, estimate_tokens, load_brief
from .claude import check_ready
from .control import activate, show, toggle_record
from .cost import SUMMARY_GUESS, format_usd
from .detect import (
    DEFAULT_ALLOW,
    END_GRACE_S,
    START_HOLD_S,
    list_recording_streams,
    qualifying,
)
from .echo import cancel_file
from .export import meeting_markdown
from .notes import (
    Dropped,
    Restored,
    drop_open_todos,
    forget_asst,
    pull_todos,
    restore_dropped_todos,
    sync_todos,
)
from .playback import TRACKS, clean_path
from .realign import format_plan, has_audio, plan, realign_session
from .retention import audio_bytes, delete_session, human_bytes
from .session import paused_until, set_pause
from .state import bar_json, read_state
from .summarize import (
    has_summary,
    is_archived,
    iter_sessions,
    load_meta,
    load_project,
    load_title,
    needs_citations,
    projects_root,
    resolve_session,
    set_archived,
    summarize_session,
    transcript_path,
)

ENV_TEMPLATE = """\
# steno config - required keys
DEEPGRAM_API_KEY=

# Model calls are one OpenAI-style chat-completions POST. Flip these whenever
# the best model moves; nothing else needs touching.
#   STENO_MODEL=mimo-v2.6-flash          (default)
#   STENO_MODEL=mimo-v2.6-pro[1m]
#   STENO_ASK_MODEL=mimo-v2.6-pro[1m]    (the Ask box only; summaries keep STENO_MODEL)
#   STENO_PROVIDER=mimo|openrouter       (default: mimo)
#   STENO_BASE_URL=https://.../v1/chat/completions   (one-off endpoint override)
# Key: STENO_API_KEY, else the provider's file below (same as `claude-open`).
#   mimo       -> ~/.config/mimo.env       MIMO_API_KEY
#   openrouter -> ~/.config/openrouter.env OPENROUTER_API_KEY

# Optional. Where your projects live, for tying a meeting to its codebase.
# STENO_PROJECTS_DIR=~/projects

# Optional. Drop meeting audio after N days, keeping the text. Unset keeps it.
# STENO_KEEP_AUDIO_DAYS=30

# Optional. 0 hides the tray icon, for a bar already running `steno bar`.
# STENO_TRAY=0

# Optional. 0 stops steno copying a meeting's summary and transcript into
# its project's notes/meetings folder. On by default.
# STENO_PROJECT_NOTES=0

# Optional. Any value stops steno running `asst`: no to-do is filed, ticked
# or removed in the task app. For scratch runs; "0" does not undo it.
# STENO_NO_ASST=1
"""


def _split_csv(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


def cmd_init(_args: argparse.Namespace) -> None:
    cfg = config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    env_path = cfg / ".env"
    if env_path.exists():
        print(f"already exists: {env_path}")
    else:
        env_path.write_text(ENV_TEMPLATE)
        print(f"wrote template:  {env_path}")
    sessions = data_dir() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    print(f"sessions dir:    {sessions}")
    print(f"brief:           {cfg / BRIEF_FILENAME}  (standing context; optional)")


def cmd_gui(args: argparse.Namespace) -> None:
    load_dotenv(config_dir() / ".env")
    _preflight()
    from .gui import run  # noqa: PLC0415  # GTK is heavy; only gui needs it

    allow = None if args.any_app else (_split_csv(args.allow) or DEFAULT_ALLOW)
    sys.exit(run(
        root=args.dir,
        background=args.background,
        allow=allow,
        deny=_split_csv(args.deny),
        brief_path=args.brief,
        summarize=not args.no_summary,
        start_hold_s=args.start_hold,
        end_grace_s=args.end_grace,
    ))


def cmd_bar(args: argparse.Namespace) -> None:
    """Feed a waybar custom module: one JSON object per line, on change.

    Deliberately dumb and cheap. It reads a state file the app publishes, so it
    starts instantly, needs no API keys, and prints a truthful `off` when the
    app is not running rather than the last thing it saw.
    """

    last = None
    while True:
        if args.once:
            print(_json.dumps(bar_json(read_state())), flush=True)
            return
        current = bar_json(read_state())
        if current != last:
            print(_json.dumps(current), flush=True)
            last = current
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return


def cmd_show(_args: argparse.Namespace) -> None:
    """Raise the window, starting the app if it isn't running."""

    if not show():
        print("could not start steno", file=sys.stderr)
        sys.exit(1)


def cmd_toggle(_args: argparse.Namespace) -> None:
    """Start or stop recording, starting the listener if it is down."""

    if not toggle_record():
        _alert(
            "Steno is not recording",
            "Could not start or reach the listener. "
            "`steno gui` in a terminal says why.",
        )
        sys.exit(1)


def cmd_quit(_args: argparse.Namespace) -> None:

    if not activate("quit"):
        print("steno is not running", file=sys.stderr)
        sys.exit(1)


def cmd_autostart(args: argparse.Namespace) -> None:
    from . import autostart  # noqa: PLC0415  # only the autostart subcommand needs it

    compositor = autostart.started_by_compositor()
    if args.state == "on":
        autostart.enable()
    elif args.state == "off":
        autostart.disable()
        if compositor is not None:
            print(f"still started by {compositor}; remove that line to stop it")
    enabled = autostart.is_enabled()
    print(f"start at login: {'on' if enabled else 'off'}")
    if enabled and compositor is None and not autostart.session_reads_autostart():
        print("this session may not run autostart entries; start "
              f"`{autostart.listener_command()}` from its config")


def cmd_watch(args: argparse.Namespace) -> None:
    """Print mic-holding apps so you can tune --allow/--deny against real names."""

    allow = None if args.any_app else DEFAULT_ALLOW

    async def loop() -> None:
        seen: set[tuple] = set()
        print("watching source-outputs - Ctrl+C to stop\n", flush=True)
        while True:
            streams = await list_recording_streams()
            live = {s.index for s in qualifying(streams, allow)}
            now = {(s.index, s.app_name, s.binary, s.corked) for s in streams}
            for index, app, binary, corked in sorted(now - seen):
                flags = []
                if corked:
                    flags.append("corked")
                if index in live:
                    flags.append("MEETING")
                suffix = f"  [{', '.join(flags)}]" if flags else ""
                print(f"  + #{index} {app or '?'} (binary={binary or '?'}){suffix}",
                      flush=True)
            for index, app, _b, _c in sorted(seen - now):
                print(f"  - #{index} {app or '?'}", flush=True)
            seen = now
            await asyncio.sleep(1)

    try:
        asyncio.run(loop())
    except KeyboardInterrupt:
        print("\nstopped")


def cmd_status(_args: argparse.Namespace) -> None:

    until = paused_until()
    if until is not None:
        when = _dt.datetime.fromtimestamp(until).strftime("%H:%M:%S")
        print(f"paused: until {when} ({(until - time.time()) / 60:.0f} min left)")
    else:
        print("paused: no")
    root = data_dir() / "sessions"

    sessions = iter_sessions(root)
    archived = [s for s in sessions if is_archived(s)]
    # `summarize --all` leaves archived meetings alone, so they are not
    # what it would catch up.
    unsummarized = [
        s for s in sessions if s not in archived and not has_summary(s)
    ]
    print(f"sessions: {len(sessions)} in {root}")
    if archived:
        print(f"  {len(archived)} archived "
              "(`steno unarchive NAME` brings one back)")
    if unsummarized:
        print(f"  {len(unsummarized)} without a summary "
              f"(`steno summarize --all` to catch up)")


def cmd_pause(args: argparse.Namespace) -> None:
    """Pause detection, telling a running app so its bar and header catch up."""
    if args.off:
        set_pause(None)
        activate("unpause")
        print("pause cleared")
        return
    # Whole minutes, rounded up: the running app's pause action takes minutes,
    # and 0 of them means "resume".
    minutes = max(1, math.ceil(_parse_duration(args.duration) / 60))
    set_pause(minutes * 60)
    activate("pause", [minutes])
    print(f"detection paused for {minutes} min (`steno pause --off` to clear)")


def _parse_duration(text: str) -> float:
    """Parse 30s / 45m / 2h / a bare number of minutes."""
    t = text.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    if t and t[-1] in units:
        try:
            return float(t[:-1]) * units[t[-1]]
        except ValueError:
            pass
    else:
        try:
            return float(t) * 60
        except ValueError:
            pass
    print(f"bad duration: {text} (try 30m, 2h, 90s)", file=sys.stderr)
    sys.exit(1)


def _progress_line(text: str) -> str:
    """Show an in-place progress line, returning what a replacement must cover.

    Only a terminal can overwrite; into a pipe or a log `\r` is just a
    character, so there the progress line is skipped rather than left stranded
    mid-line in the output.
    """
    if not sys.stdout.isatty():
        return ""
    print(text, end="", flush=True)
    return text


def _replace_line(text: str, previous: str) -> None:
    """Overwrite the progress line `previous` with `text`.

    A bare `\r` only moves the cursor, so a shorter replacement leaves the tail
    of the longer line on screen - which is how a title once picked up the end
    of the model name it was overwriting. Pad to cover it.
    """
    prefix = "\r" if previous else ""
    print(prefix + text + " " * max(0, len(previous) - len(text)), flush=True)


def _leave_out_archived(sessions: list[Path]) -> list[Path]:
    """The sessions a bulk run (`--all`, `--uncited`) goes through.

    An archived meeting is one the user put away, so a run over everything
    leaves it alone, and says how many it left. A command given a meeting's
    name does not come through here: naming one is asking for it.
    """
    kept = [s for s in sessions if not is_archived(s)]
    if left := len(sessions) - len(kept):
        print(f"{left} archived meeting{'s' if left != 1 else ''} left out "
              "(`steno unarchive NAME` to include)")
    return kept


def _summarize_one(
    session_dir: Path, model: str, brief: str | None, force: bool = True
) -> bool | None:
    """Summarize one session, reporting progress.

    True if it wrote one, False if it tried and failed, None if it was
    already summarized and left alone.
    """
    if not force and has_summary(session_dir):
        print(f"  {session_dir.name}: already summarized (--force to redo)")
        return None
    progress = _progress_line(f"  {session_dir.name}: summarizing with {model}...")
    try:
        summary = asyncio.run(summarize_session(session_dir, model, brief))
    except Exception as e:
        _replace_line(f"  {session_dir.name}: failed - {e}", progress)
        return False
    todos = sync_todos(session_dir, summary.body)
    open_n = sum(1 for t in todos if t.open)
    project = f"  ({summary.project})" if summary.project else ""
    _replace_line(f"  {session_dir.name}: {summary.title}{project}", progress)
    print(f"    -> {session_dir / 'summary.md'}"
          + (f"  ·  {open_n} todo{'s' if open_n != 1 else ''}" if open_n else ""))
    _notes_after(session_dir)
    return True


def _summarize_uncited(
    args: argparse.Namespace, model: str, brief: str | None
) -> None:
    """Redo the summaries that have no citations, after saying what it costs."""
    targets = _leave_out_archived(
        [s for s in iter_sessions(args.dir) if needs_citations(s)]
    )
    if not targets:
        print("every summary already has citations")
        return
    noun = "summary" if len(targets) == 1 else "summaries"
    print(f"{len(targets)} {noun} without citations, newest first:")
    for target in targets:
        minutes = load_meta(target).get("duration_min")
        print(f"  {target.name}  {load_title(target) or '(untitled)'}"
              + (f"  ({minutes:.0f} min)" if isinstance(minutes, (int, float)) else ""))
    # One flat guess per summary: a long meeting costs more than a short one.
    print(f"about {format_usd(len(targets) * SUMMARY_GUESS)} to write "
          f"{'it' if len(targets) == 1 else 'them'} again. Notes, ticked "
          "to-dos and projects are kept.")
    if not args.yes:
        print("nothing done; pass --yes to go ahead")
        return
    results = [_summarize_one(t, model, brief) for t in targets]
    print(f"{results.count(True)}/{len(targets)} summarized")
    if False in results:
        sys.exit(1)


def cmd_summarize(args: argparse.Namespace) -> None:
    load_dotenv(config_dir() / ".env")

    if args.uncited and (args.all or args.session):
        print("--uncited chooses the sessions itself: no name, no --all",
              file=sys.stderr)
        sys.exit(1)
    if err := check_ready():
        print(f"{err} -- summaries run through the chat-completions endpoint.", file=sys.stderr)
        sys.exit(1)

    model = model_default()
    brief = load_brief(_brief_path(args))

    if args.uncited:
        _summarize_uncited(args, model, brief)
        return
    if args.all:
        targets = _leave_out_archived(iter_sessions(args.dir))
        if not targets:
            print("no sessions to summarize")
            return
    else:
        try:
            targets = [resolve_session(args.session, args.dir)]
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)

    results = [
        _summarize_one(t, model, brief, force=args.force or not args.all)
        for t in targets
    ]
    if args.all:
        print(f"{results.count(True)}/{len(targets)} summarized")
    if False in results:
        sys.exit(1)


def cmd_realign(args: argparse.Namespace) -> None:
    """Re-time old sessions against their audio, after saying what it costs."""
    load_dotenv(config_dir() / ".env")
    if not os.environ.get("DEEPGRAM_API_KEY"):
        print(f"Missing DEEPGRAM_API_KEY (see {config_dir() / '.env'})", file=sys.stderr)
        sys.exit(1)
    if args.all:
        targets = _leave_out_archived(
            [s for s in iter_sessions(args.dir) if has_audio(s)]
        )
    else:
        try:
            targets = [resolve_session(args.session, args.dir)]
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
    if not args.force:
        targets = [t for t in targets if not plan(t).already_aligned]
    if not targets:
        print("nothing to re-time (--force to redo aligned sessions)")
        return

    plans = [plan(t) for t in targets]
    print(f"re-timing {len(plans)} session{'s' if len(plans) != 1 else ''}:")
    print(format_plan(plans))
    if args.dry_run:
        return
    if not args.yes and not _confirm("spend that? [y/N] "):
        print("nothing done")
        return

    failed = 0
    for job in plans:
        progress = _progress_line(f"  {job.name}: transcribing {job.minutes:.0f} min...")
        try:
            lines = asyncio.run(realign_session(job.session_dir))
        except Exception as e:
            _replace_line(f"  {job.name}: failed - {e}", progress)
            failed += 1
            continue
        _replace_line(f"  {job.name}: {lines} lines re-timed", progress)
        if projectnotes.read_record(job.session_dir) is not None:
            _notes_after(job.session_dir)
    if failed:
        sys.exit(1)


def _confirm(prompt: str) -> bool:
    """Ask before spending money. A pipe is not a yes."""
    if not sys.stdin.isatty():
        print("not a terminal; pass --yes to go ahead", file=sys.stderr)
        return False
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _recording_session(root: Path | None) -> Path | None:
    """The meeting the listener is recording right now, if it is."""
    newest = iter_sessions(root)
    if read_state().get("status") == "recording" and newest:
        return newest[0]
    return None


def _being_recorded(session_dir: Path, root: Path | None) -> bool:
    """Is this the meeting the listener is recording right now?"""
    recording = _recording_session(root)
    return (recording is not None
            and recording.resolve() == session_dir.resolve())


def cmd_delete(args: argparse.Namespace) -> None:
    """Move sessions to the trash."""
    load_dotenv(config_dir() / ".env")
    try:
        session_dir = resolve_session(args.session, args.dir)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if _being_recorded(session_dir, args.dir):
        # Bare `steno delete` means "the most recent", which is the meeting
        # being recorded right now; the engine would keep writing into the trash.
        print(f"{session_dir.name} is being recorded; stop it first", file=sys.stderr)
        sys.exit(1)
    title = load_title(session_dir) or session_dir.name
    size = human_bytes(audio_bytes(session_dir))
    if not args.yes and not _confirm(f"delete '{title}' ({size})? [y/N] "):
        print("nothing done")
        return
    # Its copy in a project goes first: taking it out needs the meta.json
    # that says where it went, which goes with the meeting, and a copy
    # that cannot be taken out must not be left behind with nothing to
    # say it is there. A transcript page is other people's speech.
    gone = _reporting(projectnotes.try_remove, session_dir)
    if gone.error:
        print(f"could not take it out of {gone.project}'s notes: {gone.error}\n"
              f"{session_dir.name} was not deleted; make that folder "
              "writable and run it again", file=sys.stderr)
        sys.exit(1)
    if told := _to_report(gone.changes, set()):
        print(f"taken out of {gone.project}'s notes:")
        _print_changes(told, projects_root())
    # Same as the window's delete: tasks filed from this meeting go with it.
    freed = forget_asst(session_dir)
    if freed:
        print(f"removed {freed} task{'s' if freed != 1 else ''} from asst")
    outcome, where = delete_session(session_dir, permanent=args.permanent)
    if outcome == "failed" and gone.altered:
        _reporting(projectnotes.try_write, session_dir)  # still here
    if outcome == "trashed":
        print(f"moved to trash: {where}")
    elif outcome == "deleted":
        print(f"deleted: {session_dir}")
    else:
        print(f"could not delete {session_dir} "
              f"(try --permanent if the trash is on another filesystem)",
              file=sys.stderr)
        sys.exit(1)


def _archive_each(args: argparse.Namespace, archive: bool) -> None:
    """Archive or unarchive each named meeting; one that cannot be, says so."""
    done = "archived" if archive else "unarchived"
    failed = False
    for spec in args.session:
        try:
            session_dir = resolve_session(spec, args.dir)
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            failed = True
            continue
        # Any directory resolves by path, and archiving writes into it.
        if not transcript_path(session_dir).is_file():
            print(f"not a meeting, no transcript: {spec}", file=sys.stderr)
            failed = True
            continue
        if archive and _being_recorded(session_dir, args.dir):
            print(f"{session_dir.name} is being recorded; stop it first",
                  file=sys.stderr)
            failed = True
            continue
        title = load_title(session_dir) or session_dir.name
        changed = set_archived(session_dir, archive)
        print(f"{done if changed else 'already ' + done}: "
              f"{title}  ({session_dir.name})")
        # Also when it already was: that retries what asst refused.
        if archive:
            failed |= _say_dropped(drop_open_todos(session_dir))
        else:
            failed |= _say_restored(restore_dropped_todos(session_dir))
    if failed:
        sys.exit(1)


def _open_tasks(n: int) -> str:
    return f"{n} open task{'s' if n != 1 else ''}"


def _say_dropped(dropped: Dropped) -> bool:
    """What archiving did to the meeting's to-dos. True if asst refused
    some, which the exit status says."""
    if dropped.removed:
        print(f"  removed {_open_tasks(dropped.removed)} from asst")
    if dropped.stuck:
        print(f"  could not remove {_open_tasks(dropped.stuck)} from asst; "
              "archive it again to retry", file=sys.stderr)
    return bool(dropped.stuck)


def _say_restored(restored: Restored) -> bool:
    """What bringing the meeting back did to its to-dos. True if asst
    refused some."""
    if restored.filed:
        print(f"  filed {_open_tasks(restored.filed)} in asst again")
    if restored.failed:
        print(f"  asst did not take {_open_tasks(restored.failed)}; "
              "they are open here only", file=sys.stderr)
    return bool(restored.failed)


def cmd_archive(args: argparse.Namespace) -> None:
    """Put meetings away: out of the list and out of every --all, their
    open to-dos closed (and the tasks removed from asst)."""
    _archive_each(args, archive=True)


def cmd_unarchive(args: argparse.Namespace) -> None:
    """Bring archived meetings back, and their to-dos with them."""
    _archive_each(args, archive=False)


def _reporting(call, session_dir: Path, **kwargs) -> projectnotes.Result:
    """Run `projectnotes.try_write` or `try_remove`, whose report the
    caller prints.

    The module logs a warning for each file it leaves alone, which is
    for the window, where nothing else says so. Here the caller says it
    on stdout, and a project's index in the way would be said once for
    each of its meetings.
    """
    log = logging.getLogger(projectnotes.__name__)
    level = log.level
    log.setLevel(logging.ERROR)
    try:
        return call(session_dir, **kwargs)
    finally:
        log.setLevel(level)


def _shown(path: Path, root: Path) -> str:
    """A path inside the projects directory as `project/notes/...`."""
    for base in (root, root.resolve()):
        with contextlib.suppress(ValueError):
            return str(path.relative_to(base))
    return str(path)


def _to_report(
    changes: list[projectnotes.Change],
    seen: set[tuple[Path, str]],
) -> list[projectnotes.Change]:
    """The changes worth a line, and not told before in this run.

    A file that was already right is not. A project's index and
    `.gitignore` are touched by each of its meetings: told once, as
    made if the first meeting made it, so that a dry run, which makes
    nothing, says what the real run does.
    """
    fresh = []
    for change in changes:
        key = (change.path, change.action)
        if change.action == "same" or key in seen:
            continue
        if change.action == "update" and (change.path, "create") in seen:
            continue
        seen.add(key)
        fresh.append(change)
    return fresh


def _print_changes(
    changes: list[projectnotes.Change], root: Path, indent: str = "  "
) -> None:
    for change in changes:
        note = f"  ({change.note})" if change.note else ""
        print(f"{indent}{change.action:<7} {_shown(change.path, root)}{note}")


def _notes_after(session_dir: Path) -> None:
    """What a command that changed a meeting does for its project: write
    its pages again, and say so in a line. A meeting with no project, or
    project notes switched off, says nothing."""
    result = _reporting(projectnotes.try_write, session_dir)
    root = projects_root()
    if result.error:
        print(f"    project notes: {result.error}", file=sys.stderr)
        return
    for change in result.skipped:
        print(f"    project notes: left {_shown(change.path, root)} as it is "
              f"({change.note})", file=sys.stderr)
    if altered := result.altered:
        many = "s" if len(altered) != 1 else ""
        print(f"    -> {result.project}/notes/meetings: "
              f"{len(altered)} file{many} changed")


def _for_project_notes(
    sessions: list[Path], project: str | None
) -> list[Path]:
    """The meetings a bulk `steno project-notes` goes through: those
    filed under a project (under `project`, when given) and those with
    pages in one that it would take away."""
    kept = []
    for session_dir in sessions:
        filed = load_project(session_dir) or None
        if project is not None and filed != project:
            continue
        if filed or projectnotes.read_record(session_dir) is not None:
            kept.append(session_dir)
    return kept


def _tally(counts: Counter[str], dry_run: bool) -> str:
    """`2 created, 1 updated`, or with a dry run `2 to create`."""
    forms = (("create", "created", "to create"),
             ("update", "updated", "to update"),
             ("remove", "removed", "to remove"),
             ("skip", "left alone", "left alone"))
    parts = [f"{counts[act]} {dry if dry_run else done}"
             for act, done, dry in forms if counts[act]]
    return ", ".join(parts) or "nothing to change"


def _notes_targets(args: argparse.Namespace) -> list[Path]:
    """The meetings `steno project-notes` writes, or an exit."""
    if args.all and args.session:
        print("name a meeting or pass --all, not both", file=sys.stderr)
        sys.exit(1)
    if len(args.session) > 1:
        print("name one meeting, or pass --all", file=sys.stderr)
        sys.exit(1)
    if not (args.force or projectnotes.enabled()):
        print(f"{projectnotes.SWITCH_ENV}="
              f"{os.environ[projectnotes.SWITCH_ENV]!r}: project notes are "
              "off (--force writes them anyway)", file=sys.stderr)
        sys.exit(1)
    recording = _recording_session(args.dir)
    if not args.all:
        return [_named_for_notes(args, recording)]
    targets = _for_project_notes(
        _leave_out_archived(iter_sessions(args.dir)), args.project
    )
    return [t for t in targets if t != recording]


def _say_written(
    session_dir: Path,
    result: projectnotes.Result,
    root: Path,
    named: bool,
    seen: set[tuple[Path, str]],
) -> tuple[str, list[projectnotes.Change]]:
    """Print what writing a meeting's pages did. Returns how it went,
    `error`, `unfiled` (nothing to write, which fails a meeting that
    was asked for by name), `changed` or `current`, and the changes
    that were told."""
    title = load_title(session_dir) or "(untitled)"
    head = f"{session_dir.name}  {title}  [{result.project or '-'}]"
    if result.error:
        print(f"{head}\n  could not write: {result.error}", file=sys.stderr)
        return "error", []
    if result.note or (named and result.project is None):
        print(f"{head}\n  {result.note or 'not filed under a project'}")
        return "unfiled", []
    if told := _to_report(result.changes, seen):
        print(head)
        _print_changes(told, root)
        return "changed", told
    if named:
        print(f"{head}: up to date")
    return "current", []


def cmd_project_notes(args: argparse.Namespace) -> None:
    """Keep meetings' summaries and transcripts in their projects'
    notes/meetings, or with `--remove` take them out again."""
    load_dotenv(config_dir() / ".env")
    if args.remove:
        _remove_notes(args)
    else:
        _write_notes(args)


def _write_notes(args: argparse.Namespace) -> None:
    """Copy meetings' summaries and transcripts into their projects'
    notes/meetings. `--dry-run` lists what it would do and writes
    nothing; read that before a run over everything."""
    targets = _notes_targets(args)
    root = projects_root()
    counts: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    seen: set[tuple[Path, str]] = set()
    for session_dir in targets:
        result = _reporting(
            projectnotes.try_write, session_dir,
            dry_run=args.dry_run, force=args.force,
        )
        outcome, told = _say_written(
            session_dir, result, root, not args.all, seen
        )
        counts.update(c.action for c in told)
        outcomes[outcome] += 1
    if args.dry_run:
        print("dry run, nothing written: ", end="")
    if args.all:
        print(f"{len(targets)} meetings looked at, {outcomes['current']} "
              f"already up to date; {_tally(counts, args.dry_run)}")
    elif args.dry_run:
        print(_tally(counts, args.dry_run))
    if outcomes["error"] or (not args.all and outcomes["unfiled"]):
        sys.exit(1)


def _named_for_notes(args: argparse.Namespace, recording: Path | None) -> Path:
    """The one meeting `steno project-notes [NAME]` is about, or an exit."""
    name = args.session[0] if args.session else None
    try:
        session_dir = resolve_session(name, args.dir)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not transcript_path(session_dir).is_file():
        print(f"not a meeting, no transcript: {name}", file=sys.stderr)
        sys.exit(1)
    if session_dir == recording:
        print(f"{session_dir.name} is being recorded; stop it first",
              file=sys.stderr)
        sys.exit(1)
    filed = load_project(session_dir) or None
    if args.project is not None and filed != args.project:
        print(f"{session_dir.name} is filed under {filed or 'no project'}, "
              f"not {args.project}; nothing done", file=sys.stderr)
        sys.exit(1)
    return session_dir


def _remove_notes(args: argparse.Namespace) -> None:
    """Take meetings' pages out of their projects. Works with the app
    stopped, and only removes what steno wrote."""
    if not args.session:
        print("--remove needs the names of the meetings", file=sys.stderr)
        sys.exit(1)
    if args.all or args.project is not None or args.force:
        print("--remove takes meeting names only", file=sys.stderr)
        sys.exit(1)
    root = projects_root()
    seen: set[tuple[Path, str]] = set()
    failed = False
    for spec in args.session:
        try:
            session_dir = resolve_session(spec, args.dir)
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            failed = True
            continue
        result = _reporting(
            projectnotes.try_remove, session_dir, dry_run=args.dry_run
        )
        if result.error:
            print(f"{session_dir.name}: could not remove: {result.error}",
                  file=sys.stderr)
            failed = True
        elif result.project is None:
            print(f"{session_dir.name}: {result.note}")
        else:
            print(f"{session_dir.name}  [{result.project}]"
                  + ("  (dry run)" if args.dry_run else ""))
            _print_changes(_to_report(result.changes, seen), root)
    if failed:
        sys.exit(1)


def cmd_clean_echo(args: argparse.Namespace) -> None:
    """Take the speakers back out of the microphone track, after the fact.

    Only worth running on a meeting held on speakers. On headphones the filter
    finds nothing to cancel, says so, and writes nothing.
    """
    if args.all:
        targets = _leave_out_archived(iter_sessions(args.dir))
    else:
        try:
            targets = [resolve_session(args.session, args.dir)]
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)

    for session_dir in targets:
        mic = session_dir / TRACKS["you"]
        ref = session_dir / TRACKS["them"]
        out = clean_path(mic)
        if not (mic.is_file() and ref.is_file()):
            print(f"  {session_dir.name}: no audio")
            continue
        if out.is_file() and not args.force:
            print(f"  {session_dir.name}: already cleaned (--force to redo)")
            continue
        progress = _progress_line(f"  {session_dir.name}: cancelling echo...")
        try:
            result = cancel_file(mic, ref, out, suppress=not args.no_suppress)
        except (OSError, ValueError, ImportError) as e:
            _replace_line(f"  {session_dir.name}: failed - {e}", progress)
            continue
        if not result.worthwhile:
            _replace_line(
                f"  {session_dir.name}: nothing to cancel "
                f"({result.erle_db:.1f} dB) - recorded on headphones",
                progress,
            )
            continue
        _replace_line(
            f"  {session_dir.name}: echo down {result.erle_db:.1f} dB "
            f"(speakers reach the mic {result.delay_s * 1000:.0f} ms late)",
            progress,
        )
        if result.muted_s:
            minutes = result.seconds / 60.0 if result.seconds else 0.0
            share = 100.0 * result.muted_s / result.seconds if result.seconds else 0.0
            print(f"    muted {result.muted_s:.0f}s of far-dominant residual "
                  f"({share:.0f}% of {minutes:.1f} min)")
        print(f"    -> {out.name}  (`steno realign {session_dir.name}` to "
              f"re-transcribe from it)")


def cmd_todos(args: argparse.Namespace) -> None:
    """Everything still open, newest meeting first.

    A meeting put away is left out, and says so: its to-dos were closed
    with it. A row closed that way is never listed, `--all` or not; that
    flag adds the ticked ones.
    """
    any_open = False
    for session_dir in _leave_out_archived(iter_sessions(args.dir)):
        todos = [
            t for t in pull_todos(session_dir)
            if t.open or (args.all and t.done)
        ]
        if not todos:
            continue
        any_open = True
        print(f"\n{load_title(session_dir) or session_dir.name}  ({session_dir.name})")
        for t in todos:
            mark = "x" if t.done else " "
            print(f"  [{mark}] {t.text}   ({t.owner})")
    if not any_open:
        print("nothing open")


def cmd_export(args: argparse.Namespace) -> None:
    """Print or save a meeting as markdown."""
    try:
        session_dir = resolve_session(args.session, args.dir)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    md = meeting_markdown(session_dir)
    if args.output is None:
        print(md, end="")
        return
    try:
        args.output.write_text(md)
    except OSError as e:
        print(f"could not write {args.output}: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"wrote {args.output}")


def _brief_path(args: argparse.Namespace) -> Path:
    """Explicit flag, else the config-dir brief, else one in the cwd."""
    if getattr(args, "brief", None) is not None:
        return args.brief
    configured = config_dir() / BRIEF_FILENAME
    if configured.is_file():
        return configured
    return Path.cwd() / BRIEF_FILENAME


def cmd_brief(args: argparse.Namespace) -> None:
    brief_path = _brief_path(args)
    if args.new:
        _brief_new(brief_path)
    else:
        _brief_show(brief_path)


def _brief_show(brief_path: Path) -> None:
    if not brief_path.is_file():
        print(f"no brief at {brief_path}", file=sys.stderr)
        print("create one with: steno brief --new", file=sys.stderr)
        sys.exit(1)
    text = load_brief(brief_path)
    if text is None:
        print(f"brief is empty: {brief_path}")
        return
    tok = estimate_tokens(text)
    age = brief_age_days(brief_path)
    age_s = "today" if age == 0 else f"{age}d old" if age is not None else "?"
    print(f"== {brief_path} ==")
    print(f"chars: {len(text):,} · ~tokens: {tok:,} · {age_s}")
    print()
    print(text)


def _brief_new(brief_path: Path) -> None:
    # An interactive session, so it stays a claude process -- routed through
    # the claude-open wrapper when it is on PATH, else the subscription.
    local_bin = f"{Path.home() / '.local/bin'}:{os.environ.get('PATH', '')}"
    binary = shutil.which("claude-open", path=local_bin) or shutil.which("claude")
    if binary is None:
        print(
            "neither claude-open nor claude found. Install Claude Code "
            "(https://docs.claude.com/claude-code) to use --new; "
            "claude-open runs it on z.ai or OpenRouter, plain claude on the subscription.",
            file=sys.stderr,
        )
        sys.exit(1)
    prompt = textwrap.dedent(f"""\
        Help me draft a meeting brief at {brief_path}. This becomes standing context
        for a meeting assistant that listens to my conversations, answers questions I
        ask it mid-meeting, and summarizes each meeting afterward.

        Keep it tight. The brief is sent to Claude on every advisor query and every
        summary, so every token has to earn its keep:
        - Aim for 1-3k tokens total. Smaller is better when the meeting is narrow.
        - Distill, do not dump. Do NOT inline whole project files (CLAUDE.md, README,
          etc.). Pull out the specific facts, numbers, decisions, and gotchas.
        - Its main job in summaries is resolving names, jargon, and acronyms that
          speech recognition will mangle - so include the proper nouns I say often.

        A strong brief usually contains:
        - Identity (1-2 sentences): who I am, role, current situation
        - Context (1-2 sentences): what these meetings are, what I want from them
        - Substance: the few load-bearing things - specific numbers, design choices,
          named people and projects, planned next steps, process gotchas.

        Steps:
        1. Inspect cwd (ls, README, CLAUDE.md, TODO.md, key code) to ground yourself.
        2. Ask me a few quick questions: what kind of meetings, who attends, what I want.
        3. Draft a tight, curated brief and write it to {brief_path}.
        4. If that file already exists, show me the diff before overwriting.
    """)
    os.execvp(binary, [binary, "--model", "opus", prompt])


def _alert(title: str, message: str) -> None:
    """Say it where the person will see it.

    From rofi, autostart, a bar click or a keybind there is no terminal, so
    stderr alone is a silent no-op and the thing they asked for never happens.
    """
    print(message, file=sys.stderr)
    if not sys.stderr.isatty() and shutil.which("notify-send"):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["notify-send", "-a", "Steno", title, message],
                timeout=5, check=False,
            )


def _cannot_start(message: str) -> None:
    """Say why the app will not start, and stop."""
    _alert("Steno cannot start", message)
    sys.exit(1)


def _preflight() -> None:
    missing_env = [k for k in ("DEEPGRAM_API_KEY",) if not os.environ.get(k)]
    if missing_env:
        cfg = config_dir() / ".env"
        _cannot_start(
            f"Missing env: {', '.join(missing_env)}.\n"
            f"Put keys in {cfg} (run `steno init` to create a template) "
            f"or export them in your shell."
        )
    missing_tools = [t for t in ("pactl", "parec") if not shutil.which(t)]
    if missing_tools:
        _cannot_start(
            f"Missing tools: {', '.join(missing_tools)}. "
            f"Install pipewire-pulse (or pulseaudio)."
        )

    if err := check_ready():
        _cannot_start(
            f"{err} -- summaries and questions run through the chat-completions endpoint."
        )


# Options that take a value, so `--allow pause` is not read as `pause`.
_VALUE_FLAGS = {"--brief", "--allow", "--deny", "--dir", "--start-hold", "--end-grace"}


def _names_a_command(argv: list[str], commands: set[str]) -> bool:
    """Is the first positional word a subcommand? Option values are skipped."""
    skip = False
    for token in argv:
        if skip:
            skip = False
        elif token in _VALUE_FLAGS:
            skip = True
        elif not token.startswith("-"):
            return token in commands
    return False


def _add_detection_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow", default=None,
        help="Comma-separated app names/binaries that count as meetings",
    )
    parser.add_argument(
        "--deny", default=None,
        help="Comma-separated app names/binaries to ignore",
    )
    parser.add_argument(
        "--any-app", action="store_true",
        help="Treat any app holding the mic as a meeting (ignores --allow)",
    )
    parser.add_argument(
        "--no-summary", action="store_true", help="Record only; skip the summary"
    )
    parser.add_argument(
        "--dir", type=Path, default=None,
        help="Sessions directory (default: XDG data dir)",
    )
    parser.add_argument(
        "--start-hold", type=float, default=START_HOLD_S,
        help=f"Seconds of continuous capture before a meeting starts "
             f"(default {START_HOLD_S:.0f})",
    )
    parser.add_argument(
        "--end-grace", type=float, default=END_GRACE_S,
        help=f"Seconds after the mic closes before a meeting ends "
             f"(default {END_GRACE_S:.0f})",
    )


def main() -> None:  # noqa: PLR0915 - one argparse tree, every subcommand declared inline
    parser = argparse.ArgumentParser(
        prog="steno",
        description="Steno: listens, keeps up, and turns calls into notes.",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--brief", type=Path, default=None,
        help="Path to brief.md (default: the one in your config dir)",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_gui = sub.add_parser("gui", help="The window (default)")
    p_gui.add_argument(
        "--background", action="store_true",
        help="Start listening without opening a window (for autostart)",
    )
    _add_detection_flags(p_gui)
    # The same flag as the top level's, for `steno --brief x --any-app`, which
    # reaches here as `gui --brief x --any-app`. SUPPRESS: only set when given,
    # so it cannot overwrite a top-level value with None.
    p_gui.add_argument("--brief", type=Path, default=argparse.SUPPRESS,
                       help="Path to brief.md (default: the one in your config dir)")
    p_gui.set_defaults(func=cmd_gui)

    p_bar = sub.add_parser("bar", help="Status line for waybar (JSON per change)")
    p_bar.add_argument("--interval", type=float, default=1.0,
                       help="Seconds between state checks (default 1)")
    p_bar.add_argument("--once", action="store_true",
                       help="Print the current state and exit")
    p_bar.set_defaults(func=cmd_bar)

    p_show = sub.add_parser("show", help="Raise the window, starting it if needed")
    p_show.set_defaults(func=cmd_show)

    p_toggle = sub.add_parser("toggle", help="Start or stop recording")
    p_toggle.set_defaults(func=cmd_toggle)

    p_quit = sub.add_parser("quit", help="Stop the running app (and its listening)")
    p_quit.set_defaults(func=cmd_quit)

    p_auto = sub.add_parser("autostart", help="Start listening at login (per user)")
    p_auto.add_argument("state", nargs="?", choices=("on", "off"),
                        help="Turn it on or off; no argument shows the setting")
    p_auto.set_defaults(func=cmd_autostart)

    p_init = sub.add_parser("init", help="Create config dir and .env template")
    p_init.set_defaults(func=cmd_init)

    p_brief = sub.add_parser(
        "brief", help="Show the brief, or scaffold a new one via Claude Code"
    )
    p_brief.add_argument("--new", action="store_true",
                         help="Scaffold a tight curated brief.md via Claude Code")
    p_brief.set_defaults(func=cmd_brief)

    p_sum = sub.add_parser(
        "summarize", help="Summarize a session (default: the most recent)"
    )
    p_sum.add_argument("session", nargs="?", default=None,
                       help="Session name or path; default: most recent")
    p_sum.add_argument("--all", action="store_true", help="Summarize every session")
    p_sum.add_argument("--force", action="store_true",
                       help="Re-summarize even if summary.md exists")
    p_sum.add_argument("--uncited", action="store_true",
                       help="Redo every summary written before citations: "
                            "lists them and the cost, and waits for --yes")
    p_sum.add_argument("--yes", action="store_true",
                       help="With --uncited: do it; without, only list and price")
    p_sum.add_argument("--dir", type=Path, default=None, help="Sessions directory")
    p_sum.set_defaults(func=cmd_summarize)

    p_realign = sub.add_parser(
        "realign", help="Re-time a session against its audio (costs a pass)"
    )
    p_realign.add_argument("session", nargs="?", default=None,
                           help="Session name or path; default: most recent")
    p_realign.add_argument("--all", action="store_true", help="Every session")
    p_realign.add_argument("--force", action="store_true",
                           help="Redo sessions that are already aligned")
    p_realign.add_argument("--dry-run", action="store_true",
                           help="Show what it would cost and stop")
    p_realign.add_argument("--yes", action="store_true", help="Skip the prompt")
    p_realign.add_argument("--dir", type=Path, default=None, help="Sessions directory")
    p_realign.set_defaults(func=cmd_realign)

    p_clean = sub.add_parser(
        "clean-echo",
        help="Subtract the speakers from a session's microphone track",
    )
    p_clean.add_argument("session", nargs="?", default=None,
                         help="Session name or path; default: most recent")
    p_clean.add_argument("--all", action="store_true", help="Every session")
    p_clean.add_argument("--force", action="store_true",
                         help="Redo sessions that already have a cleaned track")
    p_clean.add_argument("--no-suppress", action="store_true",
                         help="Linear cancellation only, no residual suppression")
    p_clean.add_argument("--dir", type=Path, default=None, help="Sessions directory")
    p_clean.set_defaults(func=cmd_clean_echo)

    p_delete = sub.add_parser("delete", help="Move a session to the trash")
    p_delete.add_argument("session", nargs="?", default=None,
                          help="Session name or path; default: most recent")
    p_delete.add_argument("--permanent", action="store_true",
                          help="Delete outright instead of trashing it")
    p_delete.add_argument("--yes", action="store_true", help="Skip the prompt")
    p_delete.add_argument("--dir", type=Path, default=None, help="Sessions directory")
    p_delete.set_defaults(func=cmd_delete)

    p_archive = sub.add_parser(
        "archive",
        help="Put meetings away: out of the list and every --all, kept "
             "whole, their open to-dos closed",
    )
    p_archive.add_argument("session", nargs="+",
                           help="Session names or paths")
    p_archive.add_argument("--dir", type=Path, default=None,
                           help="Sessions directory")
    p_archive.set_defaults(func=cmd_archive)

    p_unarchive = sub.add_parser(
        "unarchive", help="Bring archived meetings back, to-dos reopened"
    )
    p_unarchive.add_argument("session", nargs="+",
                             help="Session names or paths")
    p_unarchive.add_argument("--dir", type=Path, default=None,
                             help="Sessions directory")
    p_unarchive.set_defaults(func=cmd_unarchive)

    p_notes = sub.add_parser(
        "project-notes",
        help="Keep a meeting's summary and transcript in its project",
        description="Copy a meeting's summary and transcript into "
                    "notes/meetings of the project it is filed under, "
                    "and bring them up to date. Nothing leaves this "
                    "machine. With --remove, take them out again.",
    )
    p_notes.add_argument("session", nargs="*", metavar="NAME",
                         help="Session name or path; default: most "
                              "recent. With --remove, one or more")
    p_notes.add_argument("--all", action="store_true",
                         help="Every meeting filed under a project "
                              "(not the archived ones)")
    p_notes.add_argument("--project", default=None, metavar="P",
                         help="Only meetings filed under project P")
    p_notes.add_argument("--dry-run", action="store_true",
                         help="List what it would change and change "
                              "nothing")
    p_notes.add_argument("--force", action="store_true",
                         help="Write even with STENO_PROJECT_NOTES=0")
    p_notes.add_argument("--remove", action="store_true",
                         help="Take the named meetings' pages out of "
                              "their projects")
    p_notes.add_argument("--dir", type=Path, default=None,
                         help="Sessions directory")
    p_notes.set_defaults(func=cmd_project_notes)

    p_todos = sub.add_parser("todos", help="What you owe, across every meeting")
    p_todos.add_argument("--all", action="store_true", help="Include finished items")
    p_todos.add_argument("--dir", type=Path, default=None, help="Sessions directory")
    p_todos.set_defaults(func=cmd_todos)

    p_export = sub.add_parser("export", help="Print or save a meeting as markdown")
    p_export.add_argument("session", nargs="?", default=None,
                          help="Session name or path; default: most recent")
    p_export.add_argument("-o", "--output", type=Path, default=None,
                          help="Write to this file instead of stdout")
    p_export.add_argument("--dir", type=Path, default=None, help="Sessions directory")
    p_export.set_defaults(func=cmd_export)

    p_watch = sub.add_parser(
        "watch", help="Print apps opening the mic, to tune --allow/--deny"
    )
    p_watch.add_argument("--any-app", action="store_true",
                         help="Mark every uncorked stream as a meeting")
    p_watch.set_defaults(func=cmd_watch)

    p_status = sub.add_parser(
        "status", help="Pause state, archived and unsummarized sessions"
    )
    p_status.set_defaults(func=cmd_status)

    p_pause = sub.add_parser("pause", help="Suspend meeting detection for a while")
    p_pause.add_argument("duration", nargs="?", default="60m",
                         help="e.g. 90s, 45m, 2h (default 60m)")
    p_pause.add_argument("--off", action="store_true", help="Clear an active pause")
    p_pause.set_defaults(func=cmd_pause)

    # Two `is_dir()` calls, before anything can look at the wrong directory and
    # conclude the archive is empty.
    for old, new in migrate_legacy():
        print(f"moved {old} -> {new}")

    argv = sys.argv[1:]
    asked_for_help = {"-h", "--help", "--version"}.intersection(argv)
    if not _names_a_command(argv, set(sub.choices)) and not asked_for_help:
        # Bare `steno` opens the window; that is what this tool is, so the
        # detection flags (`steno --allow zoom`) mean the window's.
        argv = ["gui", *argv]
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
