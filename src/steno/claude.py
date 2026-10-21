"""One OpenAI-style chat-completions POST per model call.

Endpoint, model, and key are config, not code, because the best model for a
meeting summary changes often. Flip `STENO_MODEL` for the model;
`STENO_PROVIDER` (or `STENO_BASE_URL`) for where it is hosted. Defaults are
MiMo on the token plan, using the same `~/.config/mimo.env` key as
`claude-open`. That host also speaks Anthropic Messages (`…/anthropic`) for
Claude Code; this client uses its OpenAI route, which takes the same
json_schema, tools, and SSE we already build.

The only tools a call ever gets are three read-only ones scoped to a single
directory: the transcript is other people's speech, and grounding must not
read the rest of the machine.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path

import httpx

# name -> how to reach a chat-completions endpoint and who to bill.
# `base_url` may be a callable when it is derived from the key file (MiMo
# hosts the OpenAI route beside its Anthropic one, and the plan picks the host).
PROVIDERS: dict[str, dict] = {
    "mimo": {
        "key_file": "~/.config/mimo.env",
        "key_name": "MIMO_API_KEY",
        "model": "mimo-v2.6-flash",
        "presets": ("mimo-v2.6-pro[1m]",),
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "key_file": "~/.config/openrouter.env",
        "key_name": "OPENROUTER_API_KEY",
        "model": "@preset/cc-flash-med",
    },
}
DEFAULT_PROVIDER = "mimo"
MAX_TOOL_ROUNDS = 8
_RETRY_STATUSES = {408, 429, 500, 502, 503, 504, 522, 524}
HTTP_OK = 200
MAX_TOOL_HITS = 60  # cap hits returned per tool call

_transport: httpx.AsyncBaseTransport | None = None   # tests inject a MockTransport

log = logging.getLogger("steno.claude")


class ClaudeError(RuntimeError):
    pass


class _StreamDeadError(Exception):
    """A retryable stream failure: dropped connection, or no [DONE] at the end."""


def _read_kv(path: Path, name: str) -> str:
    try:
        for line in path.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _expand(path: str) -> Path:
    return Path(path).expanduser()


def provider() -> str:
    return os.environ.get("STENO_PROVIDER") or DEFAULT_PROVIDER


def _settings() -> dict:
    name = provider()
    if name not in PROVIDERS:
        msg = f"unknown STENO_PROVIDER={name!r}; known: {', '.join(sorted(PROVIDERS))}"
        raise ClaudeError(msg)
    return PROVIDERS[name]


def _key_file() -> Path:
    override = os.environ.get("STENO_API_KEY_FILE")
    return _expand(override) if override else _expand(_settings()["key_file"])


def _key_name() -> str:
    return os.environ.get("STENO_API_KEY_NAME") or _settings()["key_name"]


def _mimo_base_url() -> str:
    """MiMo's OpenAI route, derived from the Anthropic host in mimo.env.

    `MIMO_BASE_URL` is what Claude Code points at (`…/anthropic`). The
    chat-completions path is the same origin without that suffix.
    """
    raw = (_read_kv(_key_file(), "MIMO_BASE_URL") or os.environ.get("MIMO_BASE_URL")
           or "https://token-plan-sgp.xiaomimimo.com/anthropic")
    root = raw.rstrip("/").removesuffix("/anthropic")
    return root + "/v1/chat/completions"


def base_url() -> str:
    """The chat-completions endpoint. STENO_BASE_URL wins, for one-off experiments."""
    if url := os.environ.get("STENO_BASE_URL"):
        return url.rstrip("/")
    if provider() == "mimo":
        return _mimo_base_url()
    return _settings()["base_url"]


def default_model() -> str:
    return _settings()["model"]


def presets() -> list[str]:
    """Models worth picking by hand on the active provider, default first."""
    settings = _settings()
    return list(dict.fromkeys([settings["model"], *settings.get("presets", ())]))


def api_key() -> str:
    if key := os.environ.get("STENO_API_KEY", "").strip():
        return key
    return _read_kv(_key_file(), _key_name())


def check_ready() -> str | None:
    """What a call would fail on, or None if one can be made now."""
    if api_key():
        return None
    return f"no {_key_name()} in {_key_file()}"


def _content(message: dict) -> str:
    """The text of one assistant message: a string or a list of typed parts.

    Reasoning models carry their thinking in `reasoning`/`reasoning_content`;
    it is never part of the answer.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") == "text")
    return ""


def _strict(schema: dict) -> dict:
    """OpenAI strict mode: a `type: [T, "null"]` union becomes anyOf."""
    out: dict = {}
    for k, v in schema.items():
        if isinstance(v, dict):
            out[k] = _strict(v)
        elif isinstance(v, list):
            out[k] = [_strict(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    types = out.get("type")
    if isinstance(types, list):
        out = {k: v for k, v in out.items() if k != "type"}
        out["anyOf"] = [{"type": t} for t in types]
    return out


def _payload(model: str, messages: list[dict], *, schema: dict | None = None,
             tools: list[dict] | None = None, stream: bool = False) -> dict:
    payload: dict = {"model": model, "messages": messages}
    if stream:
        payload["stream"] = True
    if schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "steno", "strict": True, "schema": _strict(schema)},
        }
    if tools:
        payload["tools"] = tools
    return payload


def _error_text(status: int, body: str) -> str:
    try:
        err = json.loads(body).get("error")
        if err:
            return str(err.get("message") or err)[:200]
    except ValueError:
        pass
    return f"HTTP {status}: {body[:200]}"


def _backoff(attempt: int, resp: httpx.Response | None) -> float:
    if resp is not None:
        with contextlib.suppress(TypeError, ValueError):
            return min(60.0, float(resp.headers.get("retry-after", "")))
    return min(30.0, 5.0 * 4.0 ** (attempt - 1))


async def _retry_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _call(client: httpx.AsyncClient, payload: dict) -> dict:
    """One POST. Retried: 429/5xx, a dropped connection, a truncated body, and
    a 200 with no content -- a provider that dies mid-stream is not covered by
    failover, so empty is just another retryable failure. Other 4xx raise."""
    headers = {"Authorization": f"Bearer {api_key()}"}
    reason, resp = "no response", None
    for attempt in range(3):
        if attempt:
            log.info("model retry %s after %s", attempt, reason)
            await _retry_sleep(_backoff(attempt, resp))
        try:
            resp = await client.post(base_url(), json=payload, headers=headers)
        except httpx.TransportError as e:
            reason, resp = str(e), None
            log.warning("model transport error: %s", e)
            continue
        if resp.status_code in _RETRY_STATUSES:
            reason = f"HTTP {resp.status_code}"
            continue
        if resp.status_code != HTTP_OK:
            log.warning("model HTTP %s", resp.status_code)
            raise ClaudeError(_error_text(resp.status_code, resp.text))
        try:
            data = resp.json()
        except ValueError:
            reason = "truncated response body"
            continue
        err = data.get("error")
        message = (data.get("choices") or [{}])[0].get("message") or {}
        if err:
            reason = str(err.get("message") or err)
        elif message.get("tool_calls") or _content(message):
            log.info("model ok attempts=%s", attempt + 1)
            return data
        else:
            reason = "empty response"
    log.warning("model failed: %s", reason)
    msg = f"OpenRouter: {reason}, after 3 attempts"
    raise ClaudeError(msg)


async def run_structured(  # noqa: PLR0913 - one call shape: prompt + model config
    prompt: str,
    *,
    system: str,
    model: str,
    schema: dict,
    read_root: Path | None = None,
    timeout: float = 900.0,
) -> dict:
    """One call that must return an object matching `schema`. Raises ClaudeError."""
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    tools = _tools(read_root) if read_root is not None else None
    async with httpx.AsyncClient(transport=_transport,
                                 timeout=httpx.Timeout(timeout, connect=15.0)) as client:
        for _ in range(MAX_TOOL_ROUNDS):
            data = await _call(client, _payload(model, messages, schema=schema, tools=tools))
            message = data["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not calls or read_root is None:
                # No tools were offered, so none can come back.
                return parse_structured(_content(message))
            messages.append({"role": "assistant", "content": _content(message),
                             "tool_calls": calls})
            messages += [_tool_result(read_root, call) for call in calls]
        # Over the tool budget: one forced answer with the tools withdrawn.
        data = await _call(client, _payload(model, messages, schema=schema))
        return parse_structured(_content(data["choices"][0]["message"]))


def parse_structured(text: str) -> dict:
    """The model's JSON, fences stripped; anything else raises."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        if body.endswith("```"):
            body = body[: -3].rstrip()
    try:
        out = json.loads(body)
    except json.JSONDecodeError:
        raise ClaudeError(_tail(text) or "the model returned nothing") from None
    if not isinstance(out, dict):
        msg = f"the model returned a {type(out).__name__}, not an object"
        raise ClaudeError(msg)
    return out


def _tail(text: str, n: int = 300) -> str:
    return text.strip()[-n:]


# --- the provider's model list ---------------------------------------------

def models_url(chat_url: str) -> str:
    """The OpenAI-style model list that sits beside a chat endpoint.

    Raises ValueError for an endpoint that is not `…/chat/completions`:
    there is no telling where such a host keeps its list.
    """
    root = chat_url.rstrip("/")
    if not root.endswith("/chat/completions"):
        msg = f"{chat_url} is not a chat-completions endpoint"
        raise ValueError(msg)
    return root.removesuffix("/chat/completions") + "/models"


# MiMo lists its speech models beside the chat ones (`mimo-v2.5-tts`,
# `mimo-v2.5-asr`); the list has no field that says which is which.
_NOT_CHAT = re.compile(r"(?:^|[-_/.:])(?:tts|asr)(?:$|[-_/.:])")


def parse_models(body: object) -> list[str]:
    """The chat models in a `/models` reply, sorted and without repeats.

    OpenAI-style hosts answer `{"data": [{"id": "..."}, ...]}`. Anything
    else lists nothing.
    """
    rows = body.get("data") if isinstance(body, dict) else None
    ids = {row["id"].strip() for row in rows or []
           if isinstance(row, dict) and isinstance(row.get("id"), str)}
    return sorted(i for i in ids if i and not _NOT_CHAT.search(i))


async def list_models(timeout: float = 8.0) -> list[str]:
    """The active provider's models. Raises ClaudeError if it will not say.

    One try and a short timeout: this fills a menu, and a menu can wait
    for the next open rather than for a retry.
    """
    try:
        url = models_url(base_url())
        async with httpx.AsyncClient(transport=_transport,
                                     timeout=httpx.Timeout(timeout)) as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {api_key()}"})
    except (ValueError, httpx.HTTPError) as e:
        raise ClaudeError(str(e)) from None
    if resp.status_code != HTTP_OK:
        raise ClaudeError(_error_text(resp.status_code, resp.text))
    try:
        return parse_models(resp.json())
    except ValueError:
        msg = "the model list was not JSON"
        raise ClaudeError(msg) from None


# --- grounding tools ---------------------------------------------------------

_PRUNE = {".git", "node_modules", ".venv", "__pycache__"}

# `<project>/notes/meetings` is where steno keeps its copies of earlier
# meetings (projectnotes.py): other people's speech as heard, not
# scrubbed. A summary is of one meeting, so the tools never show the model
# another, and nothing in it is sent on.
_WITHHELD = ("notes", "meetings")


def _withheld(top: Path, path: Path) -> bool:
    """True for a resolved `path` in a project's meeting notes.

    Those are where `notes/meetings` leads, not only where it is named:
    steno follows a link to a directory inside the project (`notes ->
    docs`) and writes there, so the pages are at `docs/meetings`.
    """
    try:
        parts = path.relative_to(top).parts
    except ValueError:
        return False  # outside the root, which `_resolve` refuses
    if parts[1:3] == _WITHHELD:
        return True
    if not parts:
        return False
    try:
        folder = top.joinpath(parts[0], *_WITHHELD).resolve()
    except (OSError, RuntimeError):
        return True  # a folder that cannot be told from here is not shown
    return path.is_relative_to(folder)

_TOOLS = (
    {"name": "list_dir", "description": "List a directory.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Relative path, default '.'"}}}},
    {"name": "read_file", "description": "Read the first 4000 characters of a file.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"]}},
    {"name": "grep", "description": "Regex search; path:line: text per match, capped.",
     "parameters": {"type": "object", "properties": {
         "pattern": {"type": "string"},
         "path": {"type": "string", "description": "File or directory, default '.'"}},
                    "required": ["pattern"]}},
)


def _tools(root: Path) -> list[dict]:
    """The tools offered to the model. The scoping itself is enforced by
    _resolve; the description just says where the paths start."""
    return [{"type": "function",
             "function": {**t, "description": f"{t['description']} Paths are relative to {root}."}}
            for t in _TOOLS]


def _resolve(root: Path, path: str) -> Path:
    p = (root / path).resolve()
    if not p.is_relative_to(root.resolve()):
        msg = f"{path} is outside the project directory"
        raise ValueError(msg)
    if _withheld(root.resolve(), p):
        msg = f"{path} is not available"
        raise ValueError(msg)
    return p


def _run_tool(root: Path, name: str, args: dict) -> str:
    if name == "list_dir":
        top = root.resolve()
        entries = sorted(
            (e for e in _resolve(root, args.get("path", ".")).iterdir()
             if not _withheld(top, e.resolve())),
            key=lambda e: (e.is_file(), e.name))
        return "\n".join(f"{e.name}/" if e.is_dir() else e.name for e in entries[:200]) or "(empty)"
    if name == "read_file":
        text = _resolve(root, args["path"]).read_text(errors="replace")
        return text[:4000] or "(empty file)"
    if name == "grep":
        return _grep(root, args.get("pattern", ""), args.get("path", "."))
    msg = f"unknown tool {name!r}"
    raise ValueError(msg)


def _tool_result(root: Path, call: dict) -> dict:
    """Run one tool call. Any failure, including a path outside the root,
    becomes an error string the model can recover from."""
    try:
        fn = call.get("function", {})
        args = json.loads(fn.get("arguments") or "{}")
    except Exception as e:
        return {"role": "tool", "tool_call_id": call.get("id", ""), "content": f"error: {e}"}
    if not isinstance(args, dict):
        return {"role": "tool", "tool_call_id": call.get("id", ""),
                "content": "error: arguments must be an object"}
    try:
        result = _run_tool(root, fn.get("name", ""), args)
    except Exception as e:
        result = f"error: {e}"
    return {"role": "tool", "tool_call_id": call.get("id", ""), "content": result}


def _grep(root: Path, pattern: str, path: str) -> str:
    rx = re.compile(pattern)
    base = _resolve(root, path)
    hits: list[str] = []

    top = root.resolve()

    def scan(f: Path) -> None:
        if len(hits) >= MAX_TOOL_HITS:
            return
        try:
            # `read_file` refuses anything that resolves outside the root; a
            # symlink the walk happens upon must meet the same rule, or a
            # `.env -> ../secrets` in a project is read and sent.
            if not f.resolve().is_relative_to(top):
                return
            if _withheld(top, f.resolve()):
                return
            if f.stat().st_size > (1 << 20):
                return
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            return
        for n, line in enumerate(lines, 1):
            if rx.search(line):
                hits.append(f"{f.resolve().relative_to(top)}:{n}: {line.strip()}")
                if len(hits) >= MAX_TOOL_HITS:
                    return

    if base.is_file():
        scan(base)
    else:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _PRUNE]
            for fname in filenames:
                scan(Path(dirpath) / fname)
                if len(hits) >= MAX_TOOL_HITS:
                    break
            if len(hits) >= MAX_TOOL_HITS:
                break
    return "\n".join(hits) or "no match"


# --- streaming ---------------------------------------------------------------

async def stream_text(
    prompt: str,
    *,
    system: str,
    model: str,
    on_delta: Callable[[str], None],
    timeout: float = 180.0,
) -> str:
    """One tool-less call, handing each piece of text to `on_delta` as it arrives."""
    payload = _payload(model, [{"role": "system", "content": system},
                               {"role": "user", "content": prompt}], stream=True)
    headers = {"Authorization": f"Bearer {api_key()}"}
    seen = False

    def emit(text: str) -> None:
        nonlocal seen
        seen = True
        on_delta(text)

    async with httpx.AsyncClient(transport=_transport,
                                 timeout=httpx.Timeout(timeout, connect=15.0)) as client:
        # Exactly one retry, and only before anything was shown: a mid-stream
        # death after the user has seen text would start the answer over.
        for attempt in (0, 1):
            try:
                return await _stream(client, payload, headers, emit)
            except (_StreamDeadError, httpx.TransportError) as e:
                if attempt or seen:
                    raise ClaudeError(str(e)) from None
                await _retry_sleep(2.0)
    raise AssertionError  # unreachable


async def _stream(client: httpx.AsyncClient, payload: dict, headers: dict,
                  on_delta: Callable[[str], None]) -> str:
    accumulated: list[str] = []
    async with client.stream("POST", base_url(), json=payload, headers=headers) as resp:
        if resp.status_code != HTTP_OK:
            if resp.status_code in _RETRY_STATUSES:
                msg = f"HTTP {resp.status_code}"
                raise _StreamDeadError(msg)
            raise ClaudeError(_error_text(resp.status_code, (await resp.aread()).decode(errors="replace")))
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue   # SSE keepalive comments and the blank separators
            data = line[5:].strip()
            if data == "[DONE]":
                return "".join(accumulated)
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if "error" in event:
                raise ClaudeError(str(event["error"].get("message") or event["error"])[:200])
            delta = ((event.get("choices") or [{}])[0].get("delta") or {})
            if delta.get("reasoning") or delta.get("reasoning_content"):
                continue   # thinking must never reach the transcript
            if delta.get("content"):
                accumulated.append(delta["content"])
                on_delta(delta["content"])
    msg = "stream ended without [DONE]"
    raise _StreamDeadError(msg)
