"""Keeping the speakers out of the microphone.

Steno records two tracks: the microphone (`you`) and the monitor of whatever
the machine is playing (`them`). On headphones those are genuinely separate
signals. On speakers they are not — the microphone hears the far side coming
out of the speakers, so `you.wav` contains a delayed, room-coloured copy of
`them.wav`, and every question the transcript is supposed to answer about who
said something becomes a coin flip.

There is one fix, after the fact: `cancel_file()` estimates the delay between
the two tracks and adapts a filter that models the path from the speakers back
into the mic. The reference signal that filter needs is exactly what `them.wav`
already is.

A live PipeWire canceller in front of the microphone was tried and taken back
out: it left two permanent recording streams in every mixer and kept the mic hot
whenever anything woke the speaker path. Offline is enough — on speakers, run
`steno clean-echo` and the bleed comes out. It lives behind a command rather
than running on its own (it is lossy and best-effort) and never overwrites the
original.
"""
from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

from .playback import clean_path

ERLE_WORTHWHILE_DB = 3.0  # below this the canceller found nothing
MIN_RMS = 1e-4  # silence floor for the reference channel
MIN_BLOCKS = 2  # need at least two blocks to estimate a filter
SAMPLE_WIDTH_16BIT = 2  # bytes per sample in the WAV files we read

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
# The linear estimate is a lower bound on the echo. Speaker distortion is a
# nonlinear function of what was played, so energy lands in bands the filter
# never predicted (harmonics). The far track is the other proxy for "this
# could be theirs". It is only usable after the same room gain the filter
# measured: speakers are loud and the mic is not, and an unscaled far level
# eats your own quiet speech along with the bleed. That gain is taken from
# blocks where the filter was not making things worse; a diverged filter
# inflates estimate energy and would turn the far proxy into a mute-everything
# knob.
FAR_ECHO_WEIGHT = 0.8
# Relative energy gate, after suppression. Residual during far speech is quiet
# next to them; your own voice is not. Absolute RMS cannot tell them apart
# (both sit near 0.005 on a laptop), so the comparison is concurrent with the
# far track. Keep only what is at least as loud as them: residual after a hard
# suppress still sits at 0.2-1x far in short bursts, which is exactly what ASR
# re-attributes as "Yes." / "Okay." next to the same words on `them`. Anything
# quieter than the speaker goes to silence. That mutes quiet questions and
# backchannels during loud talk; you.wav is sacred, and the clean copy is
# lossy on purpose. The window is 50 ms, not a frame of speech: 20 ms peaks
# of residual clear a ratio on their own and hangover then holds the gate.
GATE_FRAME_S = 0.05
GATE_RATIO = 1.0
# Keep the last syllable of your own speech through a brief dip in the ratio,
# but only when the speech clearly dominated them. A borderline keep is still
# residual and must not hold the gate open for ASR. Mute edges fade so the
# gate does not click.
GATE_HANGOVER_S = 0.05
GATE_HANGOVER_RATIO = 2.0
GATE_FADE_S = 0.01
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
    muted_s: float = 0.0

    @property
    def worthwhile(self) -> bool:
        """Did it remove enough to be worth keeping?

        Below a few dB the filter found nothing to cancel, which is the normal
        and happy outcome for a meeting recorded on headphones.
        """
        return self.erle_db >= ERLE_WORTHWHILE_DB


def _read(path: Path):
    import numpy as np  # noqa: PLC0415  # optional extra (clean-echo only)

    with wave.open(str(path)) as handle:
        if handle.getsampwidth() != SAMPLE_WIDTH_16BIT or handle.getnchannels() != 1:
            msg = f"{path.name}: expected 16-bit mono"
            raise ValueError(msg)
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0, rate


def _write(path: Path, samples, rate: int) -> None:
    import numpy as np  # noqa: PLC0415  # optional extra (clean-echo only)

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
    import numpy as np  # noqa: PLC0415  # optional extra (clean-echo only)

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
        if float(np.sqrt(np.mean(b * b))) < MIN_RMS:
            continue  # nothing playing: nothing to align to
        size = 1 << (2 * window - 1).bit_length()
        a_fft = np.fft.rfft(a, size)
        b_fft = np.fft.rfft(b, size)
        cross = a_fft * np.conj(b_fft)
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
    """Subtract the reference from the microphone.

    Returns ``(cleaned, ERLE dB, muted seconds)``.

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

    What the linear filter cannot reach is then attenuated and, when the far
    side is clearly dominant, gated. ERLE is measured on the linear stage only,
    so the number stays comparable across runs; the gate reports its own
    contribution as muted seconds.
    """
    import numpy as np  # noqa: PLC0415  # optional extra (clean-echo only)

    n = BLOCK
    bins = n + 1

    aligned = align(ref, delay, len(mic))
    blocks = len(mic) // n
    out = np.array(mic, dtype="float32", copy=True)
    if blocks < MIN_BLOCKS:
        return out, 0.0, 0.0
    estimated = np.zeros_like(out)

    weights = np.zeros((PARTITIONS, bins), dtype="complex128")
    history = np.zeros((PARTITIONS, bins), dtype="complex128")
    power = np.zeros(bins)
    energy_in = 0.0
    energy_out = 0.0
    # Per-block estimate/far gains, from blocks where the filter was not
    # making the signal worse. A diverged filter inflates estimate energy and
    # would turn the far proxy into a mute-everything knob.
    gains: list[float] = []
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
        if float(residual @ residual) <= float(near @ near):
            gains.append(
                float(np.sqrt(np.mean(estimate * estimate) / np.mean(current * current)))
            )

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

    erle = 0.0
    if energy_in > 0 and energy_out > 0:
        erle = 10.0 * float(np.log10(energy_in / energy_out))

    muted_s = 0.0
    if suppress:
        path_gain = float(np.median(gains)) if gains else 0.0
        out = suppress_residual(out, estimated, far=aligned, path_gain=path_gain)
        # The relative gate only applies when the filter actually found echo.
        # On headphones the far track is still "active", and gating on its
        # level alone would punch holes in your own voice.
        if erle >= ERLE_WORTHWHILE_DB:
            out, muted_s = gate_far_dominant(out, aligned, rate)

    return out, erle, muted_s


def align(ref, delay: int, length: int):
    """Slide the reference by `delay` and make it exactly `length` samples.

    Padded rather than trimmed at the near end: the microphone track is what
    every offset in the transcript is measured against, so its length is not
    ours to change.
    """
    import numpy as np  # noqa: PLC0415  # optional extra (clean-echo only)

    if delay > 0:
        ref = np.concatenate([np.zeros(delay, dtype="float32"), ref])
    elif delay < 0:
        ref = ref[-delay:]
    if len(ref) < length:
        return np.concatenate([ref, np.zeros(length - len(ref), dtype="float32")])
    return ref[:length]


def suppress_residual(residual, estimate, far=None, path_gain: float = 0.0):
    """Turn down what is left of the echo, band by band.

    The adaptive filter can only remove the part of the echo that is a linear
    function of what was played; a laptop speaker is not linear, and what
    survives is a quieter, dirtier copy of the same words. It cannot be
    subtracted, but it can be attenuated wherever the echo estimate says most
    of the energy in a band was theirs and not yours.

    The linear estimate alone underestimates that leftover (harmonics land
    where the filter never predicted them). `far` is the delay-aligned
    playback, scaled by `path_gain` (how much of the far track the filter saw
    in the mic). Without that scale the far track is speaker-loud and would
    suppress your own quiet speech along with the bleed.

    Half-overlapped frames under a root-Hann window, applied twice, so the
    frames sum back to unity and a gain that changes between them does not
    click. Where you are the one talking the estimate is small next to what is
    actually there, the gain stays near one, and your voice comes through.
    """
    import numpy as np  # noqa: PLC0415  # optional extra (clean-echo only)

    frame = SUPPRESS_FRAME
    hop = frame // 2
    window = np.sqrt(np.hanning(frame + 1)[:frame])
    padded = ((max(0, len(residual) - frame)) // hop + 1) * hop + frame
    left = np.zeros(padded)
    left[:len(residual)] = residual
    echo = np.zeros(padded)
    echo[:len(estimate)] = estimate
    reference = None
    if far is not None and path_gain > 0.0:
        reference = np.zeros(padded)
        reference[:len(far)] = far

    out = np.zeros(padded)
    norm = np.zeros(padded)
    previous = None
    for i in range(0, padded - frame + 1, hop):
        spectrum = np.fft.rfft(left[i:i + frame] * window)
        echo_spectrum = np.fft.rfft(echo[i:i + frame] * window)
        proxy = np.abs(echo_spectrum)
        if reference is not None:
            far_spectrum = np.fft.rfft(reference[i:i + frame] * window)
            far_level = float(np.sqrt(np.mean(np.abs(far_spectrum) ** 2)))
            proxy = np.maximum(
                proxy, path_gain * FAR_ECHO_WEIGHT * far_level
            )
        gain = np.clip(
            1.0 - SUPPRESS_OVER * proxy / (np.abs(spectrum) + 1e-9),
            SUPPRESS_FLOOR, 1.0,
        )
        if previous is not None:
            gain = 0.5 * gain + 0.5 * previous
        previous = gain
        out[i:i + frame] += np.fft.irfft(spectrum * gain) * window
        norm[i:i + frame] += window * window
    out /= np.maximum(norm, 1e-9)
    return out[:len(residual)].astype("float32")


def _gate_mute_mask(residual, far, rate: int):
    """Per-frame bool: silence this frame? Uses the same 50 ms grid as the gate."""
    import numpy as np  # noqa: PLC0415  # optional extra (clean-echo only)

    frame = max(1, int(GATE_FRAME_S * rate))
    hangover = max(1, int(GATE_HANGOVER_S / GATE_FRAME_S))
    n = len(residual)
    frames = (n + frame - 1) // frame
    mute = np.zeros(frames, dtype=bool)
    remaining = 0
    for i in range(frames):
        start = i * frame
        stop = min(n, start + frame)
        near = residual[start:stop]
        ref = far[start:stop] if start < len(far) else far[-1:]
        far_rms = float(np.sqrt(np.mean(ref * ref))) if len(ref) else 0.0
        near_rms = float(np.sqrt(np.mean(near * near))) if len(near) else 0.0
        far_active = far_rms > ACTIVITY_FLOOR
        if (not far_active) or (near_rms >= GATE_RATIO * far_rms):
            # Only speech that clearly dominated them holds the gate open.
            # A borderline keep is residual and must not hang over into ASR.
            if (not far_active) or (near_rms >= GATE_HANGOVER_RATIO * far_rms):
                remaining = hangover
        elif remaining > 0:
            remaining -= 1
        else:
            mute[i] = True
    return mute


def _fade_mute_edges(out, mute, rate: int) -> None:
    """Soften mute/unmute boundaries so a gate that chatters does not click."""
    import numpy as np  # noqa: PLC0415  # optional extra (clean-echo only)

    frame = max(1, int(GATE_FRAME_S * rate))
    fade = max(1, int(GATE_FADE_S * rate))
    n = len(out)
    previous = False
    for i in range(len(mute)):
        if mute[i] == previous:
            continue
        previous = bool(mute[i])
        at = i * frame
        if mute[i]:
            start = max(0, at - fade)
            if at > start:
                ramp = np.cos(np.linspace(0.0, np.pi / 2, at - start, endpoint=False)) ** 2
                out[start:at] *= ramp
        else:
            stop = min(n, at + fade)
            if stop > at:
                ramp = np.sin(np.linspace(0.0, np.pi / 2, stop - at, endpoint=False)) ** 2
                out[at:stop] *= ramp


def gate_far_dominant(residual, far, rate: int):
    """Silence residual that is quiet next to what the speakers are playing.

    After suppression the leftover is still speech-shaped, and ASR will
    transcribe it as you. Energy alone cannot call it speech: residual and a
    quiet near side both sit near the room floor. Concurrent far energy can.
    When they are talking and you are not at least as loud as them, it is
    their bleed; when they are quiet, nothing is gated and a headphones
    meeting comes through untouched.

    A hangover keeps the last syllable of your own speech through a brief dip
    in the ratio, and the mute edges fade so the gate does not click. Length
    is preserved: every offset in the transcript is measured against the mic.
    Returns ``(gated, muted seconds)``.
    """
    import numpy as np  # noqa: PLC0415  # optional extra (clean-echo only)

    frame = max(1, int(GATE_FRAME_S * rate))
    out = np.array(residual, dtype="float32", copy=True)
    mute = _gate_mute_mask(out, far, rate)
    if not mute.any():
        return out, 0.0

    n = len(out)
    for i in np.flatnonzero(mute):
        out[i * frame:min(n, (i + 1) * frame)] = 0.0
    _fade_mute_edges(out, mute, rate)
    muted = int(mute.sum()) * frame
    return out, muted / rate if rate else 0.0


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
        msg = "tracks were recorded at different sample rates"
        raise ValueError(msg)
    delay = estimate_delay(mic, ref, rate)
    cleaned, erle, muted_s = cancel(mic, ref, rate, delay, suppress=suppress)
    result = Cancellation(
        delay / rate, erle, len(mic) / rate if rate else 0.0, muted_s
    )
    if result.worthwhile:
        _write(out_path or clean_path(mic_path), cleaned, rate)
    return result
