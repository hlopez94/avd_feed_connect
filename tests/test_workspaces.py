"""Bounded placement and graceful fallback without a real Shell extension."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
pytest.importorskip('gi')
from gi.repository import GLib
from avd_feed_connect.gui import workspaces


def placement():
    return workspaces.WorkspacePlacement(SimpleNamespace(poll=lambda: None), 'AVD-' + 'a' * 32)


def test_start_only_once(monkeypatch):
    value = placement()
    monkeypatch.setattr(value, '_try_place', Mock())
    value.start()
    value.start()
    value._try_place.assert_called_once()


def test_exited_and_exhausted_never_call_bus(monkeypatch):
    bus = Mock()
    monkeypatch.setattr(workspaces.Gio, 'bus_get_sync', bus)
    value = placement()
    value.proc.poll = lambda: 1
    value.start()
    value.proc.poll = lambda: None
    value.attempts = 60
    value._try_place()
    bus.assert_not_called()


def test_absent_extension_does_not_retry(monkeypatch):
    value = placement()
    timer = Mock()
    monkeypatch.setattr(workspaces.GLib, 'timeout_add', timer)
    connection = Mock()
    connection.call_finish.side_effect = GLib.Error('No service')
    value._finished(connection, object())
    timer.assert_not_called()


def test_window_map_race_and_late_monitor_grace(monkeypatch):
    value = placement()
    timer = Mock()
    monkeypatch.setattr(workspaces.GLib, 'timeout_add', timer)
    connection = Mock()
    connection.call_finish.return_value = GLib.Variant('(b)', (False,))
    value._finished(connection, object())
    timer.assert_called_once_with(250, value._try_place)
    timer.reset_mock()
    connection.call_finish.return_value = GLib.Variant('(b)', (True,))
    value._finished(connection, object())
    timer.assert_called_once_with(250, value._try_place)
    timer.reset_mock()
    value.attempts = 60
    value._finished(connection, object())
    timer.assert_not_called()


def test_availability_requires_running_bridge(monkeypatch):
    connection = Mock()
    monkeypatch.setattr(workspaces.Gio, 'bus_get_sync', lambda *args: connection)
    for present in (False, True):
        connection.call_sync.return_value = GLib.Variant('(b)', (present,))
        assert workspaces.available() is present
    connection.call_sync.side_effect = GLib.Error('Permission denied')
    assert workspaces.available() is False
