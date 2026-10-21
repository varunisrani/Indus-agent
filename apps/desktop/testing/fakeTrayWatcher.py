"""A stand-in StatusNotifier watcher with a host for the tray end-to-end test (P20-40).

Linux desktops show tray icons through `org.kde.StatusNotifierWatcher`; the desktop app only
offers "minimise to tray" when a host is registered there (P20-36). This program owns that name
on the session bus it is started on, answers `IsStatusNotifierHostRegistered = true`, accepts
item registrations (printing each one) and prints `ready` once the name is owned. Stopping it
(SIGTERM) is how the test makes the host go away. Runs with the system Python's GObject
bindings (`gi`); nothing here is product code.
"""

import signal
import sys

from gi.repository import Gio, GLib  # type: ignore[import-not-found]

NAME = "org.kde.StatusNotifierWatcher"
PATH = "/StatusNotifierWatcher"
XML = f"""
<node>
  <interface name="{NAME}">
    <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
    <method name="RegisterStatusNotifierHost"><arg type="s" direction="in"/></method>
    <property name="RegisteredStatusNotifierItems" type="as" access="read"/>
    <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
    <property name="ProtocolVersion" type="i" access="read"/>
    <signal name="StatusNotifierItemRegistered"><arg type="s"/></signal>
    <signal name="StatusNotifierItemUnregistered"><arg type="s"/></signal>
    <signal name="StatusNotifierHostRegistered"/>
  </interface>
</node>
"""

items: list[str] = []


def on_call(conn, sender, _path, _iface, method, params, invocation):  # noqa: ANN001, PLR0913
    if method == "RegisterStatusNotifierItem":
        (service,) = params.unpack()
        item = f"{sender}{service}" if service.startswith("/") else service
        items.append(item)
        print(f"item {item}", flush=True)
        signal_args = GLib.Variant("(s)", (item,))
        conn.emit_signal(None, PATH, NAME, "StatusNotifierItemRegistered", signal_args)
    invocation.return_value(None)


def on_property(_conn, _sender, _path, _iface, prop):  # noqa: ANN001
    if prop == "IsStatusNotifierHostRegistered":
        return GLib.Variant("b", True)
    if prop == "ProtocolVersion":
        return GLib.Variant("i", 0)
    if prop == "RegisteredStatusNotifierItems":
        return GLib.Variant("as", items)
    return None


def main() -> int:
    loop = GLib.MainLoop()
    info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]

    def acquired_bus(conn, _name):  # noqa: ANN001
        conn.register_object(PATH, info, on_call, on_property, None)

    def acquired_name(_conn, _name):  # noqa: ANN001
        print("ready", flush=True)

    def lost(_conn, _name):  # noqa: ANN001
        print("lost", flush=True)
        loop.quit()

    Gio.bus_own_name(
        Gio.BusType.SESSION, NAME, Gio.BusNameOwnerFlags.NONE, acquired_bus, acquired_name, lost
    )
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, loop.quit)
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
