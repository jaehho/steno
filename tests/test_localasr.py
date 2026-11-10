"""Batch transcription on this machine: the rows, the switch, the GPU libraries.

The tests that run a model use the small `base.en` from the Hugging Face
cache with downloads off, and are skipped where it, faster-whisper or
espeak-ng is missing. Nothing here reaches the network.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from steno import localasr, transcribe
from steno.localasr import LocalConfig, config_from_env, rows_from_segments

STARTED_AT = 1_700_000_000.0
TEST_MODEL = "base.en"


@dataclass
class Seg:
    """What faster-whisper yields, as far as the rows need it."""

    start: float
    end: float
    text: str


def seg(start: float, end: float, text: str) -> Seg:
    return Seg(start, end, text)


def write_wav(path: Path, pcm: bytes = b"", rate: int = 16000,
              channels: int = 1) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return path


def model_cached(name: str) -> bool:
    try:
        from faster_whisper.utils import download_model  # noqa: PLC0415
    except ImportError:
        return False
    try:
        download_model(name, local_files_only=True)
    except Exception:  # any failure means "not here"
        return False
    return True


def speak(text: str, dest: Path) -> Path:
    """`text` as a steno track, synthesized by espeak-ng."""
    raw = dest.with_suffix(".raw.wav")
    subprocess.run(["espeak-ng", "-s", "150", "-w", str(raw), text],
                   check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i",
                    str(raw), "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
                    str(dest)], check=True)
    raw.unlink()
    return dest


class TestRows(unittest.TestCase):
    def test_rows_have_the_shape_the_deepgram_pass_returns(self):
        rows = rows_from_segments(
            [seg(1.0, 3.5, " Hello there. ")], STARTED_AT, "you"
        )
        self.assertEqual(rows, [{
            "ts": STARTED_AT + 3.5, "speaker": "you",
            "text": "Hello there.", "offset": 1.0,
        }])

    def test_ts_is_the_segments_end_and_offset_its_start(self):
        """As the live path stamps a line when it was finished."""
        rows = rows_from_segments(
            [seg(0.0, 2.0, "a"), seg(10.0, 12.5, "b")], STARTED_AT, "them"
        )
        self.assertEqual([r["ts"] - STARTED_AT for r in rows], [2.0, 12.5])
        self.assertEqual([r["offset"] for r in rows], [0.0, 10.0])

    def test_segments_with_no_words_are_dropped(self):
        rows = rows_from_segments(
            [seg(0, 1, "  "), seg(1, 2, ""), seg(2, 3, "ok")], 0.0, "you"
        )
        self.assertEqual([r["text"] for r in rows], ["ok"])


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        config = config_from_env({})
        self.assertEqual(config, LocalConfig())
        self.assertEqual(config.model, "large-v3-turbo")
        self.assertEqual(config.language, "en")
        self.assertEqual(config.label, "local:large-v3-turbo")

    def test_an_empty_language_lets_whisper_guess(self):
        self.assertIsNone(config_from_env({"STENO_LOCAL_LANGUAGE": ""}).language)

    def test_an_unknown_device_is_refused(self):
        with self.assertRaises(ValueError):
            config_from_env({"STENO_LOCAL_DEVICE": "tpu"})


class TestBackendSwitch(unittest.TestCase):
    def test_deepgram_unless_asked(self):
        self.assertEqual(transcribe.asr_backend({}), "deepgram")
        self.assertEqual(transcribe.asr_backend({"STENO_ASR": "Local"}), "local")
        self.assertEqual(transcribe.asr_backend({}, default="local"), "local")

    def test_an_unknown_backend_is_refused(self):
        with self.assertRaises(ValueError):
            transcribe.asr_backend({"STENO_ASR": "whisperx"})

    def test_local_never_reaches_deepgram_or_needs_its_key(self):
        local = mock.AsyncMock(return_value=[{"ts": 1.0}])
        env = {k: v for k, v in os.environ.items() if k != "DEEPGRAM_API_KEY"}
        env["STENO_ASR"] = "local"
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(transcribe, "transcribe_file_local", local), \
             mock.patch.object(transcribe.httpx, "AsyncClient",
                               side_effect=AssertionError("network")):
            rows = asyncio.run(transcribe.transcribe_file(
                Path("/x/you.wav"), 5.0, "you", keyterms=["Mimo"]
            ))
        self.assertEqual(rows, [{"ts": 1.0}])
        local.assert_awaited_once_with(Path("/x/you.wav"), 5.0, "you")

    def test_a_local_pass_cannot_tell_voices_apart(self):
        local = mock.AsyncMock(return_value=[])
        with mock.patch.object(transcribe, "transcribe_file_local", local), \
             self.assertRaisesRegex(ValueError, "cannot tell voices apart"):
            asyncio.run(transcribe.transcribe_file(
                Path("/x/you.wav"), 0.0, "you", voices_as="mic",
                backend="local",
            ))
        local.assert_not_awaited()

    def test_an_explicit_backend_beats_the_environment(self):
        local = mock.AsyncMock(return_value=[])
        with mock.patch.dict(os.environ, {"STENO_ASR": "deepgram"}), \
             mock.patch.object(transcribe, "transcribe_file_local", local):
            asyncio.run(transcribe.transcribe_file(
                Path("/x/you.wav"), 0.0, "you", backend="local"
            ))
        local.assert_awaited_once()


class TestCuda12(unittest.TestCase):
    def test_nothing_is_loaded_when_the_wheels_are_not_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "nvidia" / "cublas" / "lib").mkdir(parents=True)
            self.assertEqual(localasr.preload_cuda12([tmp]), [])
            self.assertEqual(localasr.preload_cuda12([]), [])

    def test_the_wheels_lib_directories_are_found_by_part(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            (Path(a) / "nvidia" / "cublas" / "lib").mkdir(parents=True)
            (Path(b) / "nvidia" / "cudnn" / "lib").mkdir(parents=True)
            found = localasr._cuda12_dirs([a, b])
        self.assertEqual(set(found), {"cublas", "cudnn"})


class TestMemory(unittest.TestCase):
    """steno's window runs the passes and stays open; each one hands the
    model's memory back when it ends, whether it worked or not."""

    def test_a_pass_hands_its_memory_back(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(localasr, "release_freed_memory") as release:
            path = write_wav(Path(tmp) / "x.wav")
            self.assertEqual(
                localasr.transcribe_wav(path, 0.0, "you", LocalConfig()), [])
        release.assert_called_once_with()

    def test_so_does_a_pass_that_fails(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(localasr, "release_freed_memory") as release:
            path = write_wav(Path(tmp) / "x.wav", rate=8000)
            with self.assertRaises(ValueError):
                localasr.transcribe_wav(path, 0.0, "you", LocalConfig())
        release.assert_called_once_with()

    def test_without_glibc_it_does_nothing(self):
        with mock.patch.object(localasr.ctypes, "CDLL", side_effect=OSError):
            localasr.release_freed_memory()


class TestReadWav(unittest.TestCase):
    def test_only_the_format_steno_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_wav(Path(tmp) / "x.wav", b"\0\0" * 10, rate=44100,
                             channels=2)
            with self.assertRaises(ValueError):
                localasr.read_wav(path)


@unittest.skipUnless(model_cached(TEST_MODEL),
                     f"faster-whisper or the cached {TEST_MODEL} model is missing")
class TestWithAModel(unittest.TestCase):
    config = LocalConfig(model=TEST_MODEL, device="cpu", threads=2)

    def setUp(self):
        patch = mock.patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"})
        patch.start()
        self.addCleanup(patch.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_an_empty_track_has_no_lines(self):
        path = write_wav(self.tmp / "you.wav")
        self.assertEqual(
            localasr.transcribe_wav(path, STARTED_AT, "you", self.config), []
        )

    def test_silence_has_no_lines(self):
        path = write_wav(self.tmp / "you.wav", b"\0\0" * 16000 * 5)
        self.assertEqual(
            localasr.transcribe_wav(path, STARTED_AT, "you", self.config), []
        )

    @unittest.skipUnless(shutil.which("espeak-ng"), "espeak-ng is missing")
    def test_speech_comes_back_as_timed_rows(self):
        path = speak("The meeting is on Thursday. "
                     "Sam will send the report by Friday.", self.tmp / "you.wav")
        rows = localasr.transcribe_wav(path, STARTED_AT, "you", self.config)
        self.assertTrue(rows)
        text = " ".join(r["text"] for r in rows).lower()
        for word in ("thursday", "report", "friday"):
            self.assertIn(word, text)
        with wave.open(str(path)) as w:
            seconds = w.getnframes() / w.getframerate()
        for r in rows:
            self.assertEqual(set(r), {"ts", "speaker", "text", "offset"})
            self.assertEqual(r["speaker"], "you")
            self.assertLessEqual(0.0, r["offset"])
            self.assertLess(STARTED_AT + r["offset"], r["ts"])
            self.assertLessEqual(r["ts"], STARTED_AT + seconds + 1.0)


if __name__ == "__main__":
    unittest.main()
