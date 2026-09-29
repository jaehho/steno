"""What the transcript is costing, in minutes and dollars.

Only ever an estimate — Deepgram bills to the millisecond and the model bills
in tokens we do not see — but it is the number that answers "how much is this
tool spending?" without opening a dashboard. The rates are the published
pay-as-you-go ones for nova-3, kept in one place so a price change is a
one-line edit.
"""
from __future__ import annotations

# Deepgram nova-3, USD per minute of audio. Live streaming is metered
# separately from prerecorded and is the dearer of the two.
STREAM_PER_MINUTE = 0.0077
PRERECORDED_PER_MINUTE = 0.0043

# A summary is one chat-completions call. Measured in output tokens the caller
# never sees; this is a deliberately flat guess at "a meeting's summary".
SUMMARY_GUESS = 0.01
CENT = 0.01  # USD display floor
SECONDS_PER_MINUTE = 60


def session_minutes(audio_seconds: float) -> float:
    return audio_seconds / 60.0


def stream_cost(minutes: float) -> float:
    return minutes * STREAM_PER_MINUTE


def prerecorded_cost(minutes: float) -> float:
    return minutes * PRERECORDED_PER_MINUTE


def session_cost(streamed_min: float, batched_min: float, summaries: int = 1) -> float:
    """One meeting: the far side streamed live, the mic batched after."""
    return (
        stream_cost(streamed_min)
        + prerecorded_cost(batched_min)
        + summaries * SUMMARY_GUESS
    )


def format_usd(amount: float) -> str:
    if amount <= 0:
        return "$0"
    if amount < CENT:
        return "<$0.01"
    return f"${amount:.2f}"


def format_minutes(minutes: float) -> str:
    if minutes < 1:
        return "0 min"
    if minutes < SECONDS_PER_MINUTE:
        return f"{minutes:.0f} min"
    return f"{minutes / SECONDS_PER_MINUTE:.1f} h"


def format_meter(minutes: float, dollars: float) -> str:
    """One line for the footer: `4.2 h · ~$1.14`."""
    return f"{format_minutes(minutes)} · ~{format_usd(dollars)}"
