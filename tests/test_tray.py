"""What the tray item tells a host over D-Bus, without a bus.

Quickshell's bar (the owner's tray host) shows an item's tooltip title,
else its Title, and lights an item, or the chevron of its shut tray
drawer, only while the item's Status is NeedsAttention.
"""
from __future__ import annotations

import unittest

from steno.gui.tray import Tray


class FakeConn:
    def __init__(self) -> None:
        self.signals: list[tuple[str, object]] = []

    def emit_signal(self, _dest, _path, _iface, name, params) -> None:
        self.signals.append(
            (name, params.unpack() if params is not None else None))


def prop(tray: Tray, name: str):
    return tray._on_get(None, None, None, None, name).unpack()


class TestTrayItem(unittest.TestCase):
    def test_a_test_build_names_itself_where_the_host_looks(self):
        tray = Tray(app=None, src="feat/x@1")
        self.assertEqual(prop(tray, "Title"), "Steno · test build feat/x@1")
        _icon, _pixmaps, title, text = prop(tray, "ToolTip")
        self.assertEqual(title, "Steno · test build feat/x@1")
        self.assertEqual(text, "Listening for a meeting")
        self.assertEqual(prop(tray, "Status"), "NeedsAttention")

    def test_asking_for_attention_keeps_an_icon_to_draw(self):
        # Without AttentionIconName, Quickshell draws a NeedsAttention
        # item from pixmaps, and steno sends none.
        tray = Tray(app=None, src="feat/x@1")
        self.assertEqual(prop(tray, "AttentionIconName"),
                         prop(tray, "IconName"))

    def test_the_installed_build_is_the_item_it_always_was(self):
        tray = Tray(app=None)
        self.assertEqual(prop(tray, "Title"), "Steno")
        self.assertEqual(prop(tray, "Status"), "Active")
        self.assertEqual(prop(tray, "ToolTip")[2], "Steno")

    def test_a_new_icon_is_also_the_new_attention_icon(self):
        tray = Tray(app=None, src="feat/x@1")
        tray.conn = FakeConn()
        tray.update("recording", "Zoom")
        names = [name for name, _ in tray.conn.signals]
        self.assertIn("NewIcon", names)
        self.assertIn("NewAttentionIcon", names)
        self.assertIn("NewToolTip", names)
        # The build does not change while the app runs.
        self.assertNotIn("NewStatus", names)
        self.assertNotIn("NewTitle", names)


if __name__ == "__main__":
    unittest.main()
