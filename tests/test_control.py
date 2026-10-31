"""The record key must start a listener that is down, and toggle once."""
from __future__ import annotations

import unittest
from unittest import mock

from steno import control


class TestShowSession(unittest.TestCase):
    def test_a_running_app_is_asked_without_a_launch(self):
        with mock.patch.object(control, "is_running", return_value=True), \
             mock.patch.object(control, "_spawn") as spawn, \
             mock.patch.object(control, "activate", return_value=True) as act:
            self.assertTrue(control.show_session("20261012-100000"))
        spawn.assert_not_called()
        self.assertEqual(act.call_args[0], ("open-session", ["20261012-100000"]))

    def test_a_down_app_is_started_without_a_window_then_asked(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch.object(control, "is_running", side_effect=[False, True]), \
             mock.patch.object(control, "_spawn", return_value=proc) as spawn, \
             mock.patch.object(control, "activate", return_value=True) as act:
            self.assertTrue(control.show_session("s"))
        spawn.assert_called_once_with(background=True)
        act.assert_called_once()

    def test_an_app_that_will_not_start_is_a_failure(self):
        with mock.patch.object(control, "is_running", return_value=False), \
             mock.patch.object(control, "_spawn", return_value=None), \
             mock.patch.object(control, "activate") as act:
            self.assertFalse(control.show_session("s"))
        act.assert_not_called()


class TestPlatformData(unittest.TestCase):
    def test_the_token_goes_by_both_names(self):
        self.assertEqual(
            control.platform_data({"XDG_ACTIVATION_TOKEN": "t"}),
            {"activation-token": "t", "desktop-startup-id": "t"},
        )
        self.assertEqual(
            control.platform_data({"DESKTOP_STARTUP_ID": "x"})["activation-token"],
            "x",
        )

    def test_no_token_no_data(self):
        self.assertEqual(control.platform_data({}), {})


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
