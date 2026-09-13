"""Keeping the speakers out of the microphone.

Steno records two tracks: the microphone (`you`) and the monitor of whatever
the machine is playing (`them`). On headphones those are genuinely separate
signals. On speakers they are not — the microphone hears the far side coming
out of the speakers, so `you.wav` contains a delayed, room-coloured copy of
`them.wav`, and every question the transcript is supposed to answer about who
said something becomes a coin flip.

There are two places to fix that, and this module holds both.

*Before the fact*, PipeWire can put a canceller in front of the microphone
(dotfiles installs it, `audio` package). It is a WirePlumber smart filter, so
recording the default microphone already records through it; nothing in the
engine needs to know, and there is nothing to configure here.

*After the fact*, an existing recording can be cleaned the same way, because
the reference signal the canceller needs is exactly what `them.wav` already is.
`cancel_file()` estimates the delay between the two tracks and adapts a filter
that models the path from the speakers back into the mic. That is only worth
doing for meetings recorded before any of this existed, so it lives behind a
command rather than running on its own, and it never overwrites the original.
"""
from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

from .playback import clean_path

# --------------------------------------------------------------------- offline

# 64 ms blocks at 16 kHz. Long enough that an hour of audio is tens of thousands
# of iterations rather than millions; short enough to track a room.
BLOCK = 1024
# Eight blocks of tail: half a second of echo path, which covers a laptop's
# speaker-to-mic delay and the buffering on either side of it with room to spare.
# Measured: a longer tail buys nothing, because what limits this is the speaker
# distorting the signal, not the room being bigger than half a second.
PARTITIONS = 8
# All partitions share one normalisation, so the stability bound is 2/PARTITIONS.
# 0.15 sits comfortably under it; 0.5 diverges, loudly and audibly.
STEP = 0.15
# Residual suppression. What the adaptive filter cannot reach is the part of the
# echo that is not a linear function of the reference — mostly the speaker's own
# distortion — so it is attenuated by gain rather than subtracted.
SUPPRESS_FRAME = 512
SUPPRESS_OVER = 2.0
SUPPRESS_FLOOR = 0.1
# How far apart the two recordings are allowed to be before we give up on
# aligning them and assume something other than echo is going on.
MAX_DELAY_S = 1.0
# RMS below which a block of reference audio counts as nothing playing.
ACTIVITY_FLOOR = 1e-3


@dataclass(frozen=True)
class Cancellation:
    """What a cleaning pass actually achieved, in numbers rather than vibes."""

    delay_s: float
    erle_db: float
    seconds: float

    @property
    def worthwhile(self) -> bool:
        """Did it remove enough to be worth keeping?

        Below a few dB the filter found nothing to cancel, which is the normal
        and happy outcome for a meeting recorded on headphones.
        """
        return self.erle_db >= 3.0


def _read(path: Path):
    import numpy as np

    with wave.open(str(path)) as handle:
        if handle.getsampwidth() != 2 or handle.getnchannels() != 1:
            raise ValueError(f"{path.name}: expected 16-bit mono")
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0, rate


def _write(path: Path, samples, rate: int) -> None:
    import numpy as np

    clipped = np.clip(samples, -1.0, 1.0)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((clipped * 32767.0).astype("<i2").tobytes())


def estimate_delay(mic, ref, rate: int, max_delay_s: float = MAX_DELAY_S) -> int:
    """Samples the microphone lags the reference by.

    Generalised cross-correlation with phase transform: dividing out the
    magnitude leaves only phase, so the peak is where the two signals line up
    rather than where they are both merely loud. Measured over several windows
    and taken as a median, because any single window can land on a stretch where
    only one side is talking and correlate with noise.
    """
    import numpy as np

    limit = int(max_delay_s * rate)
    usable = min(len(mic), len(ref))
    if usable <= limit * 2:
        return 0
    # 32 s at 16 kHz, or the largest power of two that fits a short recording.
    window = min(1 << 19, 1 << (usable.bit_length() - 1))

    starts = np.linspace(0, max(0, usable - window), 9, dtype=int)
    votes: list[int] = []
    for start in starts:
        a = mic[start:start + window]
        b = ref[start:start + window]
        if len(a) < window or len(b) < window:
            continue
        if float(np.sqrt(np.mean(b * b))) < 1e-4:
            continue  # nothing playing: nothing to align to
        size = 1 << (2 * window - 1).bit_length()
        A = np.fft.rfft(a, size)
        B = np.fft.rfft(b, size)
        cross = A * np.conj(B)
        cross /= np.abs(cross) + 1e-12
        corr = np.fft.irfft(cross, size)
        # Only lags within the plausible range, both signs: which of the two
        # `parec` processes started first is not something we control.
        head = corr[:limit + 1]
        tail = corr[-limit:]
        joined = np.concatenate([tail, head])
        peak = int(np.argmax(joined)) - limit
        votes.append(peak)
    if not votes:
        return 0
    return int(np.median(votes))


def cancel(mic, ref, rate: int, delay: int, suppress: bool = True):
    """Subtract the reference from the microphone. Returns (cleaned, ERLE dB).

    A partitioned frequency-domain adaptive filter — the standard multi-delay
    block filter. Each block of reference audio is convolved with a filter that
    is itself updated from how much echo was left over last time, so the thing
    it converges on is a model of the room: the speaker, the air, the desk, and
    the microphone's own colouring of all three.

    There is deliberately no double-talk detector. Your own voice is not
    correlated with what the speakers are playing, so over an hour it pushes the
    filter in no particular direction and averages out of the gradient; a
    detector would converge faster on a short clip and is one more thing to get
    wrong on a long one. Blocks where nothing was playing are skipped, since
    they have nothing to teach.
    """
    import numpy as np

    n = BLOCK
    bins = n + 1

    aligned = align(ref, delay, len(mic))
    blocks = len(mic) // n
    out = np.array(mic, dtype="float32", copy=True)
    if blocks < 2:
        return out, 0.0
    estimated = np.zeros_like(out)

    weights = np.zeros((PARTITIONS, bins), dtype="complex128")
    history = np.zeros((PARTITIONS, bins), dtype="complex128")
    power = np.zeros(bins)
    energy_in = 0.0
    energy_out = 0.0
    # Below this the speakers were silent, and a silent block cancels perfectly
    # for reasons that say nothing about the filter.
    floor = ACTIVITY_FLOOR ** 2 * n

    for m in range(1, blocks):
        block = aligned[(m - 1) * n:(m + 1) * n]
        history = np.roll(history, 1, axis=0)
        history[0] = np.fft.rfft(block)

        estimate = np.fft.irfft(np.sum(weights * history, axis=0))[n:]
        near = mic[m * n:(m + 1) * n]
        residual = near - estimate
        out[m * n:(m + 1) * n] = residual
        estimated[m * n:(m + 1) * n] = estimate

        current = aligned[m * n:(m + 1) * n]
        if float(current @ current) < floor:
            continue
        energy_in += float(near @ near)
        energy_out += float(residual @ residual)

        power = 0.9 * power + 0.1 * np.sum(np.abs(history) ** 2, axis=0)
        padded = np.concatenate([np.zeros(n, dtype="float32"), residual])
        error = np.fft.rfft(padded)
        gradient = np.conj(history) * error * (STEP / (power + 1e-6))
        # Keep the filter causal and no longer than its partition: the update is
        # computed in the frequency domain, where nothing stops it from growing
        # a tail that wraps around and models the future.
        taps = np.fft.irfft(gradient, axis=1)
        taps[:, n:] = 0.0
        weights += np.fft.rfft(taps, axis=1)

    if suppress:
        out = suppress_residual(out, estimated)

    erle = 0.0
    if energy_in > 0 and energy_out > 0:
        erle = 10.0 * float(np.log10(energy_in / energy_out))
    return out, erle


def align(ref, delay: int, length: int):
    """Slide the reference by `delay` and make it exactly `length` samples.

    Padded rather than trimmed at the near end: the microphone track is what
    every offset in the transcript is measured against, so its length is not
    ours to change.
    """
    import numpy as np

    if delay > 0:
        ref = np.concatenate([np.zeros(delay, dtype="float32"), ref])
    elif delay < 0:
        ref = ref[-delay:]
    if len(ref) < length:
        return np.concatenate([ref, np.zeros(length - len(ref), dtype="float32")])
    return ref[:length]


def suppress_residual(residual, estimate):
    """Turn down what is left of the echo, band by band.

    The adaptive filter can only remove the part of the echo that is a linear
    function of what was played; a laptop speaker is not linear, and what
    survives is a quieter, dirtier copy of the same words. It cannot be
    subtracted, but it can be attenuated wherever the echo estimate says most
    of the energy in a band was theirs and not yours.

    Half-overlapped frames under a root-Hann window, applied twice, so the
    frames sum back to unity and a gain that changes between them does not
    click. Where you are the one talking the estimate is small next to what is
    actually there, the gain stays near one, and your voice comes through — 0.6
    dB of it goes, against 3.4 dB of theirs.
    """
    import numpy as np

    frame = SUPPRESS_FRAME
    hop = frame // 2
    window = np.sqrt(np.hanning(frame + 1)[:frame])
    padded = ((max(0, len(residual) - frame)) // hop + 1) * hop + frame
    left = np.zeros(padded)
    left[:len(residual)] = residual
    echo = np.zeros(padded)
    echo[:len(estimate)] = estimate

    out = np.zeros(padded)
    norm = np.zeros(padded)
    previous = None
    for i in range(0, padded - frame + 1, hop):
        spectrum = np.fft.rfft(left[i:i + frame] * window)
        echo_spectrum = np.fft.rfft(echo[i:i + frame] * window)
        gain = np.clip(
            1.0 - SUPPRESS_OVER * np.abs(echo_spectrum) / (np.abs(spectrum) + 1e-9),
            SUPPRESS_FLOOR, 1.0,
        )
        if previous is not None:
            gain = 0.5 * gain + 0.5 * previous
        previous = gain
        out[i:i + frame] += np.fft.irfft(spectrum * gain) * window
        norm[i:i + frame] += window * window
    out /= np.maximum(norm, 1e-9)
    return out[:len(residual)].astype("float32")


def cancel_file(
    mic_path: Path,
    ref_path: Path,
    out_path: Path | None = None,
    suppress: bool = True,
) -> Cancellation:
    """Clean one recorded microphone track against its own session's playback.

    Writes beside the original rather than over it. A recording is the one thing
    here that cannot be regenerated, and this is a lossy, best-effort filter.
    """
    mic, rate = _read(mic_path)
    ref, ref_rate = _read(ref_path)
    if ref_rate != rate:
        raise ValueError("tracks were recorded at different sample rates")
    delay = estimate_delay(mic, ref, rate)
    cleaned, erle = cancel(mic, ref, rate, delay, suppress=suppress)
    result = Cancellation(delay / rate, erle, len(mic) / rate if rate else 0.0)
    if result.worthwhile:
        _write(out_path or clean_path(mic_path), cleaned, rate)
    return result
