"""Optional GNOME Shell bridge. No Shell Eval or X11-only workspace hacks."""
from gi.repository import Gio, GLib

BUS_NAME = "io.github.shakeelosmani.AvdWorkspaces"
OBJECT_PATH = "/io/github/shakeelosmani/AvdWorkspaces"


def available():
    """Check the optional bridge before changing the session window title."""
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result = connection.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", (BUS_NAME,)),
            GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NO_AUTO_START, 1000, None)
        return result.unpack()[0]
    except GLib.Error:
        return False


class WorkspacePlacement:
    """One bounded, asynchronous placement attempt per successful launch."""

    def __init__(self, proc, title):
        self.proc = proc
        self.title = title
        self.started = False
        self.attempts = 0

    def start(self):
        if not self.started:
            self.started = True
            self._try_place()
        return False

    def _try_place(self):
        if self.proc.poll() is not None or self.attempts >= 60:
            return False
        self.attempts += 1
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            connection.call(
                BUS_NAME, OBJECT_PATH, BUS_NAME, "PlaceWindow",
                GLib.Variant("(s)", (self.title,)), GLib.VariantType.new("(b)"),
                Gio.DBusCallFlags.NO_AUTO_START, 1000, None, self._finished)
        except GLib.Error:
            pass  # Extension absent, another desktop, or sandbox permission missing.
        return False

    def _finished(self, connection, result):
        try:
            connection.call_finish(result).unpack()
        except GLib.Error:
            return  # Missing/disabled extension is an ordinary launch.
        # Keep a bounded grace period after first placement for other monitor
        # windows mapping later. Shell placement is idempotent and does not
        # activate the workspace again unless a new session window appears.
        if self.proc.poll() is None and self.attempts < 60:
            GLib.timeout_add(250, self._try_place)
