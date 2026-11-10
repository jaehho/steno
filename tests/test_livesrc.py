"""Which tree the running app says it runs from, and its lease.

The git checks build a scratch repository with a linked worktree, as
`~/projects/steno.wt/*` are; nothing reads this checkout's own git
state except `package_tree`, which only looks at paths.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import steno
from steno import livesrc
from steno.livesrc import Head, Source

CHECKOUT = Path(__file__).resolve().parent.parent
INSTALLED = "/home/u/projects/steno"
WORKTREE = "/home/u/projects/steno.wt/25-rename"
# The owner's global git config may sign commits or run hooks.
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
        env={**os.environ, **GIT_ENV})
    return done.stdout.strip()


class TestTestBuild(unittest.TestCase):
    def test_the_installed_checkout_on_main_is_not_one(self):
        source = Source(INSTALLED, "main", "130d018", INSTALLED)
        self.assertFalse(source.is_test_build)

    def test_a_worktree_is_one_whatever_its_branch(self):
        self.assertTrue(
            Source(WORKTREE, "feat/25-rename", "201e472",
                   INSTALLED).is_test_build)
        self.assertTrue(
            Source(WORKTREE, "main", "130d018", INSTALLED).is_test_build)

    def test_the_installed_checkout_on_another_branch_is_one(self):
        source = Source(INSTALLED, "feat/x", "abc1234", INSTALLED)
        self.assertTrue(source.is_test_build)

    def test_a_detached_head_is_one(self):
        self.assertTrue(Source(INSTALLED, "", "abc1234", "").is_test_build)

    def test_without_the_launcher_main_still_reads_as_installed(self):
        # `uv run steno gui` in the checkout: no STENO_DEFAULT_ROOT.
        self.assertFalse(Source(INSTALLED, "main", "130d018", "").is_test_build)

    def test_the_label_names_the_branch_and_commit(self):
        source = Source(WORKTREE, "feat/25-rename", "201e472", INSTALLED)
        self.assertEqual(source.label, "feat/25-rename@201e472")

    def test_a_detached_label_names_the_folder(self):
        source = Source(WORKTREE, "", "201e472", INSTALLED)
        self.assertEqual(source.label, "25-rename@201e472")


class TestLease(unittest.TestCase):
    def test_the_record_says_whose_it_is_and_since_when(self):
        source = Source(WORKTREE, "feat/25-rename", "201e472", INSTALLED)
        record = livesrc.lease_record(source, pid=4242, started_at=10.5)
        self.assertEqual(record["pid"], 4242)
        self.assertEqual(record["started_at"], 10.5)
        self.assertTrue(record["test_build"])
        self.assertEqual(record["label"], "feat/25-rename@201e472")
        self.assertEqual(record["tree"], WORKTREE)

    def test_the_log_line_says_test_build_and_both_trees(self):
        source = Source(WORKTREE, "feat/25-rename", "201e472", INSTALLED)
        line = livesrc.log_line(source)
        self.assertTrue(line.startswith("test build feat/25-rename@201e472"))
        self.assertIn(WORKTREE, line)
        self.assertIn(INSTALLED, line)

    def test_round_trip_and_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "steno" / livesrc.LEASE_FILENAME
            self.assertEqual(livesrc.read_lease(path), {})
            livesrc.write_lease(path, {"pid": 1, "label": "x"})
            self.assertEqual(livesrc.read_lease(path),
                             {"pid": 1, "label": "x"})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
            path.write_text("{not json")
            self.assertEqual(livesrc.read_lease(path), {})
            path.write_text("[1, 2]")
            self.assertEqual(livesrc.read_lease(path), {})

    def test_it_lives_beside_the_log(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/s"}):
            self.assertEqual(livesrc.lease_path(),
                             Path("/s/steno/live-src.json"))


class TestPackageTree(unittest.TestCase):
    def test_it_is_the_checkout_this_suite_runs_from(self):
        self.assertEqual(livesrc.package_tree(steno.__file__), CHECKOUT)


@unittest.skipUnless(shutil.which("git"), "no git")
class TestGitHead(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="steno-livesrc-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.main = self.root / "steno"
        self.main.mkdir()
        git(self.main, "init", "-q", "-b", "main")
        (self.main / "f").write_text("1\n")
        git(self.main, "add", "f")
        git(self.main, "commit", "-q", "-m", "one")
        self.sha = git(self.main, "rev-parse", "--short", "HEAD")

    def test_the_main_checkout(self):
        head = livesrc.git_head(self.main)
        self.assertEqual(head, Head("main", self.sha))

    def test_a_linked_worktree_on_a_branch(self):
        tree = self.root / "steno.wt" / "25-rename"
        git(self.main, "worktree", "add", "-q", "-b", "feat/25-rename",
            str(tree))
        head = livesrc.git_head(tree)
        self.assertEqual(head, Head("feat/25-rename", self.sha))
        source = livesrc.describe(tree, head, str(self.main))
        self.assertTrue(source.is_test_build)
        self.assertEqual(source.default_root, str(self.main.resolve()))

    def test_a_detached_head(self):
        git(self.main, "checkout", "-q", "--detach")
        self.assertEqual(livesrc.git_head(self.main), Head("", self.sha))

    def test_not_a_checkout(self):
        bare = self.root / "unpacked"
        bare.mkdir()
        self.assertEqual(livesrc.git_head(bare), Head("", ""))

    def test_the_installed_checkout_reads_as_installed(self):
        head = livesrc.git_head(self.main)
        source = livesrc.describe(self.main, head, str(self.main))
        self.assertFalse(source.is_test_build)
        self.assertEqual(json.loads(json.dumps(livesrc.lease_record(
            source, pid=1, started_at=0.0)))["test_build"], False)


if __name__ == "__main__":
    unittest.main()
