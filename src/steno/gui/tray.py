"""The tray icon: a StatusNotifierItem on the session bus.

GTK4 has no tray API, and libayatana-appindicator is GTK3-only, so it cannot load
into this process. Its GTK-free successor (libayatana-appindicator-glib) exports
its menu as `org.gtk.Menus`, which waybar, KDE, XFCE and GNOME's extension do not
read. So the item itself is exported here over Gio — a handful of properties and
two signals — and the menu goes through libdbusmenu-glib, which is GTK-free and
speaks the `com.canonical.dbusmenu` every tray host does.

Every entry activates an app action, the same ones the bar and `steno toggle`
use. A missing library or a desktop with no tray is not an error: the app runs
exactly as before, and the waybar module and notifications still work.
"""
from __future__ import annotations

import os
from typing import Any

from gi.repository import Gio, GLib

from .. import icon_dir
from ..state import PAUSE_CHOICES, tray_view

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"

INTERFACE = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
    <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewTitle"/>
    <signal name="NewStatus"><arg type="s"/></signal>
  </interface>
</node>
"""


def _dbusmenu():
    try:
        import gi

        gi.require_version("Dbusmenu", "0.4")
        from gi.repository import (
            Dbusmenu,  # pyright: ignore[reportAttributeAccessIssue]
        )
    except (ImportError, ValueError):
        return None
    return Dbusmenu


class Tray:
    """Owns the item, its menu, and its registration with the host."""

    def __init__(self, app) -> None:
        self.app = app
        self.view = tray_view("idle")
        self.conn: Gio.DBusConnection | None = None
        self._registration = 0
        self._name_id = 0
        self._watch_id = 0
        self._server: Any = None
        self._dbusmenu: Any = None
        self._items: dict[str, Any] = {}

    def start(self) -> bool:
        """Export and register. False if there is nothing to put a tray on."""
        Dbusmenu = _dbusmenu()
        conn = self.app.get_dbus_connection()
        if Dbusmenu is None or conn is None:
            return False
        self.conn = conn
        info = Gio.DBusNodeInfo.new_for_xml(INTERFACE).interfaces[0]
        self._registration = conn.register_object(
            ITEM_PATH, info, self._on_call, self._on_get, None
        )
        self._build_menu(Dbusmenu)
        # The spec's well-known name, so a host that restarts can find us again.
        name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._name_id = Gio.bus_own_name_on_connection(
            conn, name, Gio.BusNameOwnerFlags.NONE, None, None
        )
        # The host comes and goes with the bar; register every time it appears.
        self._watch_id = Gio.bus_watch_name_on_connection(
            conn, WATCHER, Gio.BusNameWatcherFlags.NONE,
            lambda c, _n, _o: self._register(c, name), None,
        )
        return True

    def stop(self) -> None:
        if self.conn is None:
            return
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
        if self._name_id:
            Gio.bus_unown_name(self._name_id)
        if self._registration:
            self.conn.unregister_object(self._registration)
        self._server = None
        self.conn = None

    def _register(self, conn: Gio.DBusConnection, name: str) -> None:
        conn.call(
            WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (name,)), None, Gio.DBusCallFlags.NONE, -1, None,
            None,
        )

    # ------------------------------------------------------------------- state

    def update(self, status: str, detail: str = "") -> None:
        old, self.view = self.view, tray_view(status, detail)
        if self.conn is None:
            return
        if old["icon"] != self.view["icon"]:
            self._emit("NewIcon")
        if old["tooltip"] != self.view["tooltip"]:
            self._emit("NewToolTip")
        self._sync_menu()

    def _emit(self, signal: str, params: GLib.Variant | None = None) -> None:
        if self.conn is None:
            return
        self.conn.emit_signal(
            None, ITEM_PATH, "org.kde.StatusNotifierItem", signal, params
        )

    # ------------------------------------------------------------------ D-Bus

    def _on_get(self, _conn, _sender, _path, _iface, prop: str):
        view = self.view
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "steno"),
            "Title": GLib.Variant("s", "Steno"),
            "Status": GLib.Variant("s", "Active"),
            "IconName": GLib.Variant("s", view["icon"]),
            "IconThemePath": GLib.Variant("s", str(icon_dir())),
            "ToolTip": GLib.Variant(
                "(sa(iiay)ss)", (view["icon"], [], "Steno", view["tooltip"])
            ),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(prop)

    def _on_call(self, _conn, _sender, _path, _iface, method: str, _params, invocation):
        if method == "Activate":
            self.app.activate_action("show", None)
        elif method == "SecondaryActivate":
            self.app.activate_action("toggle-record", None)
        invocation.return_value(None)

    # ------------------------------------------------------------------- menu

    def _build_menu(self, Dbusmenu) -> None:
        def item(key: str, label: str = "", action: str | None = None, param=None):
            node = Dbusmenu.Menuitem.new()
            if label:
                node.property_set(Dbusmenu.MENUITEM_PROP_LABEL, label)
            else:
                node.property_set(Dbusmenu.MENUITEM_PROP_TYPE, "separator")
            if action is not None:
                node.connect(
                    Dbusmenu.MENUITEM_SIGNAL_ITEM_ACTIVATED,
                    lambda *_a: self._activate(action, param),
                )
            self._items[key] = node
            return node

        root = Dbusmenu.Menuitem.new()
        status = item("status", self.view["tooltip"])
        status.property_set_bool(Dbusmenu.MENUITEM_PROP_ENABLED, False)
        root.child_append(status)
        root.child_append(item("sep1"))
        root.child_append(item("show", "Open Steno", "show"))
        root.child_append(item("record", "Start recording", "toggle-record"))
        pause = item("pause", "Pause listening")
        pause.property_set(
            Dbusmenu.MENUITEM_PROP_CHILD_DISPLAY, Dbusmenu.MENUITEM_CHILD_DISPLAY_SUBMENU
        )
        for minutes, label in PAUSE_CHOICES:
            pause.child_append(
                item(f"pause-{minutes}", label, "pause", GLib.Variant("i", minutes))
            )
        root.child_append(pause)
        root.child_append(item("unpause", "Resume listening", "unpause"))
        root.child_append(item("sep2"))
        root.child_append(item("quit", "Quit, stop listening", "quit"))

        self._dbusmenu = Dbusmenu
        self._server = Dbusmenu.Server.new(MENU_PATH)
        self._server.set_root(root)
        self._sync_menu()

    def _sync_menu(self) -> None:
        if self._server is None:
            return
        Dbusmenu, view, items = self._dbusmenu, self.view, self._items
        items["status"].property_set(Dbusmenu.MENUITEM_PROP_LABEL, view["tooltip"])
        items["record"].property_set(Dbusmenu.MENUITEM_PROP_LABEL, view["record_label"])
        items["record"].property_set_bool(
            Dbusmenu.MENUITEM_PROP_ENABLED, view["can_record"]
        )
        items["pause"].property_set_bool(Dbusmenu.MENUITEM_PROP_VISIBLE, not view["paused"])
        items["unpause"].property_set_bool(Dbusmenu.MENUITEM_PROP_VISIBLE, view["paused"])

    def _activate(self, action: str, param: GLib.Variant | None) -> None:
        # Quitting mid-meeting has to ask, and the question lives in the window.
        if action == "quit" and self.view["recording"]:
            self.app.activate_action("show", None)
        self.app.activate_action(action, param)
