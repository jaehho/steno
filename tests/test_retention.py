"""Dropping old audio — and, more importantly, refusing to."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from steno import retention


def make_session(root: Path, name: str, *, transcript=True, summary=True,
                 audio=True, age_days=0) -> Path:
    session = root / name
    session.mkdir(parents=True)
    if transcript:
        (session / "transcript.jsonl").write_text(
            json.dumps({"ts": 1.0, "speaker": "them", "text": "hello"}) + "\n"
        )
    if summary:
        (session / "summary.md").write_text("# A meeting\n")
    if audio:
        (session / "you.wav").write_bytes(b"\0" * 1024)
        (session / "them.wav").write_bytes(b"\0" * 2048)
    if age_days and transcript:
        old = time.time() - age_days * 86400
        os.utime(session / "transcript.jsonl", (old, old))
    return session


class TestKeepDays(unittest.TestCase):
    def test_default_is_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(retention.keep_days(), 0)

    def test_reads_the_environment(self):
        with mock.patch.dict(os.environ, {retention.KEEP_DAYS_ENV: "14"}):
            self.assertEqual(retention.keep_days(), 14)

    def test_nonsense_is_off_rather_than_a_crash(self):
        with mock.patch.dict(os.environ, {retention.KEEP_DAYS_ENV: "soon"}):
            self.assertEqual(retention.keep_days(), 0)


class TestPruning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_off_by_default_deletes_nothing(self):
        make_session(self.root, "20200101-000000", age_days=999)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(retention.prune_audio(self.root), [])

    def test_old_and_summarized_is_pruned(self):
        session = make_session(self.root, "20200101-000000", age_days=90)
        pruned = retention.prune_audio(self.root, days=30)
        self.assertEqual(pruned, [session])
        self.assertFalse((session / "you.wav").exists())
        self.assertTrue((session / "transcript.jsonl").exists())
        self.assertTrue((session / "summary.md").exists())

    def test_recent_audio_is_kept(self):
        make_session(self.root, "20200101-000000", age_days=2)
        self.assertEqual(retention.prune_audio(self.root, days=30), [])

    def test_never_prunes_an_unsummarized_session(self):
        """It can still be summarized; and if the transcript were ever lost,
        the audio is the only remaining copy of the meeting."""
        make_session(self.root, "20200101-000000", summary=False, age_days=90)
        self.assertEqual(retention.prune_audio(self.root, days=30), [])

    def test_never_prunes_a_session_with_no_transcript(self):
        make_session(self.root, "20200101-000000", transcript=False, age_days=90)
        self.assertEqual(retention.prune_audio(self.root, days=30), [])

    def test_records_that_the_audio_went_deliberately(self):
        session = make_session(self.root, "20200101-000000", age_days=90)
        retention.prune_audio(self.root, days=30)
        meta = json.loads((session / "meta.json").read_text())
        self.assertIn("audio_pruned_at", meta)
        self.assertEqual(meta["audio_freed_bytes"], 3072)

    def test_pruning_twice_is_harmless(self):
        make_session(self.root, "20200101-000000", age_days=90)
        retention.prune_audio(self.root, days=30)
        self.assertEqual(retention.prune_audio(self.root, days=30), [])

    def test_totals(self):
        make_session(self.root, "20200101-000000")
        make_session(self.root, "20200102-000000")
        self.assertEqual(retention.total_audio_bytes(self.root), 6144)


class TestTrash(unittest.TestCase):
    """Deleting a meeting deletes a conversation that cannot be had again."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.root = self.home / "sessions"
        self.root.mkdir()
        patcher = mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": str(self.home / "data")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_trashed_not_destroyed(self):
        session = make_session(self.root, "20200101-000000")
        outcome, where = retention.delete_session(session)
        self.assertEqual(outcome, "trashed")
        self.assertFalse(session.exists())
        assert where is not None
        self.assertTrue(where.is_dir())
        self.assertTrue((where / "transcript.jsonl").is_file())

    def test_writes_a_trashinfo_so_the_desktop_can_restore_it(self):
        session = make_session(self.root, "20200101-000000")
        retention.delete_session(session)
        info = retention.trash_dir() / "info" / "20200101-000000.trashinfo"
        self.assertTrue(info.is_file())
        body = info.read_text()
        self.assertIn("[Trash Info]", body)
        self.assertIn("DeletionDate=", body)
        self.assertIn("sessions", body)

    def test_two_deletions_of_the_same_name_coexist(self):
        first = make_session(self.root, "20200101-000000")
        retention.delete_session(first)
        second = make_session(self.root, "20200101-000000")
        _outcome, where = retention.delete_session(second)
        assert where is not None
        self.assertEqual(where.name, "20200101-000000-1")
        self.assertTrue((retention.trash_dir() / "files" / "20200101-000000").is_dir())

    def test_a_failed_trash_is_never_upgraded_to_a_real_delete(self):
        """The one behaviour that must not regress: if trashing fails, the
        meeting is still there and the caller is told, rather than it being
        quietly destroyed instead."""
        session = make_session(self.root, "20200101-000000")
        with mock.patch.object(retention, "move_to_trash", return_value=None):
            outcome, where = retention.delete_session(session)
        self.assertEqual((outcome, where), ("failed", None))
        self.assertTrue(session.is_dir())

    def test_permanent_when_explicitly_asked(self):
        session = make_session(self.root, "20200101-000000")
        outcome, _ = retention.delete_session(session, permanent=True)
        self.assertEqual(outcome, "deleted")
        self.assertFalse(session.exists())

    def test_missing_session(self):
        self.assertEqual(
            retention.delete_session(self.root / "nope"), ("failed", None)
        )

    def test_failed_trash_leaves_no_dangling_info(self):
        session = make_session(self.root, "20200101-000000")
        with mock.patch("os.rename", side_effect=OSError(18, "cross-device")):
            self.assertIsNone(retention.move_to_trash(session))
        self.assertEqual(list((retention.trash_dir() / "info").glob("*")), [])


class TestDeleteAudio(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_audio_goes_and_words_stay(self):
        session = make_session(self.root, "20200101-000000")
        freed = retention.delete_audio(session)
        self.assertEqual(freed, 3072)
        self.assertFalse((session / "you.wav").exists())
        self.assertTrue((session / "transcript.jsonl").is_file())
        self.assertTrue((session / "summary.md").is_file())

    def test_no_audio_frees_nothing(self):
        session = make_session(self.root, "20200101-000000", audio=False)
        self.assertEqual(retention.delete_audio(session), 0)


class TestHumanBytes(unittest.TestCase):
    def test_scales(self):
        self.assertEqual(retention.human_bytes(512), "512 B")
        self.assertEqual(retention.human_bytes(2048), "2 KB")
        self.assertTrue(retention.human_bytes(5 * 1024 * 1024).endswith("MB"))
        self.assertTrue(retention.human_bytes(3 * 1024**3).endswith("GB"))


if __name__ == "__main__":
    unittest.main()
