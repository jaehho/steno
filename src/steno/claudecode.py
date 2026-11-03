"""Headless Claude Code: model calls through `claude -p`, on the subscription.

The provider steno uses unless told otherwise. It runs on the subscription
login rather than an API key, so a key is removed from the child's
environment even when `.env` still sets one. Settings, hooks, and MCP servers
are off: the transcript is other people's speech, and the only tools a call
ever gets are read-only ones scoped to a single directory, with every
project's meeting notes kept out of reach.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

READ_TOOLS = ("Read", "Grep", "Glob")
# An API key or another host in the environment would bill the call
# elsewhere: these never reach the child.
NOT_PASSED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
# One `assistant` event carries a whole message; the default 64 KiB line limit
# is not enough once a tool result is in it.
LINE_LIMIT = 1 << 24
# `<project>/notes/meetings`: steno's copies of earlier meetings, which a
# summary of this one must not read (projectnotes.py).
WITHHELD = ("notes", "meetings")


class ClaudeCodeError(RuntimeError):
    pass


class Reply(NamedTuple):
    """The object a structured call returned, and the model that wrote it:
    the exact one, even when an alias such as `sonnet` was asked for."""
    content: dict
    model: str


def claude_path() -> str | None:
    """`claude`, including its native install dir, which a user service's PATH lacks."""
    local_bin = Path.home() / ".local" / "bin"
    return shutil.which("claude", path=f"{os.environ.get('PATH', '')}:{local_bin}")


def check_ready() -> str | None:
    """What a call would fail on, or None if one can be made now."""
    if claude_path() is None:
        return "`claude` is not on PATH; install Claude Code and log in"
    return None


def child_env(environ: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in environ.items() if k not in NOT_PASSED}


def withheld_dirs(root: Path) -> list[Path]:
    """Each project's meeting notes under `root`, where they are named and
    where a link leads them, so a deny rule covers both."""
    found: list[Path] = []
    with contextlib.suppress(OSError):
        for project in sorted(root.iterdir()):
            folder = project.joinpath(*WITHHELD)
            if folder.is_dir():
                found += dict.fromkeys([folder.absolute(), folder.resolve()])
    return found


def read_rules(root: Path, withheld: Iterable[Path]) -> tuple[str, list[str]]:
    """The allow rule for `root` and the deny rules for `withheld`.

    A leading `//` makes a rule an absolute path. Grep and Glob are
    governed by Read rules, a deny wins over an allow, and anything
    unmatched is refused in -p mode.
    """
    return (
        f"Read(/{root.resolve()}/**)",
        [f"Read(/{folder}/**)" for folder in withheld],
    )


def build_argv(  # noqa: PLR0913 - one call shape: the model config and its scope
    binary: str,
    *,
    model: str,
    effort: str,
    system: str,
    output_format: str,
    read_root: Path | None = None,
    schema: dict | None = None,
) -> list[str]:
    """The flags for one call. With `read_root`, Read/Grep/Glob work inside it only."""
    argv = [
        binary, "-p", "--model", model, "--system-prompt", system,
        "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
        "--output-format", output_format,
    ]
    if effort:
        argv += ["--effort", effort]
    if read_root is None:
        argv += ["--tools", ""]
    else:
        allow, deny = read_rules(read_root, withheld_dirs(read_root))
        argv += ["--tools", ",".join(READ_TOOLS), "--allowedTools", allow]
        if deny:
            argv += ["--disallowedTools", *deny]
    if schema is not None:
        argv += ["--json-schema", json.dumps(schema)]
    if output_format == "stream-json":
        argv += ["--verbose", "--include-partial-messages"]
    return argv


async def _spawn(argv: list[str], cwd: Path | None) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=child_env(dict(os.environ)),
        limit=LINE_LIMIT,
    )


def _require_binary() -> str:
    binary = claude_path()
    if binary is None:
        msg = "`claude` is not on PATH; install Claude Code and log in"
        raise ClaudeCodeError(msg)
    return binary


async def run_structured(  # noqa: PLR0913 - one call shape: prompt + model config
    prompt: str,
    *,
    system: str,
    model: str,
    effort: str,
    schema: dict,
    read_root: Path | None = None,
    timeout: float = 900.0,
) -> Reply:
    """One call that must return an object matching `schema`."""
    argv = build_argv(
        _require_binary(), model=model, effort=effort, system=system,
        output_format="json", read_root=read_root, schema=schema,
    )
    proc = await _spawn(argv, read_root)
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
    except TimeoutError:
        msg = f"claude -p took longer than {timeout:.0f}s"
        raise ClaudeCodeError(msg) from None
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    return parse_structured(out.decode(), err.decode(), asked=model)


def parse_structured(out: str, err: str = "", *, asked: str = "") -> Reply:
    """The object in a `--output-format json` result, and its model."""
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        msg = _tail(err or out) or "claude -p printed nothing"
        raise ClaudeCodeError(msg) from None
    structured = payload.get("structured_output")
    if payload.get("is_error") or not isinstance(structured, dict):
        reason = payload.get("result") or payload.get("subtype") or _tail(err)
        msg = f"claude -p returned no result: {str(reason)[:200]}"
        raise ClaudeCodeError(msg)
    return Reply(structured, model_used(payload, asked))


def model_used(payload: dict, asked: str) -> str:
    """The model that wrote most of a result, by `modelUsage`; `asked`
    when the result does not say."""
    usage = payload.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return asked
    return max(usage, key=lambda name: (usage[name] or {}).get("outputTokens", 0))


async def stream_text(  # noqa: PLR0913 - one call shape: prompt + model config
    prompt: str,
    *,
    system: str,
    model: str,
    effort: str,
    on_delta: Callable[[str], None],
    timeout: float = 180.0,
) -> str:
    """One tool-less call, handing each piece of text to `on_delta` as it arrives."""
    argv = build_argv(
        _require_binary(), model=model, effort=effort, system=system,
        output_format="stream-json",
    )
    proc = await _spawn(argv, None)
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    try:
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        async with asyncio.timeout(timeout):
            final: str | None = None
            async for raw in proc.stdout:
                kind, text = parse_stream_line(raw.decode())
                if kind == "delta":
                    on_delta(text)
                elif kind == "result":
                    final = text
                elif kind == "error":
                    raise ClaudeCodeError(text)
            await proc.wait()
        if final is None:
            err = (await proc.stderr.read()).decode()
            msg = _tail(err) or f"claude -p exited {proc.returncode}"
            raise ClaudeCodeError(msg)
    except TimeoutError:
        msg = f"claude -p took longer than {timeout:.0f}s"
        raise ClaudeCodeError(msg) from None
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    return final


def parse_stream_line(line: str) -> tuple[str, str]:
    """Classify one stream-json line as ("delta"|"result"|"error"|"", text)."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return "", ""
    if event.get("type") == "stream_event":
        delta = event.get("event", {}).get("delta", {})
        if delta.get("type") == "text_delta":
            return "delta", delta.get("text", "")
    elif event.get("type") == "result":
        if event.get("is_error"):
            return "error", str(event.get("result") or event.get("subtype"))[:200]
        return "result", event.get("result") or ""
    return "", ""


def _tail(text: str, n: int = 300) -> str:
    return text.strip()[-n:]
