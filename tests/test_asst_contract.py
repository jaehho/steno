"""What steno reads from asst, read from asst's own samples (issue 35).

`asst-contract/` is a copy of asst's `contract/`: what `asst --json`
prints for each command steno reads, and the calls steno may make
(`consumers.json`), both pinned by asst's tests (jaehho/asst#27). The
fake asst answers from the same files. To take a change from asst:

    cp ~/projects/asst/contract/*.json tests/asst-contract/
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from steno import asstlink
from tests.fakeasst import CONTRACT

# A sibling checkout, as on the author's machine; ASST_SRC as asst's own
# launcher reads it.
ASST = Path(
    os.environ.get("ASST_SRC")
    or Path(__file__).resolve().parents[2] / "asst"
)


def sample(name: str) -> str:
    return (CONTRACT / name).read_text()


class TestCopy(unittest.TestCase):
    @unittest.skipUnless(
        (ASST / "contract").is_dir(), f"no asst checkout at {ASST}"
    )
    def test_the_copy_is_what_asst_promises(self):
        theirs = sorted(p.name for p in (ASST / "contract").glob("*.json"))
        ours = sorted(p.name for p in CONTRACT.glob("*.json"))
        self.assertEqual(ours, theirs)
        for name in theirs:
            with self.subTest(name):
                self.assertEqual(
                    json.loads(sample(name)),
                    json.loads((ASST / "contract" / name).read_text()),
                    f"{name} changed in asst; copy it (see this module)",
                )


class TestSamples(unittest.TestCase):
    """steno's parsers on what asst really prints."""

    def test_add(self):
        self.assertEqual(asstlink._parse_id(sample("add.json")), "5f3a")

    def test_show_open_and_completed(self):
        self.assertFalse(asstlink._parse_completed(sample("show.json")))
        (done,) = json.loads(sample("completed.json"))
        self.assertTrue(asstlink._parse_completed(json.dumps(done)))

    def test_lists(self):
        with mock.patch.object(
            asstlink, "_run", lambda _args: sample("lists.json")
        ):
            self.assertEqual(asstlink.list_names(), ["Inbox", "thesis"])

    def test_pull(self):
        def _run(args):
            return sample("ls.json" if args[0] == "ls" else "completed.json")

        with mock.patch.object(asstlink, "available", lambda: True), \
             mock.patch.object(asstlink, "_run", side_effect=_run):
            out = asstlink.fetch_session(Path("/tmp/20261012-100000"))
        prefix = "steno:20261012-100000/"
        self.assertEqual(out, {
            prefix + "send the usage figures to alice": {
                "id": "5f3a", "completed": False, "list": "thesis",
            },
            prefix + "book the room for the review": {
                "id": "a1d0", "completed": True, "list": "Inbox",
            },
        })


if __name__ == "__main__":
    unittest.main()
