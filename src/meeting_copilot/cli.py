"""copilot CLI: argparse, env loading, preflight, subcommands."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import textwrap
from pathlib import Path

from dotenv import load_dotenv

from . import __version__, config_dir, data_dir
from .brief import BRIEF_FILENAME, brief_age_days, estimate_tokens, load_brief

ENV_TEMPLATE = """\
# meeting-copilot config — required keys
DEEPGRAM_API_KEY=
ANTHROPIC_API_KEY=

# Optional model overrides (1M context tier is enabled automatically via beta header)
#   CLAUDE_MODEL       — Ctrl+G (manual) advisor; default claude-opus-4-7
#   CLAUDE_MODEL_AUTO  — auto-triggered (question detected); default claude-sonnet-4-6
# CLAUDE_MODEL=claude-opus-4-7
# CLAUDE_MODEL_AUTO=claude-sonnet-4-6
"""


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


def cmd_run(args: argparse.Namespace) -> None:
    load_dotenv(config_dir() / ".env")
    _preflight()
    from .app import CopilotApp
    CopilotApp(brief_path=args.brief).run()


def cmd_history(args: argparse.Namespace) -> None:
    try:
        from .history import HistoryApp
    except (ImportError, OSError) as e:
        print(
            f"history browser unavailable: {e}\n"
            "Install portaudio (e.g. `pacman -S portaudio`) and reinstall the tool.",
            file=sys.stderr,
        )
        sys.exit(1)
    HistoryApp(root=args.dir).run()


def cmd_brief(args: argparse.Namespace) -> None:
    brief_path = args.brief if args.brief is not None else Path.cwd() / BRIEF_FILENAME
    if args.new:
        _brief_new(brief_path)
    else:
        _brief_show(brief_path)


def _brief_show(brief_path: Path) -> None:
    if not brief_path.is_file():
        print(f"no brief at {brief_path}", file=sys.stderr)
        print("create one with: copilot brief --new", file=sys.stderr)
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
        for a live meeting copilot that listens to my conversation and suggests responses.

        Keep it tight. The brief is sent to Claude on EVERY advisor query (manual Ctrl+G
        AND auto-triggered question responses), so every token has to earn its keep:
        - Aim for 1–3k tokens total. Smaller is better when the meeting is narrow.
        - Distill, do not dump. Do NOT inline whole project files (CLAUDE.md, README, etc.).
          Pull out the specific facts, numbers, decisions, and gotchas that matter
          for THIS meeting and put them in the brief itself.
        - Write for the specific audience (interviewer / customer / manager / collaborator).
          What would they want the copilot to ground every answer in?

        A strong brief usually contains:
        - Identity (1–2 sentences): who I am, role, current situation
        - Context (1–2 sentences): what this meeting is, what I want from it
        - Substance: the few load-bearing things — specific numbers, design choices,
          transferable contributions, planned next steps, process gotchas, whatever
          actually matters here. Tables and headers welcome if they help.

        Steps:
        1. Inspect cwd (ls, README, CLAUDE.md, TODO.md, key code) to ground yourself.
        2. Ask me a few quick questions: what kind of meeting, who attends, what I want.
        3. Draft a tight, curated brief and write it to {brief_path}.
        4. If that file already exists, show me the diff before overwriting.
    """)
    os.execvp("claude", ["claude", "--model", "opus", prompt])


def _preflight() -> None:
    missing_env = [k for k in ("DEEPGRAM_API_KEY", "ANTHROPIC_API_KEY") if not os.environ.get(k)]
    if missing_env:
        cfg = config_dir() / ".env"
        print(
            f"Missing env: {', '.join(missing_env)}.\n"
            f"Put keys in {cfg} (run `copilot init` to create a template) "
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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="copilot",
        description="Live meeting copilot: dual audio capture, Deepgram, Claude advisor TUI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--brief",
        type=Path,
        default=None,
        help="Path to brief.md (default: ./brief.md in current directory)",
    )
    sub = parser.add_subparsers(dest="cmd")
    p_init = sub.add_parser("init", help="Create config dir and .env template")
    p_init.set_defaults(func=cmd_init)
    p_brief = sub.add_parser(
        "brief",
        help="Show the brief (size, age) or scaffold a new one via Claude Code",
    )
    p_brief.add_argument(
        "--new",
        action="store_true",
        help="Scaffold a tight curated brief.md interactively via Claude Code",
    )
    p_brief.set_defaults(func=cmd_brief)
    p_history = sub.add_parser(
        "history",
        help="Browse past sessions: transcript + click-to-seek audio playback",
    )
    p_history.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Sessions directory (default: XDG data dir)",
    )
    p_history.set_defaults(func=cmd_history)

    args = parser.parse_args()
    if args.cmd is None:
        cmd_run(args)
    else:
        args.func(args)
