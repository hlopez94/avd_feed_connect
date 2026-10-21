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
    assert labels(app._account_pop.get_child())[-2:] == ['Add account', 'Sign out']
    text = app._account_rows.get_first_child().get_first_child().get_child()
    assert text.get_first_child().get_text() == '✓ example.com'
    assert text.get_last_child().get_text() == 'a@example.com'


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


def test_account_list_separator_is_hidden_until_accounts_exist(app):
    separator = app._account_rows.get_next_sibling()
    assert isinstance(separator, Gtk.Separator)
    assert not separator.get_visible()
    a = add_saved(app, 'a', 'a@example.com')
    app._select_account(a.id)
    assert separator.get_visible()
    app._sign_out()
    assert not separator.get_visible()


def test_popover_actions_follow_selected_theme(app):
    button = app._account_rows.get_next_sibling().get_next_sibling()
    app._set_theme('dark')
    drain()
    dark = button.get_style_context().get_color()
    assert dark.red > 0.8 and dark.green > 0.8
    app._set_theme('light')
    drain()
    light = button.get_style_context().get_color()
    assert light.red < 0.3 and light.green < 0.3


def test_settings_menu_has_no_trailing_separator(app):
    assert isinstance(app._settings_pop.get_child().get_last_child(), Gtk.Button)


@pytest.mark.parametrize('per_resource', [False, True])
def test_connection_settings_follow_theme(app, per_resource):
    owner = add_saved(app, 'a', 'a@example.com')
    app._select_account(owner.id)
    app._set_theme('dark')
    app._open_settings(owner.resources[0] if per_resource else None, owner)
    dialog = next(w for w in Gtk.Window.list_toplevels()
                  if w.get_transient_for() is app.win and w.get_modal())
    try:
        label = dialog.get_child().get_first_child().get_first_child()
        drain()
        dark = label.get_style_context().get_color()
        assert dark.red > 0.8 and dark.green > 0.8
        app._set_theme('light')
        drain()
        light = label.get_style_context().get_color()
        assert light.red < 0.3 and light.green < 0.3
    finally:
        dialog.destroy()


def test_account_name_editor_saves_and_restores_detected_name(app):
    owner = add_saved(app, 'a', 'a@example.com')
    app._select_account(owner.id)
    for value in ('Custom company', ''):
        app._edit_account_name(owner)
        dialog = next(w for w in Gtk.Window.list_toplevels()
                      if w.get_title() == 'Edit account name' and w.get_transient_for() is app.win)
        entry = dialog.get_child().get_first_child().get_next_sibling()
        entry.set_text(value)
        dialog.get_child().get_last_child().get_last_child().emit('clicked')
        assert owner.display_name == (value or 'example.com')
        text = app._account_rows.get_first_child().get_first_child().get_child()
        assert text.get_first_child().get_text() == '✓ ' + (value or 'example.com')
        assert text.get_last_child().get_text() == 'a@example.com'


def test_background_workspace_name_does_not_replace_account_domain(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    b.resources = [dict(b.resources[0], tenant='Updated company')]
    app._populate(b.resources, b)
    text = app._account_rows.get_last_child().get_first_child().get_child()
    assert text.get_first_child().get_text() == 'example.com'
    assert app.current is a


def test_header_displays_alias_and_keeps_email_in_selector(app):
    owner = add_saved(app, 'a', 'a@example.com')
    owner.profile['alias'] = 'Tessitura Network'
    app._select_account(owner.id)
    assert app._acct_who.get_text() == 'Tessitura Network'
    text = app._account_rows.get_first_child().get_first_child().get_child()
    assert text.get_last_child().get_text() == 'a@example.com'


@pytest.mark.parametrize('menu_name', ['_account_pop', '_settings_pop'])
def test_menu_closes_when_parent_window_loses_activation(app, menu_name, monkeypatch):
    app.win.present()
    drain()
    popover = getattr(app, menu_name)
    # Disable GTK's outside-click dismissal to exercise the independent
    # activation cleanup needed when a touch interaction misses dismissal.
    popover.set_autohide(False)
    popover.popup()
    drain()
    assert popover.get_visible()
    monkeypatch.setattr(app.win, 'is_active', lambda: False)
    app.win.notify('is-active')
    drain()
    assert not popover.get_visible()


@pytest.mark.parametrize('menu_name', ['_account_pop', '_settings_pop'])
def test_menu_stays_open_while_parent_window_is_active(app, menu_name, monkeypatch):
    app.win.present()
    drain()
    popover = getattr(app, menu_name)
    popover.set_autohide(False)
    popover.popup()
    drain()
    monkeypatch.setattr(app.win, 'is_active', lambda: True)
    app.win.notify('is-active')
    drain()
    assert popover.get_visible()


@pytest.mark.parametrize('menu_name', ['_account_pop', '_settings_pop'])
def test_menu_does_not_take_an_autohide_input_grab(app, menu_name):
    assert not getattr(app, menu_name).get_autohide()


@pytest.mark.parametrize('menu_name', ['_account_pop', '_settings_pop'])
def test_touch_on_main_content_dismisses_open_menu(app, menu_name):
    app.win.present()
    drain()
    popover = getattr(app, menu_name)
    popover.set_autohide(False)
    popover.popup()
    drain()
    assert popover.get_visible()
    app._on_menu_outside_pressed(None, 1, 20, app.win.get_height() - 20)
    drain()
    assert not popover.get_visible()


def test_repeated_gui_connections_preserve_each_log(app, tmp_path, monkeypatch):
    from pathlib import Path
    a = add_saved(app, 'a', 'a@example.com')
    app._select_account(a.id)
    resource = a.resources[0]
    child = app._child_for(resource['id'], a)
    output = Path(a.client.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    legacy = output / 'session_previous.log'
    legacy.write_bytes(b'previous session\n')
    rdp = tmp_path / 'desktop.rdp'
    client = tmp_path / 'fake-freerdp'
    client.write_text('#!/usr/bin/python3\n'
                      'import pathlib, sys\n'
                      'print(pathlib.Path(sys.argv[1]).read_text(), end="", flush=True)\n')
    client.chmod(0o755)
    monkeypatch.setattr(gui.config, 'SDL', str(client))
    monkeypatch.setattr(gui, 'apply_client_hotkeys', lambda mode: None)
    monkeypatch.setattr(app, '_ensure_token', lambda session: True)
    monkeypatch.setattr(a.client, 'download_rdp', lambda token, res: str(rdp))

    rdp.write_text('first connection\n')
    app._launch(child, resource, False, a)
    first = {p: p.read_bytes() for p in output.glob('session_*.log')}
    rdp.write_text('second connection\n')
    app._launch(child, resource, False, a)
    assert len(list(output.glob('session_*.log'))) == 3
    assert all(p.read_bytes() == content for p, content in first.items())
    assert legacy.read_bytes() == b'previous session\n'
    logs = [p.read_bytes() for p in output.glob('session_*.log') if p != legacy]
    assert any(b'first connection' in content for content in logs)
    assert any(b'second connection' in content for content in logs)
    drain()


def test_workspace_defaults_and_resource_off_persist(app, monkeypatch):
    monkeypatch.setenv('XDG_CURRENT_DESKTOP', 'GNOME')
    owner = add_saved(app, 'demo', 'alex@example.test')
    app.selected_id = owner.id
    resource = owner.resources[0]
    app._settings['_default'] = {'new_workspace': 'on'}
    assert app._eff('new_workspace', resource, owner) == 'on'
    app._open_settings(resource, owner)
    drain()
    dialog = next(w for w in Gtk.Window.get_toplevels() if w.get_title().startswith('Settings —'))
    widgets = []
    def visit(widget):
        widgets.append(widget)
        child = widget.get_first_child()
        while child:
            visit(child)
            child = child.get_next_sibling()
    visit(dialog)
    dropdown = next(w for w in widgets if isinstance(w, Gtk.DropDown)
                    and w.get_model().get_string(0) == 'Use Default settings')
    assert dropdown.get_selected() == 0
    dropdown.set_selected(2)
    next(w for w in widgets if isinstance(w, Gtk.Button) and w.get_label() == 'Save').emit('clicked')
    assert owner.resource_settings[resource['id']]['new_workspace'] == 'off'
    assert app._eff('new_workspace', resource, owner) == 'off'


def test_workspace_placement_only_on_first_logon(app):
    from types import SimpleNamespace
    from unittest.mock import Mock
    placement = Mock()
    placement.start.return_value = False
    child = SimpleNamespace(_workspace_placement=placement)
    owner = add_saved(app, 'demo', 'alex@example.test')
    resource = owner.resources[0]
    state = {'connected': False}
    app._on_freerdp_line(child, resource, -1, 'Connection failed', state, owner)
    drain()
    placement.start.assert_not_called()
    for _ in range(2):
        app._on_freerdp_line(child, resource, -1, 'Logon Info', state, owner)
    drain()
    placement.start.assert_called_once()


@pytest.mark.parametrize('resource_settings', [False, True])
def test_workspace_setting_hidden_outside_gnome(app, monkeypatch, resource_settings):
    monkeypatch.setenv('XDG_CURRENT_DESKTOP', 'KDE')
    monkeypatch.setenv('XDG_SESSION_DESKTOP', 'gnome')
    owner = add_saved(app, 'demo', 'alex@example.test')
    app.selected_id = owner.id
    resource = owner.resources[0]
    app._settings['_default'] = {'new_workspace': 'on'}
    assert app._eff('new_workspace', resource, owner) == 'on'
    assert gui.workspace_backend() is None
    app._open_settings(resource if resource_settings else None, owner)
    drain()
    dialog = next(w for w in Gtk.Window.get_toplevels() if w != app.win)
    labels = []
    def visit(w):
        if isinstance(w, Gtk.Label):
            labels.append(w)
        child = w.get_first_child()
        while child:
            visit(child)
            child = child.get_next_sibling()
    visit(dialog)
    label = next(w for w in labels if w.get_text() == 'Start connection in a new virtual desktop')
    assert not label.get_parent().get_visible()
    note = next(w for w in labels if 'Requires the optional GNOME extension' in w.get_text())
    assert not note.get_visible()
    dialog.destroy()
