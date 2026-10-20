"""Exercise account flows with real GTK widgets, without network or credentials."""
import base64
import json
import os

import pytest

pytest.importorskip('gi')
import gi
try:
    gi.require_version('Gtk', '4.0')
    gi.require_version('WebKit', '6.0')
except ValueError:
    pytest.skip('GTK/WebKit typelibs are unavailable', allow_module_level=True)
from gi.repository import Gtk, GLib
if not Gtk.init_check():
    pytest.skip('GTK display is unavailable', allow_module_level=True)

import avd_feed_gui as gui
from avd_feed_connect.accounts import profile_dir
from avd_feed_connect.gui.session import AccountSession


def drain():
    context = GLib.MainContext.default()
    for _ in range(200):
        if not context.pending():
            break
        context.iteration(False)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('AVD_DEMO', '1')
    monkeypatch.setattr(gui.config, 'CACHE', str(tmp_path / 'token-cache.json'))
    monkeypatch.setattr(gui.storage, 'SETTINGS_FILE', str(tmp_path / 'settings.json'))
    monkeypatch.setattr(gui.TokenStore, '_keyring_store', lambda *a: False)
    monkeypatch.setattr(gui.TokenStore, '_keyring_load', lambda *a: None)
    monkeypatch.setattr(gui.TokenStore, '_keyring_clear', lambda *a: None)
    import uuid
    monkeypatch.setattr(gui, "APP_ID", "io.github.avdfeed.test" + uuid.uuid4().hex)
    value = gui.AvdApp()
    value.register(None)
    value._build_ui()
    monkeypatch.setattr(value, '_background_refresh', lambda *a: None)
    monkeypatch.setattr(value, '_load_icons', lambda *a: None)
    monkeypatch.setattr(value, '_error', lambda message: pytest.fail(message))
    yield value
    if value._signin_dlg:
        value._signin_dlg.destroy()
    for account in value.accounts.values():
        if account.refresh_source:
            GLib.source_remove(account.refresh_source)
    value.win.destroy()
    drain()


def add_saved(app, tenant, email):
    profile = app.registry.upsert({'tid': tenant, 'oid': 'user', 'upn': email})
    owner = AccountSession(profile, app.registry.client(profile['id']), profile_dir(app.registry.root, profile['id']))
    owner.resources = [{'id': 'same', 'title': email, 'tenant': tenant, 'type': 'Desktop'}]
    app.accounts[owner.id] = owner
    return owner


def response(tenant, email):
    claims = {'tid': tenant, 'oid': 'user', 'preferred_username': email}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    return {'id_token': 'x.' + encoded + '.x', 'access_token': 'access', 'refresh_token': 'secret'}


def labels(widget):
    result = []
    child = widget.get_first_child()
    while child:
        if isinstance(child, Gtk.Button):
            result.append(child.get_label())
        result.extend(labels(child))
        child = child.get_next_sibling()
    return result


def test_account_popover_contains_only_accounts_and_auth_actions(app):
    a = add_saved(app, 'a', 'a@example.com')
    app._select_account(a.id)
    drain()
    assert labels(app._account_pop.get_child()) == ['✓ a@example.com', 'Add account', 'Sign out']


def test_switch_and_late_results_preserve_owner_and_process(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    child = app._child_for('same', a)
    marker = object()
    child._proc = marker
    app._select_account(b.id)
    app._populate(a.resources, a)
    app._set_status('late account a status', a)
    drain()
    assert app.current is b
    assert app._tiles[0]._res['title'] == 'b@example.com'
    assert app._child_for('same', a)._proc is marker
    app._select_account(a.id)
    assert app._tiles[0] is child


def test_cancel_add_keeps_previous_selection(app, monkeypatch):
    a = add_saved(app, 'a', 'a@example.com')
    app._select_account(a.id)
    monkeypatch.setattr(app, '_interactive_signin', lambda owner: None)
    app._add_account()
    pending = app._adding
    assert pending is not a
    app._discard_add(pending)
    assert app.current is a
    assert len(app.registry.profiles) == 1
    assert not os.path.exists(pending.directory)


def test_duplicate_add_reuses_profile(app, monkeypatch):
    a = add_saved(app, 'a', 'a@example.com')
    app._select_account(a.id)
    monkeypatch.setattr(app, '_interactive_signin', lambda owner: None)
    app._add_account()
    app._finish_signin(app._adding, response('a', 'a@example.com'))
    assert app.current is a
    assert len(app.registry.profiles) == 1
    assert app._adding is None
    assert a.client.load_token_record()['refresh_token'] == 'secret'


def test_sign_out_removes_only_selected_account(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    a.client.save_refresh_token('a')
    b.client.save_refresh_token('b')
    app._select_account(a.id)
    app._sign_out()
    drain()
    assert a.removed
    assert a.client.load_token_record() is None
    assert b.client.load_token_record()['refresh_token'] == 'b'
    assert app.current is b
    assert app._tiles[0]._account is b


def test_resource_preferences_are_account_local(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._settings['_default'] = {'scale': '125'}
    a.resource_settings['same'] = {'scale': '150'}
    assert app._eff('scale', a.resources[0], a) == '150'
    assert app._eff('scale', b.resources[0], b) == '125'


def test_tray_connects_unselected_saved_account_without_switching(app, monkeypatch):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    activated = []
    monkeypatch.setattr(app, '_on_tile_activated', lambda grid, child, windowed=False: activated.append(child._account.id))
    app._on_tray_action(('connect', b.id, 'same'))
    assert activated == [b.id]
    assert app.current is a


def test_settings_popover_keeps_general_options_separate(app):
    options = labels(app._settings_pop.get_child())
    assert 'Default settings…' in options
    assert 'Sign out' not in options
    assert 'Add account' not in options


def test_sign_out_does_not_break_reader_when_log_directory_disappears(app):
    from types import SimpleNamespace
    a = add_saved(app, 'a', 'a@example.com')
    app._select_account(a.id)
    child = app._child_for('same', a)
    child._launching = True
    app._sign_out()
    master, slave = os.pipe()
    os.close(slave)
    app._watch_session(child, a.resources[0], SimpleNamespace(poll=lambda: 0), master,
                       os.path.join(a.directory, 'feed', 'removed.log'), a)
    drain()
    assert child._launching is False
    with pytest.raises(OSError):
        os.read(master, 1)
