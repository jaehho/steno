"""Getting the speakers out of the microphone, live and after the fact."""
from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path

from steno import echo, playback

try:
    import numpy
except ImportError:  # pragma: no cover — cleaning old sessions is what needs it
    numpy = None  # type: ignore[assignment]

needs_numpy = unittest.skipIf(numpy is None, "numpy not installed")


class TestTrackChoice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for name in ("you.wav", "them.wav"):
            (self.root / name).write_bytes(b"\0")

    def test_the_original_when_it_is_all_there_is(self):
        self.assertEqual(playback.track_path(self.root, "you").name, "you.wav")

    def test_the_cleaned_copy_wins(self):
        (self.root / "you.clean.wav").write_bytes(b"\0")
        self.assertEqual(playback.track_path(self.root, "you").name, "you.clean.wav")
        # ...for that side only.
        self.assertEqual(playback.track_path(self.root, "them").name, "them.wav")

    def test_both_sides_are_offered_for_playback(self):
        self.assertEqual(
            sorted(playback.session_tracks(self.root)), ["them", "you"]
        )

    def test_a_pruned_session_offers_nothing(self):
        for name in ("you.wav", "them.wav"):
            (self.root / name).unlink()
        self.assertEqual(playback.session_tracks(self.root), {})

    def test_cleaning_never_lands_on_the_original(self):
        original = self.root / "you.wav"
        self.assertNotEqual(playback.clean_path(original), original)


@needs_numpy
class TestCancellation(unittest.TestCase):
    """A known echo, put in on purpose, and taken back out."""

    def setUp(self):
        self.rate = 16000
        rng = numpy.random.default_rng(7)
        seconds = 100
        self.far = (rng.standard_normal(self.rate * seconds) * 0.1).astype("float32")
        # A room: a strong first arrival and a couple of reflections.
        room = numpy.zeros(400, dtype="float32")
        room[120], room[210], room[330] = 0.5, -0.2, 0.1
        self.room = room
        self.echoed = numpy.convolve(self.far, room)[:len(self.far)].astype("float32")

    def near(self, noise: float = 0.001):
        rng = numpy.random.default_rng(11)
        return (rng.standard_normal(len(self.far)) * noise).astype("float32")

    def test_finds_the_delay(self):
        mic = self.echoed + self.near()
        self.assertEqual(echo.estimate_delay(mic, self.far, self.rate), 120)

    def test_finds_a_delay_the_other_way_round(self):
        """Which of the two recorders started first is not something we pick."""
        shifted = numpy.concatenate([
            self.far[300:], numpy.zeros(300, dtype="float32")
        ])
        mic = numpy.convolve(shifted, self.room)[:len(shifted)].astype("float32")
        self.assertEqual(
            echo.estimate_delay(mic, self.far, self.rate), 120 - 300
        )

    def test_removes_the_echo(self):
        mic = self.echoed + self.near()
        delay = echo.estimate_delay(mic, self.far, self.rate)
        cleaned, erle, _muted = echo.cancel(mic, self.far, self.rate, delay)
        self.assertGreater(erle, 10.0)
        self.assertLess(float(numpy.abs(cleaned).max()), float(numpy.abs(mic).max()))

    def test_leaves_the_near_side_alone(self):
        """The point is to remove them, not to remove everything.

        The near side has to dominate the far track to survive the gate: a
        quiet mumble under a loud speaker is gated, by design. The suppressor
        is lossy, so the bar is that what is left looks like the near side,
        not that it is a closer copy than the raw mic was.
        """
        voice = (self.near(noise=0.2)).astype("float32")
        mic = (self.echoed + voice).astype("float32")
        cleaned, _erle, _muted = echo.cancel(
            mic, self.far, self.rate, echo.estimate_delay(mic, self.far, self.rate)
        )
        def correlation(a, b):
            return float(a @ b / numpy.sqrt((a @ a) * (b @ b)))
        self.assertGreater(correlation(cleaned, voice), 0.9)
        self.assertGreater(correlation(cleaned, voice), correlation(cleaned, self.echoed))

    def test_no_echo_means_nothing_to_remove(self):
        """A meeting held on headphones must come out unharmed."""
        voice = self.near(noise=0.05)
        _cleaned, erle, muted = echo.cancel(voice, self.far, self.rate, 0)
        self.assertLess(abs(erle), 3.0)
        self.assertFalse(echo.Cancellation(0.0, erle, 1.0).worthwhile)
        self.assertEqual(muted, 0.0)

    def test_alignment_keeps_the_microphone_length(self):
        """Every offset in the transcript is measured against that length."""
        for delay in (-500, 0, 500):
            aligned = echo.align(self.far, delay, len(self.far))
            self.assertEqual(len(aligned), len(self.far))

    def test_too_short_to_align_is_not_a_crash(self):
        tiny = numpy.zeros(64, dtype="float32")
        self.assertEqual(echo.estimate_delay(tiny, tiny, self.rate), 0)
        cleaned, erle, muted = echo.cancel(tiny, tiny, self.rate, 0)
        self.assertEqual(len(cleaned), 64)
        self.assertEqual(erle, 0.0)
        self.assertEqual(muted, 0.0)


@needs_numpy
class TestCancelFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, name: str, samples, rate: int = 16000) -> Path:
        path = self.root / name
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(
                (numpy.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            )
        return path

    def test_writes_a_clean_track_beside_the_original(self):
        rng = numpy.random.default_rng(3)
        far = (rng.standard_normal(16000 * 100) * 0.1).astype("float32")
        room = numpy.zeros(400, dtype="float32")
        room[120] = 0.6
        mic = numpy.convolve(far, room)[:len(far)].astype("float32")
        mic_path = self.write("you.wav", mic)
        ref_path = self.write("them.wav", far)

        result = echo.cancel_file(mic_path, ref_path)

        self.assertTrue(result.worthwhile)
        self.assertAlmostEqual(result.delay_s, 120 / 16000, places=4)
        cleaned = playback.clean_path(mic_path)
        self.assertTrue(cleaned.is_file())
        self.assertTrue(mic_path.is_file())
        with wave.open(str(cleaned)) as handle:
            self.assertEqual(handle.getnframes(), len(mic))

    def test_headphones_leave_no_file_behind(self):
        rng = numpy.random.default_rng(4)
        far = (rng.standard_normal(16000 * 30) * 0.1).astype("float32")
        voice = (numpy.random.default_rng(5).standard_normal(len(far)) * 0.1)
        mic_path = self.write("you.wav", voice)
        ref_path = self.write("them.wav", far)

        result = echo.cancel_file(mic_path, ref_path)

        self.assertFalse(result.worthwhile)
        self.assertFalse(playback.clean_path(mic_path).exists())

    def test_mismatched_rates_are_refused(self):
        a = self.write("you.wav", numpy.zeros(16000, dtype="float32"), rate=16000)
        b = self.write("them.wav", numpy.zeros(8000, dtype="float32"), rate=8000)
        with self.assertRaises(ValueError):
            echo.cancel_file(a, b)


@needs_numpy
class TestNonlinearResidual(unittest.TestCase):
    """Speaker distortion: what the linear filter cannot reach."""

    def setUp(self):
        self.rate = 16000
        rng = numpy.random.default_rng(21)
        seconds = 30
        n = self.rate * seconds
        self.far = (rng.standard_normal(n) * 0.2).astype("float32")
        # A waveshaper before the room: the speaker is not linear, so what
        # arrives is a distorted copy the adaptive filter cannot subtract.
        driven = numpy.tanh(3.0 * self.far) / numpy.tanh(3.0)
        room = numpy.zeros(400, dtype="float32")
        room[120], room[210] = 0.5, -0.2
        self.echoed = numpy.convolve(driven, room)[:n].astype("float32")
        self.room = room

    def near(self, noise: float = 0.001):
        rng = numpy.random.default_rng(11)
        return (rng.standard_normal(len(self.far)) * noise).astype("float32")

    @staticmethod
    def _rms(x) -> float:
        return float(numpy.sqrt(numpy.mean(numpy.square(x))))

    def test_linear_cancel_alone_leaves_residual(self):
        mic = self.echoed + self.near()
        delay = echo.estimate_delay(mic, self.far, self.rate)
        cleaned, _erle, _muted = echo.cancel(
            mic, self.far, self.rate, delay, suppress=False
        )
        # Documents why suppress exists: the linear stage is not enough.
        self.assertGreater(self._rms(cleaned), 0.2 * self._rms(mic))

    def test_suppress_plus_gate_kills_saturation_residual(self):
        mic = self.echoed + self.near()
        delay = echo.estimate_delay(mic, self.far, self.rate)
        cleaned, _erle, muted = echo.cancel(mic, self.far, self.rate, delay)
        self.assertGreater(muted, 1.0)
        # Far-only stretches should be near silence, not speech-shaped bleed.
        self.assertLess(self._rms(cleaned), 0.05 * self._rms(self.echoed))
        self.assertEqual(len(cleaned), len(mic))

    def test_gate_keeps_louder_near_speech(self):
        near = self.near(noise=0.05).astype("float32")
        # A burst that clearly dominates the far side (twice its level, the
        # GATE_HANGOVER_RATIO, so it is kept and not just tolerated).
        burst = slice(self.rate * 10, self.rate * 12)
        near[burst] = 0.6 * numpy.sin(
            2 * numpy.pi * 220 * numpy.arange(burst.stop - burst.start) / self.rate
        ).astype("float32")
        mic = (self.echoed + near).astype("float32")
        delay = echo.estimate_delay(mic, self.far, self.rate)
        cleaned, _erle, _muted = echo.cancel(mic, self.far, self.rate, delay)
        self.assertGreater(self._rms(cleaned[burst]), 0.02)

    def test_gate_spares_quiet_far(self):
        voice = self.near(noise=0.05).astype("float32")
        quiet_far = numpy.zeros_like(self.far)
        _cleaned, _erle, muted = echo.cancel(voice, quiet_far, self.rate, 0)
        self.assertEqual(muted, 0.0)

    def test_hangover_keeps_trailing_syllable(self):
        residual = numpy.zeros(self.rate * 4, dtype="float32")
        far = numpy.full_like(residual, 0.2)
        # Near speech that clearly dominates them, then a short ratio dip.
        residual[self.rate : self.rate * 2] = 0.5
        residual[self.rate * 2 : self.rate * 2 + int(0.05 * self.rate)] = 0.01
        residual[self.rate * 2 + int(0.05 * self.rate) : self.rate * 3] = 0.5
        gated, muted = echo.gate_far_dominant(residual, far, self.rate)
        dip = slice(self.rate * 2, self.rate * 2 + int(0.05 * self.rate))
        self.assertGreater(float(numpy.abs(gated[dip]).max()), 0.0)
        self.assertGreater(muted, 0.0)

    def test_borderline_keep_does_not_hang_over(self):
        """Residual at the keep threshold must not hold the gate open."""
        residual = numpy.zeros(self.rate * 3, dtype="float32")
        far = numpy.full_like(residual, 0.2)
        # Just at GATE_RATIO (keep) but well under GATE_HANGOVER_RATIO.
        residual[self.rate : self.rate * 2] = 0.2
        residual[self.rate * 2 : self.rate * 2 + int(0.1 * self.rate)] = 0.01
        gated, muted = echo.gate_far_dominant(residual, far, self.rate)
        after = slice(self.rate * 2, self.rate * 2 + int(0.1 * self.rate))
        self.assertEqual(float(numpy.abs(gated[after]).max()), 0.0)
        self.assertGreater(muted, 0.0)

    def test_gate_preserves_length(self):
        mic = self.echoed + self.near()
        for delay in (0, 120, -200):
            cleaned, _erle, _muted = echo.cancel(mic, self.far, self.rate, delay)
            self.assertEqual(len(cleaned), len(mic))

    def test_cancel_file_writes_clean_not_original(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        def write(name: str, samples) -> Path:
            path = root / name
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(self.rate)
                handle.writeframes(
                    (numpy.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
                )
            return path

        mic = self.echoed + self.near()
        mic_path = write("you.wav", mic)
        ref_path = write("them.wav", self.far)
        original = mic_path.read_bytes()

        result = echo.cancel_file(mic_path, ref_path)

        self.assertTrue(result.worthwhile)
        self.assertEqual(mic_path.read_bytes(), original)
        self.assertTrue(playback.clean_path(mic_path).is_file())
        self.assertGreater(result.muted_s, 0.0)


if __name__ == "__main__":
    unittest.main()
