"""Headless Claude Code: every model call steno makes goes through `claude -p`.

It runs on the subscription login rather than an API key, so the key is removed
from the child's environment even when `.env` still sets one. Settings, hooks,
and MCP servers are off: the transcript is other people's speech, and the only
tools a call ever gets are read-only ones scoped to a single directory.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

READ_TOOLS = ("Read", "Grep", "Glob")
# One `assistant` event carries a whole message; the default 64 KiB line limit
# is not enough once a tool result is in it.
LINE_LIMIT = 1 << 24


class ClaudeError(RuntimeError):
    pass


def claude_path() -> str | None:
    """`claude`, including its native install dir, which a user service's PATH lacks."""
    local_bin = Path.home() / ".local" / "bin"
    return shutil.which("claude", path=f"{os.environ.get('PATH', '')}:{local_bin}")


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def build_argv(
    binary: str,
    model: str,
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
    if read_root is None:
        argv += ["--tools", ""]
    else:
        # A leading `//` makes the rule an absolute path. Grep and Glob are
        # governed by Read rules, and anything unmatched is denied in -p mode.
        argv += ["--tools", ",".join(READ_TOOLS),
                 "--allowedTools", f"Read(/{read_root.resolve()}/**)"]
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
        env=child_env(),
        limit=LINE_LIMIT,
    )


def _require_binary() -> str:
    binary = claude_path()
    if binary is None:
        raise ClaudeError("`claude` is not on PATH; install Claude Code and log in")
    return binary


async def run_structured(
    prompt: str,
    *,
    system: str,
    model: str,
    schema: dict,
    read_root: Path | None = None,
    timeout: float = 900.0,
) -> dict:
    """One call that must return an object matching `schema`. Raises ClaudeError."""
    argv = build_argv(_require_binary(), model, system, "json", read_root, schema)
    proc = await _spawn(argv, read_root)
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
    except TimeoutError:
        raise ClaudeError(f"claude -p took longer than {timeout:.0f}s") from None
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    return parse_structured(out.decode(), err.decode())


def parse_structured(out: str, err: str = "") -> dict:
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        raise ClaudeError(_tail(err or out) or "claude -p printed nothing") from None
    structured = payload.get("structured_output")
    if payload.get("is_error") or not isinstance(structured, dict):
        reason = payload.get("result") or payload.get("subtype") or _tail(err)
        raise ClaudeError(f"claude -p returned no result: {str(reason)[:200]}")
    return structured


async def stream_text(
    prompt: str,
    *,
    system: str,
    model: str,
    on_delta: Callable[[str], None],
    timeout: float = 180.0,
) -> str:
    """One tool-less call, handing each piece of text to `on_delta` as it arrives."""
    argv = build_argv(_require_binary(), model, system, "stream-json")
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
                    raise ClaudeError(text)
            await proc.wait()
        if final is None:
            err = (await proc.stderr.read()).decode()
            raise ClaudeError(_tail(err) or f"claude -p exited {proc.returncode}")
        return final
    except TimeoutError:
        raise ClaudeError(f"claude -p took longer than {timeout:.0f}s") from None
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()


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
