"""Load brief.md as standing advisor context. A curated, terse markdown file."""
from __future__ import annotations

import time
from pathlib import Path

BRIEF_FILENAME = "brief.md"


def estimate_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token for English/markdown."""
    return len(text) // 4


def format_token_count(n: int) -> str:
    return f"~{n} tok" if n < 1000 else f"~{n / 1000:.1f}k tok"


def load_brief(brief_path: Path) -> str | None:
    if not brief_path.is_file():
        return None
    try:
        text = brief_path.read_text()
    except OSError:
        return None
    return text if text.strip() else None


def brief_age_days(brief_path: Path) -> int | None:
    try:
        return int((time.time() - brief_path.stat().st_mtime) // 86400)
    except OSError:
        return None


def format_status(brief: str | None, age_days: int | None) -> str:
    """One-line header status."""
    if brief is None:
        return "no brief"
    tok = format_token_count(estimate_tokens(brief))
    if age_days is None:
        return f"brief: {tok}"
    age_s = "today" if age_days == 0 else f"{age_days}d old"
    return f"brief: {tok} · {age_s}"
