"""The record key must start a listener that is down, and toggle once."""
from __future__ import annotations

import unittest
from unittest import mock

from steno import control


class TestToggleRecord(unittest.TestCase):
    def toggle(self, running, proc=None, activated=True):
        """Run toggle_record; `running` is what is_running answers, in turn."""
        spawn = mock.Mock(return_value=proc)
        activate = mock.Mock(return_value=activated)
        with mock.patch.object(control, "is_running", side_effect=running), \
             mock.patch.object(control, "_spawn", spawn), \
             mock.patch.object(control, "activate", activate), \
             mock.patch.object(control.time, "sleep"):
            result = control.toggle_record()
        return result, spawn, activate

    @staticmethod
    def proc(exit_code=None):
        return mock.Mock(**{"poll.return_value": exit_code})

    def test_a_running_listener_is_toggled_without_a_launch(self):
        result, spawn, activate = self.toggle([True])
        self.assertTrue(result)
        spawn.assert_not_called()
        activate.assert_called_once()
        self.assertEqual(activate.call_args.args[0], "toggle-record")

    def test_a_down_listener_is_launched_quietly_then_toggled(self):
        result, spawn, activate = self.toggle(
            [False, False, True], proc=self.proc()
        )
        self.assertTrue(result)
        spawn.assert_called_once_with(background=True)
        activate.assert_called_once()

    def test_the_toggle_waits_longer_than_a_slow_startup(self):
        """A short call that timed out would be answered late all the same."""
        _, _, activate = self.toggle([False, True], proc=self.proc())
        self.assertGreater(activate.call_args.kwargs["timeout_ms"], 10_000)

    def test_an_unanswered_toggle_is_not_sent_again(self):
        """Sent twice, the second would stop what the first started."""
        result, _, activate = self.toggle([True], activated=False)
        self.assertFalse(result)
        activate.assert_called_once()

    def test_a_listener_that_exits_is_given_up_on_at_once(self):
        result, _, activate = self.toggle([False] * 5, proc=self.proc(1))
        self.assertFalse(result)
        activate.assert_not_called()

    def test_a_listener_that_never_appears_is_given_up_on(self):
        with mock.patch.object(control, "START_WAIT_S", 0):
            result, _, activate = self.toggle([False] * 5, proc=self.proc())
        self.assertFalse(result)
        activate.assert_not_called()

    def test_a_listener_that_cannot_be_spawned_is_a_failure(self):
        result, _, activate = self.toggle([False], proc=None)
        self.assertFalse(result)
        activate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
