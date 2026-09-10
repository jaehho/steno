"""steno CLI: argparse, env loading, preflight, subcommands.

`gui` is the default and does the real work. Everything else here is for the
times you are not in front of the window: re-summarizing an old session over
SSH, or working out why detection did or didn't fire.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import textwrap
import time
from pathlib import Path

from dotenv import load_dotenv

from . import DEFAULT_MODEL, __version__, config_dir, data_dir, migrate_legacy
from .brief import BRIEF_FILENAME, brief_age_days, estimate_tokens, load_brief
from .detect import DEFAULT_ALLOW, END_GRACE_S, START_HOLD_S

ENV_TEMPLATE = """\
# steno config — required keys
DEEPGRAM_API_KEY=
ANTHROPIC_API_KEY=

# Optional model override for the advisor and summaries.
# CLAUDE_MODEL=claude-sonnet-5

# Optional. Drop meeting audio after N days, keeping the text. Unset keeps it.
# STENO_KEEP_AUDIO_DAYS=30
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
    from .gui import run

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
    import json as _json

    from .state import bar_json, read_state

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
    from .control import show

    if not show():
        print("could not start steno", file=sys.stderr)
        sys.exit(1)


def cmd_toggle(_args: argparse.Namespace) -> None:
    """Start or stop recording in the running app."""
    from .control import activate

    if not activate("toggle-record"):
        print("steno is not running", file=sys.stderr)
        sys.exit(1)


def cmd_quit(_args: argparse.Namespace) -> None:
    from .control import activate

    if not activate("quit"):
        print("steno is not running", file=sys.stderr)
        sys.exit(1)


def cmd_watch(args: argparse.Namespace) -> None:
    """Print mic-holding apps so you can tune --allow/--deny against real names."""
    from .detect import list_recording_streams, qualifying

    allow = None if args.any_app else DEFAULT_ALLOW

    async def loop() -> None:
        seen: set[tuple] = set()
        print("watching source-outputs — Ctrl+C to stop\n", flush=True)
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
    import datetime as _dt

    from .session import paused_until

    until = paused_until()
    if until is not None:
        when = _dt.datetime.fromtimestamp(until).strftime("%H:%M:%S")  # noqa: DTZ006
        print(f"paused: until {when} ({(until - time.time()) / 60:.0f} min left)")
    else:
        print("paused: no")
    root = data_dir() / "sessions"
    from .summarize import has_summary, iter_sessions

    sessions = iter_sessions(root)
    unsummarized = [s for s in sessions if not has_summary(s)]
    print(f"sessions: {len(sessions)} in {root}")
    if unsummarized:
        print(f"  {len(unsummarized)} without a summary "
              f"(`steno summarize --all` to catch up)")


def cmd_pause(args: argparse.Namespace) -> None:
    """Pause detection, telling a running app so its bar and header catch up."""
    from .control import activate
    from .session import set_pause

    if args.off:
        set_pause(None)
        activate("unpause")
        print("pause cleared")
        return
    seconds = _parse_duration(args.duration)
    set_pause(seconds)
    activate("pause", [int(seconds // 60)])
    print(f"detection paused for {seconds / 60:.0f} min (`steno pause --off` to clear)")


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
    of the longer line on screen — which is how a title once picked up the end
    of the model name it was overwriting. Pad to cover it.
    """
    prefix = "\r" if previous else ""
    print(prefix + text + " " * max(0, len(previous) - len(text)), flush=True)


def _summarize_one(
    session_dir: Path, model: str, brief: str | None, force: bool = True
) -> bool:
    """Summarize one session, reporting progress. Returns True if it wrote one."""
    from .notes import sync_todos
    from .summarize import has_summary, summarize_session

    if not force and has_summary(session_dir):
        print(f"  {session_dir.name}: already summarized (--force to redo)")
        return False
    progress = _progress_line(f"  {session_dir.name}: summarizing with {model}...")
    try:
        summary = asyncio.run(summarize_session(session_dir, model, brief))
    except Exception as e:  # noqa: BLE001 — one bad session must not abort a batch
        _replace_line(f"  {session_dir.name}: failed — {e}", progress)
        return False
    todos = sync_todos(session_dir, summary.body)
    open_n = sum(1 for t in todos if not t.done)
    _replace_line(f"  {session_dir.name}: {summary.title}", progress)
    print(f"    -> {session_dir / 'summary.md'}"
          + (f"  ·  {open_n} todo{'s' if open_n != 1 else ''}" if open_n else ""))
    return True


def cmd_summarize(args: argparse.Namespace) -> None:
    load_dotenv(config_dir() / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            f"Missing ANTHROPIC_API_KEY. Put it in {config_dir() / '.env'} "
            f"(run `steno init`) or export it.",
            file=sys.stderr,
        )
        sys.exit(1)
    from .summarize import iter_sessions, resolve_session

    model = os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)
    brief = load_brief(_brief_path(args))

    if args.all:
        targets = iter_sessions(args.dir)
        if not targets:
            print("no sessions to summarize")
            return
    else:
        try:
            targets = [resolve_session(args.session, args.dir)]
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)

    written = sum(
        _summarize_one(t, model, brief, force=args.force or not args.all)
        for t in targets
    )
    if args.all:
        print(f"{written}/{len(targets)} summarized")


def cmd_realign(args: argparse.Namespace) -> None:
    """Re-time old sessions against their audio, after saying what it costs."""
    load_dotenv(config_dir() / ".env")
    if not os.environ.get("DEEPGRAM_API_KEY"):
        print(f"Missing DEEPGRAM_API_KEY (see {config_dir() / '.env'})", file=sys.stderr)
        sys.exit(1)
    from .realign import format_plan, has_audio, plan, realign_session
    from .summarize import iter_sessions, resolve_session

    if args.all:
        targets = [s for s in iter_sessions(args.dir) if has_audio(s)]
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

    for job in plans:
        progress = _progress_line(f"  {job.name}: transcribing {job.minutes:.0f} min...")
        try:
            lines = asyncio.run(realign_session(job.session_dir))
        except Exception as e:  # noqa: BLE001 — one bad session must not abort a batch
            _replace_line(f"  {job.name}: failed — {e}", progress)
            continue
        _replace_line(f"  {job.name}: {lines} lines re-timed", progress)


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


def cmd_delete(args: argparse.Namespace) -> None:
    """Move sessions to the trash."""
    from .retention import audio_bytes, delete_session, human_bytes
    from .summarize import load_title, resolve_session

    try:
        session_dir = resolve_session(args.session, args.dir)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    title = load_title(session_dir) or session_dir.name
    size = human_bytes(audio_bytes(session_dir))
    if not args.yes and not _confirm(f"delete “{title}” ({size})? [y/N] "):
        print("nothing done")
        return
    outcome, where = delete_session(session_dir, permanent=args.permanent)
    if outcome == "trashed":
        print(f"moved to trash: {where}")
    elif outcome == "deleted":
        print(f"deleted: {session_dir}")
    else:
        print(f"could not delete {session_dir} "
              f"(try --permanent if the trash is on another filesystem)",
              file=sys.stderr)
        sys.exit(1)


def cmd_clean_echo(args: argparse.Namespace) -> None:
    """Take the speakers back out of the microphone track, after the fact.

    Only worth running on a meeting held on speakers. On headphones the filter
    finds nothing to cancel, says so, and writes nothing.
    """
    from .echo import cancel_file
    from .playback import TRACKS, clean_path
    from .summarize import iter_sessions, resolve_session

    if args.all:
        targets = list(iter_sessions(args.dir))
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
            _replace_line(f"  {session_dir.name}: failed — {e}", progress)
            continue
        if not result.worthwhile:
            _replace_line(
                f"  {session_dir.name}: nothing to cancel "
                f"({result.erle_db:.1f} dB) — recorded on headphones",
                progress,
            )
            continue
        _replace_line(
            f"  {session_dir.name}: echo down {result.erle_db:.1f} dB "
            f"(speakers reach the mic {result.delay_s * 1000:.0f} ms late)",
            progress,
        )
        print(f"    -> {out.name}  (`steno realign {session_dir.name}` to "
              f"re-transcribe from it)")


def cmd_todos(args: argparse.Namespace) -> None:
    """Everything still open, newest meeting first."""
    from .notes import ensure_todos
    from .summarize import iter_sessions, load_title

    any_open = False
    for session_dir in iter_sessions(args.dir):
        todos = [t for t in ensure_todos(session_dir) if not t.done or args.all]
        if not todos:
            continue
        any_open = True
        print(f"\n{load_title(session_dir) or session_dir.name}  ({session_dir.name})")
        for t in todos:
            mark = "x" if t.done else " "
            print(f"  [{mark}] {t.text}   ({t.owner})")
    if not any_open:
        print("nothing open")


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
    if not shutil.which("claude"):
        print(
            "claude CLI not found. Install Claude Code "
            "(https://docs.claude.com/claude-code) to use --new; "
            "it uses your subscription, not the API.",
            file=sys.stderr,
        )
        sys.exit(1)
    prompt = textwrap.dedent(f"""\
        Help me draft a meeting brief at {brief_path}. This becomes standing context
        for a meeting assistant that listens to my conversations, answers questions I
        ask it mid-meeting, and summarizes each meeting afterward.

        Keep it tight. The brief is sent to Claude on every advisor query and every
        summary, so every token has to earn its keep:
        - Aim for 1–3k tokens total. Smaller is better when the meeting is narrow.
        - Distill, do not dump. Do NOT inline whole project files (CLAUDE.md, README,
          etc.). Pull out the specific facts, numbers, decisions, and gotchas.
        - Its main job in summaries is resolving names, jargon, and acronyms that
          speech recognition will mangle — so include the proper nouns I say often.

        A strong brief usually contains:
        - Identity (1–2 sentences): who I am, role, current situation
        - Context (1–2 sentences): what these meetings are, what I want from them
        - Substance: the few load-bearing things — specific numbers, design choices,
          named people and projects, planned next steps, process gotchas.

        Steps:
        1. Inspect cwd (ls, README, CLAUDE.md, TODO.md, key code) to ground yourself.
        2. Ask me a few quick questions: what kind of meetings, who attends, what I want.
        3. Draft a tight, curated brief and write it to {brief_path}.
        4. If that file already exists, show me the diff before overwriting.
    """)
    os.execvp("claude", ["claude", "--model", "opus", prompt])


def _preflight() -> None:
    missing_env = [k for k in ("DEEPGRAM_API_KEY", "ANTHROPIC_API_KEY")
                   if not os.environ.get(k)]
    if missing_env:
        cfg = config_dir() / ".env"
        print(
            f"Missing env: {', '.join(missing_env)}.\n"
            f"Put keys in {cfg} (run `steno init` to create a template) "
            f"or export them in your shell.",
            file=sys.stderr,
        )
        sys.exit(1)
    missing_tools = [t for t in ("pactl", "parec") if not shutil.which(t)]
    if missing_tools:
        print(
            f"Missing tools: {', '.join(missing_tools)}. "
            f"Install pipewire-pulse (or pulseaudio).",
            file=sys.stderr,
        )
        sys.exit(1)


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


def main() -> None:
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

    p_todos = sub.add_parser("todos", help="What you owe, across every meeting")
    p_todos.add_argument("--all", action="store_true", help="Include finished items")
    p_todos.add_argument("--dir", type=Path, default=None, help="Sessions directory")
    p_todos.set_defaults(func=cmd_todos)

    p_watch = sub.add_parser(
        "watch", help="Print apps opening the mic, to tune --allow/--deny"
    )
    p_watch.add_argument("--any-app", action="store_true",
                         help="Mark every uncorked stream as a meeting")
    p_watch.set_defaults(func=cmd_watch)

    p_status = sub.add_parser("status", help="Pause state and unsummarized sessions")
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

    args = parser.parse_args()
    if args.cmd is None:
        # Bare `steno` opens the window; that is what this tool is.
        args = parser.parse_args(["gui", *sys.argv[1:]])
    args.func(args)


if __name__ == "__main__":
    main()
