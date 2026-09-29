"""Load brief.md as standing advisor context. A curated, terse markdown file."""
from __future__ import annotations

import re
import time
from pathlib import Path

BRIEF_FILENAME = "brief.md"
TOKEN_K = 1000  # chars-per-token scale for the k display
MIN_TERM_LEN = 2  # single letters are not names


def estimate_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token for English/markdown."""
    return len(text) // 4


def format_token_count(n: int) -> str:
    return f"~{n} tok" if n < TOKEN_K else f"~{n / 1000:.1f}k tok"


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


# Sentence openers and glue words that are never names. A leaked common word
# costs one keyterm slot; a missed name costs a garbled word in the transcript.
_STOPWORDS = frozenset(
    ["a", "an", "the", "and", "or", "but", "if", "then", "else", "when", "while", "for", "to", "of", "in", "on", "at", "by", "with", "from", "as", "is", "are", "was", "were", "be", "been", "being", "this", "that", "these", "those", "it", "its", "i", "we", "you", "they", "he", "she", "them", "us", "my", "our", "your", "their", "his", "her", "not", "no", "yes", "do", "does", "did", "done", "so", "what", "which", "who", "whom", "how", "why", "where", "all", "any", "both", "each", "few", "more", "most", "other", "some", "such", "only", "own", "same", "than", "too", "very", "can", "will", "just", "should", "now", "also", "one", "two", "three", "new", "old", "first", "last", "next", "previous", "use", "call", "called", "meet", "meeting", "notes", "note", "todo", "todos", "today", "tomorrow", "yesterday", "week", "month", "year", "morning", "afternoon", "evening", "night", "please", "thanks", "thank", "ok", "okay", "sure", "right", "well", "back", "over", "out", "up", "down", "again", "once", "twice", "here", "there", "thing", "things", "way", "ways", "met", "said", "told", "asked", "sent", "got", "made", "took", "gave", "went", "came", "found", "thought", "knew", "felt", "tried", "started", "stopped", "finished", "working", "looking", "going", "doing", "having", "being", "let", "put", "set", "run", "ran", "keep", "kept", "let", "want", "wanted", "need", "needed", "help", "helped", "talk", "talked", "speak", "spoke", "discuss", "discussed", "review", "reviewed", "present", "presented", "join", "joined", "leave", "left", "send", "sending", "get", "getting", "make", "making", "take", "taking", "give", "giving", "think", "thinking", "know", "knowing", "see", "seeing", "look", "looking", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
)


def proper_nouns(text: str, limit: int = 40) -> list[str]:
    """Terms the speech recognition is most likely to mangle: names, products,
    acronyms, and the project's own vocabulary.

    Heuristic on purpose. The brief is one curated page of mostly proper nouns;
    capitalization and backticks are enough signal without asking a model to
    pick them out. Multi-word names stay whole (`Deepgram Nova-3`, `Jane Doe`),
    because keyterm boosting a fragment is how "Jane" lands as "chain".
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        term = term.strip(".,;:!?()[]{}\"'")
        words = term.split()
        # "The API" is not a name; "API" is. Drop glue words from the front.
        while words and words[0].lower() in _STOPWORDS:
            words.pop(0)
        term = " ".join(words)
        if len(term) < MIN_TERM_LEN or term.lower() in _STOPWORDS:
            return
        key = term.lower()
        if key not in seen:
            seen.add(key)
            candidates.append(term)

    # Backticked terms are names the author already marked as load-bearing.
    for m in re.finditer(r"`([^`\n]{2,48})`", text):
        add(m.group(1))

    # Title-Case runs on one line: "Deepgram Nova-3", "Lakeview Research Lab".
    # A newline ends the run; otherwise a heading swallows the sentence after it.
    for m in re.finditer(r"\b[A-Z][\w'\u2019-]*(?:[ \t]+[A-Z][\w'\u2019-]*)*", text):
        add(m.group(0))

    # ALL-CAPS acronyms the Title-Case pass would split or skip mid-word.
    for m in re.finditer(r"\b[A-Z][A-Z0-9]{1,7}\b", text):
        add(m.group(0))

    return candidates[:limit]


def brief_for(brief_path: Path, app: str = "") -> Path:
    """The brief for this meeting: one named for the app, else the standing one.

    A call in `zoom` is a different conversation from a Discord chat with a
    collaborator, and one page cannot hold both without diluting each. The
    fallback is always the standing brief, so an app nobody has written a page
    for is not an empty context — it is the context the user already curates.

    Calendar events would resolve the same way (`brief.<event>.md`); nothing in
    the pipeline knows about a calendar yet, so that path is the caller's to
    invent when it exists.
    """
    if app:
        # The app label comes from a PulseAudio property and can be anything:
        # "Zoom", "zoom", "Google Chrome". Normalise before it becomes a path.
        slug = re.sub(r"[^a-z0-9]+", "-", app.lower()).strip("-")
        if slug:
            candidate = brief_path.with_name(f"{brief_path.stem}.{slug}{brief_path.suffix}")
            if candidate.is_file():
                return candidate
    return brief_path
