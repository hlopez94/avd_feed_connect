"""Optional GNOME Shell bridge. No Shell Eval or X11-only workspace hacks."""
import os
from typing import Protocol

from gi.repository import Gio, GLib

BUS_NAME = "io.github.shakeelosmani.AvdWorkspaces"
OBJECT_PATH = "/io/github/shakeelosmani/AvdWorkspaces"


def _bridge_available():
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


class WorkspaceBackend(Protocol):
    """The capabilities a desktop-specific workspace implementation supplies."""

    def available(self) -> bool: ...

    def create_placement(self, proc, title): ...


class GnomeWorkspaceBackend:
    def available(self):
        return _bridge_available()

    def create_placement(self, proc, title):
        return WorkspacePlacement(proc, title)


def current_desktop_is_gnome(environ=None):
    """Honor the active desktop list before considering session fallbacks."""
    environ = os.environ if environ is None else environ
    desktop = environ.get("XDG_CURRENT_DESKTOP", "").strip()
    if not desktop:
        desktop = environ.get("XDG_SESSION_DESKTOP", "").strip()
    if not desktop:
        desktop = environ.get("DESKTOP_SESSION", "").strip()
    # GNOME sessions include ubuntu:GNOME, GNOME-Classic and gnome-xorg.
    return any(token == "gnome" or token.startswith("gnome-")
               for token in desktop.lower().split(":"))


def select_backend(environ=None) -> WorkspaceBackend | None:
    """Select only implemented desktop integrations; never assume Wayland support."""
    return GnomeWorkspaceBackend() if current_desktop_is_gnome(environ) else None


def available():
    backend = select_backend()
    return backend is not None and backend.available()
