"""`make install` puts the launcher, the entry and the icon under a
prefix; `make uninstall` takes back what it put there and nothing else.

Everything runs into a scratch prefix and data directory with HOME
pointed at scratch as well, so a write that strays lands where the
assertions look for it. Nothing here touches ~/.local, builds a real
venv, or launches the window: a checkout copy gets a stand-in `steno`,
and the one build that is tried gets a `uv` that fails.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parent.parent
HAS_MAKE = bool(shutil.which("make")) and (CHECKOUT / "Makefile").exists()
ENTRY = "dev.jaeho.Steno.desktop"
ICON = "icons/hicolor/scalable/apps/dev.jaeho.Steno.svg"
# What `make install` writes, relative to the scratch directory.
INSTALLED = {
    "prefix/bin/steno",
    f"share/applications/{ENTRY}",
    f"share/{ICON}",
}
# update-desktop-database keeps this beside every entry, not just ours.
SHARED = "share/applications/mimeinfo.cache"


# The package build runs the suite from a copy with no Makefile in it.
@unittest.skipUnless(HAS_MAKE, "no make, or no Makefile beside the tests")
class Install(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="steno-install-")
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        home = self.scratch / "home"
        home.mkdir()
        self.launcher = self.scratch / "prefix/bin/steno"
        self.entry = self.scratch / "share/applications" / ENTRY
        # Leave out make's own flags: this runs under `make test`.
        env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("MAKE", "MFLAGS"))
        }
        env.update(
            HOME=str(home),
            PREFIX=str(self.scratch / "prefix"),
            DATADIR=str(self.scratch / "share"),
            XDG_DATA_HOME=str(self.scratch / "share"),
            XDG_STATE_HOME=str(home / ".local/state"),
            XDG_CONFIG_HOME=str(home / ".config"),
            XDG_CACHE_HOME=str(home / ".cache"),
        )
        self.env = env

    def make(self, *args, root=CHECKOUT, ok=True):
        done = subprocess.run(
            ["make", "-C", str(root), *args],
            env=self.env, capture_output=True, text=True, check=False)
        if ok:
            self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def install(self):
        self.make("link", "desktop")

    def files(self):
        """Every file under scratch: {path: (mode, content)}.

        Whatever has strayed outside the prefix and the data directory,
        into HOME say, shows up here. The shared cache is left out: the
        desktop database tool owns it, and it outlives any one entry.
        """
        found = {}
        for path in self.scratch.rglob("*"):
            rel = path.relative_to(self.scratch).as_posix()
            if rel == SHARED or (path.is_dir() and not path.is_symlink()):
                continue
            content = path.readlink() if path.is_symlink() else \
                path.read_bytes()
            found[rel] = (path.lstat().st_mode, content)
        return found

    def fake_checkout(self, venv: bool, name: str = "checkout"):
        """A copy of what install reads, with or without a venv."""
        root = self.scratch / name
        root.mkdir()
        shutil.copy(CHECKOUT / "Makefile", root)
        shutil.copytree(CHECKOUT / "packaging", root / "packaging")
        shutil.copytree(CHECKOUT / "src/steno/data",
                        root / "src/steno/data")
        if venv:
            tool = root / ".venv/bin/steno"
            tool.parent.mkdir(parents=True)
            tool.write_text('#!/bin/sh\necho "fake steno: $*"\n')
            tool.chmod(0o755)
        return root

    def test_it_writes_a_launcher_an_entry_and_an_icon_and_no_more(self):
        self.install()
        self.assertEqual(set(self.files()), INSTALLED)
        modes = {k: stat.S_IMODE(v[0]) for k, v in self.files().items()}
        self.assertEqual(modes, {
            "prefix/bin/steno": 0o755,
            f"share/applications/{ENTRY}": 0o644,
            f"share/{ICON}": 0o644,
        })

    def test_make_install_itself_writes_all_three(self):
        # Not link and desktop by name: install must still run both.
        root = self.fake_checkout(venv=True)
        self.make("install", root=root)
        found = {k for k in self.files() if not k.startswith("checkout/")}
        self.assertEqual(found, INSTALLED)

    def test_a_failed_venv_build_leaves_the_install_as_it_was(self):
        # Installed from a checkout that works, then from one whose venv
        # cannot be built (offline, say): the launcher must not be
        # repointed at the one with nothing in it.
        good = self.fake_checkout(venv=True, name="good")
        self.make("install", root=good)
        before = {k: v for k, v in self.files().items()
                  if not k.startswith("good/")}
        shims = self.scratch / "shims"
        shims.mkdir()
        (shims / "uv").write_text("#!/bin/sh\nexit 1\n")
        (shims / "uv").chmod(0o755)
        self.env["PATH"] = f"{shims}:{self.env['PATH']}"
        bad = self.fake_checkout(venv=False, name="bad")
        done = self.make("install", root=bad, ok=False)
        self.assertNotEqual(done.returncode, 0)
        after = {k: v for k, v in self.files().items()
                 if not k.startswith(("good/", "bad/", "shims/"))}
        self.assertEqual(after, before)
        self.assertNotIn("installed;", done.stdout)

    def test_install_never_asks_for_privilege_a_service_or_a_package(self):
        out = self.make("-n", "install", "uninstall").stdout
        for word in ("sudo", "systemctl", "pacman", "paru", "makepkg"):
            with self.subTest(word):
                self.assertIsNone(re.search(rf"\b{word}\b", out))

    def test_install_builds_the_venv_when_there_is_none(self):
        out = self.make("-n", "install",
                        root=self.fake_checkout(venv=False)).stdout
        self.assertIn("uv venv", out)

    def test_a_built_venv_is_left_alone(self):
        root = self.fake_checkout(venv=True)
        out = self.make("-n", "install", root=root).stdout
        self.assertNotIn("uv venv", out)

    def test_a_second_install_changes_nothing(self):
        self.install()
        first = self.files()
        self.install()
        self.assertEqual(self.files(), first)

    def test_install_replaces_a_symlink_and_spares_what_it_pointed_at(self):
        # Writing through a link would empty its target.
        stash = self.scratch / "stash"
        stash.mkdir()
        dests = (self.launcher, self.entry, self.scratch / "share" / ICON)
        for dest in dests:
            dest.parent.mkdir(parents=True, exist_ok=True)
            target = stash / dest.name
            target.write_text("keep")
            dest.symlink_to(target)
        self.install()
        self.assertEqual({p.read_text() for p in stash.iterdir()}, {"keep"})
        for dest in dests:
            with self.subTest(dest.name):
                self.assertFalse(dest.is_symlink())
        self.assertEqual(
            {x for x in self.files() if not x.startswith("stash/")},
            INSTALLED)
        self.assertTrue(os.access(self.launcher, os.X_OK))

    def test_every_action_launches_by_the_absolute_launcher_path(self):
        self.install()
        lines = self.entry.read_text().splitlines()
        execs = [shlex.split(x.removeprefix("Exec=")) for x in lines
                 if x.startswith("Exec=")]
        self.assertEqual(
            execs,
            [[str(self.launcher), word] for word in
             ("gui", "toggle", "quit")])
        # Exec= is the only line that differs from the packaged entry.
        packaged = (CHECKOUT / "packaging" / ENTRY).read_text().splitlines()
        self.assertEqual([x for x in lines if not x.startswith("Exec=")],
                         [x for x in packaged if not x.startswith("Exec=")])

    def test_the_icon_is_the_one_the_package_ships(self):
        self.install()
        shipped = CHECKOUT / "src/steno/data" / ICON
        self.assertEqual((self.scratch / "share" / ICON).read_bytes(),
                         shipped.read_bytes())

    def test_the_launcher_runs_this_checkout_and_passes_arguments(self):
        # A space in the path: the launcher has to quote what it execs.
        root = self.fake_checkout(venv=True, name="my checkout")
        self.make("link", root=root)
        done = subprocess.run(
            [str(self.launcher), "gui", "--background"], cwd=self.scratch,
            env=self.env, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "fake steno: gui --background\n")

    def test_a_failed_desktop_leaves_the_installed_entry_alone(self):
        self.install()
        before = self.files()
        # A checkout whose entry template cannot be read: sed fails.
        broken = self.fake_checkout(venv=False)
        template = broken / "packaging" / ENTRY
        template.unlink()
        template.mkdir()
        done = self.make("desktop", root=broken, ok=False)
        self.assertNotEqual(done.returncode, 0)
        # The same entry, and no half-written file beside it.
        self.assertEqual(
            {k: v for k, v in self.files().items()
             if not k.startswith("checkout/")}, before)

    def test_uninstall_removes_what_install_made_and_nothing_else(self):
        # Config, sessions and notes, and neighbours of the installed
        # files: none of it is uninstall's to touch.
        for rel in ("home/.config/steno/.env",
                    "home/.local/share/steno/sessions/x/meta.json",
                    "home/.local/state/steno/state.json",
                    "prefix/bin/other-tool",
                    "share/applications/other.desktop",
                    "share/icons/hicolor/scalable/apps/other.svg"):
            path = self.scratch / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rel)
        before = self.files()
        self.install()
        self.assertEqual(set(self.files()) - set(before), INSTALLED)
        self.make("uninstall")
        self.assertEqual(self.files(), before)
        # Nothing left to remove is not an error.
        self.make("uninstall")
        self.assertEqual(self.files(), before)


if __name__ == "__main__":
    unittest.main()
