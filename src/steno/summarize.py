"""End-of-meeting summary: transcript -> summary.md + meta.json title."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from anthropic import AsyncAnthropic
from anthropic.types import TextBlockParam

SUMMARY_FILENAME = "summary.md"
META_FILENAME = "meta.json"

SYSTEM_PROMPT = """\
You summarize meeting transcripts. 'you' is the person wearing the mic (the user \
you are writing for); 'them' is everyone else on the call.

Output GitHub-flavored markdown, nothing else — no preamble, no code fence.
The FIRST line must be `# <title>`: 3-6 words naming this specific meeting, \
concrete enough to pick out of a list of fifty (topic and counterpart, not \
"Team Meeting"). Then, omitting any section that would be empty:

## TL;DR
2-4 bullets. What happened and what it means for the user.

## Decisions
What was actually settled. Skip if nothing was.

## Action items
Group under `**You**` / `**Them**` / `**Unassigned**`. Include the deadline if \
one was said. Only real commitments — do not manufacture tasks.

## Open questions
Raised and left unresolved.

## Key facts
Specific numbers, names, dates, links, and constraints worth keeping.

The transcript is automatic speech recognition: expect garbled words, dropped \
negations, and wrong homophones. Where a passage is too mangled to read, say so \
rather than guessing at it, and never invent detail that is not in the text.
Be terse. No filler, no restating these instructions."""


@dataclass
class Summary:
    title: str
    body: str


def transcript_path(session_dir: Path) -> Path:
    return session_dir / "transcript.jsonl"


def load_records(session_dir: Path) -> list[dict]:
    """Read transcript.jsonl, tolerating partial final lines from a killed session."""
    path = transcript_path(session_dir)
    if not path.is_file():
        return []
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    records.sort(key=lambda r: r.get("ts", 0.0))
    return records


def format_for_model(records: list[dict]) -> str:
    """Render as `[mm:ss] speaker: text`, relative to session start."""
    if not records:
        return ""
    t0 = records[0].get("ts", 0.0)
    lines = []
    for r in records:
        offset = int(r.get("ts", t0) - t0)
        stamp = f"{offset // 60:02d}:{offset % 60:02d}"
        lines.append(f"[{stamp}] {r.get('speaker', '?')}: {r.get('text', '')}")
    return "\n".join(lines)


def parse_summary(text: str) -> Summary:
    """Split the leading `# title` off the body; fall back to a generic title."""
    body = text.strip()
    lines = body.splitlines()
    if lines and lines[0].startswith("# "):
        return Summary(title=lines[0][2:].strip(), body=body)
    return Summary(title="Untitled meeting", body=body)


def duration_minutes(records: list[dict]) -> float:
    if len(records) < 2:
        return 0.0
    return (records[-1].get("ts", 0.0) - records[0].get("ts", 0.0)) / 60.0


async def summarize_session(
    session_dir: Path,
    model: str,
    brief: str | None = None,
    client: AsyncAnthropic | None = None,
) -> Summary:
    """Summarize one session and write summary.md + meta.json. Raises on failure."""
    records = load_records(session_dir)
    if not records:
        raise ValueError(f"no transcript in {session_dir}")
    convo = format_for_model(records)

    system: str | list[TextBlockParam]
    if brief:
        system = [
            TextBlockParam(type="text", text=SYSTEM_PROMPT),
            TextBlockParam(
                type="text",
                text=(
                    "# Standing context on the user and their work\n\n"
                    "Use it to resolve names, jargon, and acronyms in the transcript. "
                    "Do not summarize the context itself.\n\n" + brief
                ),
            ),
        ]
    else:
        system = SYSTEM_PROMPT

    owned_client = client is None
    client = client or AsyncAnthropic()
    try:
        buf = ""
        async with client.messages.stream(
            model=model,
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": convo}],
        ) as stream:
            async for delta in stream.text_stream:
                buf += delta
    finally:
        if owned_client:
            await client.close()

    summary = parse_summary(buf)
    (session_dir / SUMMARY_FILENAME).write_text(summary.body + "\n")
    write_meta(session_dir, summary, records, model)
    return summary


def write_meta(
    session_dir: Path, summary: Summary, records: list[dict], model: str
) -> None:
    """Persist the title and session stats so the archive can list them."""
    meta_path = session_dir / META_FILENAME
    meta: dict = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            meta = {}
    meta.update({
        "title": summary.title,
        "summarized_at": time.time(),
        "summary_model": model,
        "lines": len(records),
        "duration_min": round(duration_minutes(records), 1),
    })
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")


def load_title(session_dir: Path) -> str | None:
    """Title from meta.json, for listings. None if the session isn't summarized."""
    meta_path = session_dir / META_FILENAME
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text()).get("title")
    except (OSError, json.JSONDecodeError):
        return None


def has_summary(session_dir: Path) -> bool:
    return (session_dir / SUMMARY_FILENAME).is_file()


def sessions_root(root: Path | None = None) -> Path:
    from . import data_dir
    return root if root is not None else data_dir() / "sessions"


def iter_sessions(root: Path | None = None) -> list[Path]:
    """Session dirs holding a transcript, newest first."""
    base = sessions_root(root)
    if not base.is_dir():
        return []
    return sorted(
        (p for p in base.iterdir() if p.is_dir() and transcript_path(p).is_file()),
        key=lambda p: p.name,
        reverse=True,
    )


def session_stamp(session_dir: Path) -> tuple:
    """What a meeting looks like on disk, cheaply enough to check often.

    Everything the tool writes after a meeting ends — a re-timed transcript, a
    new summary, a ticked todo — happens in a file, and the CLI writes those
    files while the window is open. Comparing modification times is how a view
    notices it is showing something that has since been replaced.
    """
    marks = []
    watched = (
        transcript_path(session_dir),
        session_dir / SUMMARY_FILENAME,
        session_dir / "todos.json",
        session_dir / "notes.md",
    )
    for path in watched:
        try:
            marks.append(path.stat().st_mtime_ns)
        except OSError:
            marks.append(0)
    return (session_dir.name, *marks)


def resolve_session(spec: str | None, root: Path | None = None) -> Path:
    """Resolve a session name, a path, or None (latest). Raises FileNotFoundError."""
    if spec is None:
        sessions = iter_sessions(root)
        if not sessions:
            raise FileNotFoundError(f"no sessions with a transcript under {sessions_root(root)}")
        return sessions[0]
    as_path = Path(spec)
    if as_path.is_dir():
        return as_path
    candidate = sessions_root(root) / spec
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError(f"no such session: {spec}")
