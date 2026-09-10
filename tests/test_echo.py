"""Getting the speakers out of the microphone, live and after the fact."""
from __future__ import annotations

import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from steno import echo, playback

try:
    import numpy
except ImportError:  # pragma: no cover — cleaning old sessions is what needs it
    numpy = None  # type: ignore[assignment]

needs_numpy = unittest.skipIf(numpy is None, "numpy not installed")


class TestSourceChoice(unittest.TestCase):
    """Which microphone the engine records from. Nothing here needs audio."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(echo.ENABLE_ENV, None)

    def test_the_real_mic_when_nothing_is_filtered(self):
        self.assertEqual(echo.pick_source("mic", ["mic", "mic.monitor"]), "mic")

    def test_the_filtered_source_when_there_is_one(self):
        chosen = echo.pick_source("mic", ["mic", echo.AEC_SOURCE_NAME])
        self.assertEqual(chosen, echo.AEC_SOURCE_NAME)

    def test_someone_elses_canceller_is_not_ours(self):
        """It is filtered against a reference we did not choose."""
        self.assertEqual(
            echo.pick_source("mic", ["mic", "easyeffects_source", "echo-cancel-source"]),
            "mic",
        )

    def test_the_environment_can_turn_it_off(self):
        with mock.patch.dict(os.environ, {echo.ENABLE_ENV: "0"}):
            self.assertEqual(
                echo.pick_source("mic", ["mic", echo.AEC_SOURCE_NAME]), "mic"
            )


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
        cleaned, erle = echo.cancel(mic, self.far, self.rate, delay)
        self.assertGreater(erle, 10.0)
        self.assertLess(float(numpy.abs(cleaned).max()), float(numpy.abs(mic).max()))

    def test_leaves_the_near_side_alone(self):
        """The point is to remove them, not to remove everything."""
        voice = (self.near(noise=0.05)).astype("float32")
        mic = (self.echoed + voice).astype("float32")
        cleaned, _erle = echo.cancel(
            mic, self.far, self.rate, echo.estimate_delay(mic, self.far, self.rate)
        )
        # What is left should look much more like the near side than the mic did.
        def correlation(a, b):
            return float(a @ b / numpy.sqrt((a @ a) * (b @ b)))
        self.assertGreater(correlation(cleaned, voice), correlation(mic, voice))

    def test_no_echo_means_nothing_to_remove(self):
        """A meeting held on headphones must come out unharmed."""
        voice = self.near(noise=0.05)
        _cleaned, erle = echo.cancel(voice, self.far, self.rate, 0)
        self.assertLess(abs(erle), 3.0)
        self.assertFalse(echo.Cancellation(0.0, erle, 1.0).worthwhile)

    def test_alignment_keeps_the_microphone_length(self):
        """Every offset in the transcript is measured against that length."""
        for delay in (-500, 0, 500):
            aligned = echo.align(self.far, delay, len(self.far))
            self.assertEqual(len(aligned), len(self.far))

    def test_too_short_to_align_is_not_a_crash(self):
        tiny = numpy.zeros(64, dtype="float32")
        self.assertEqual(echo.estimate_delay(tiny, tiny, self.rate), 0)
        cleaned, erle = echo.cancel(tiny, tiny, self.rate, 0)
        self.assertEqual(len(cleaned), 64)
        self.assertEqual(erle, 0.0)


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


if __name__ == "__main__":
    unittest.main()
