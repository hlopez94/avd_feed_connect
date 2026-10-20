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


def test_unified_mode_preserves_selection_and_duplicate_resource_sessions(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    child = app._child_for('same', a)
    marker = object()
    child._proc = marker
    app._set_view_mode('all')
    drain()
    assert app.current is a
    assert {tile._account.id for tile in app._tiles} == {a.id, b.id}
    assert app._child_for('same', a)._proc is marker
    assert app._child_for('same', b) is not child
    app._set_view_mode('account')
    assert app._tiles == [child]
    assert gui.storage.load_settings()['_account_view'] == 'account'


def test_view_toggle_requires_two_live_accounts(app):
    a = add_saved(app, 'a', 'a@example.com')
    app._select_account(a.id)
    assert not app._view_modes.get_visible()
    b = add_saved(app, 'b', 'b@example.com')
    app._rebuild_account_menu()
    assert app._view_modes.get_visible()
    b.removed = True
    app._rebuild_account_menu()
    assert not app._view_modes.get_visible()


def test_unified_empty_and_failed_account_keep_other_resources(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    b.resources = []
    app._select_account(a.id)
    app._set_view_mode('all')
    app._show('signin', b)
    drain()
    assert app.stack.get_visible_child_name() == 'workspaces'
    assert app._tiles[0]._account is a
    assert b.client.upn in widget_text(app._resource_box)
    assert 'Sign in' in labels(app._resource_box)


def widget_text(widget):
    text = widget.get_text() if isinstance(widget, Gtk.Label) else ''
    child = widget.get_first_child()
    while child:
        text += widget_text(child)
        child = child.get_next_sibling()
    return text


def test_sign_out_clamps_unified_mode_and_keeps_remaining_account(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    app._set_view_mode('all')
    app._sign_out()
    drain()
    assert app.view_mode == 'account'
    assert app.current is b
    assert not app._view_modes.get_visible()
    assert gui.storage.load_settings()['_account_view'] == 'account'


def test_unified_alias_workspace_groups_and_refresh_are_independent(app, monkeypatch):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    a.profile['alias'] = 'Fictional North'
    a.resources += [dict(a.resources[0], id='second', tenant='Second workspace')]
    app._select_account(a.id)
    app._set_view_mode('all')
    app._set_status('Sign in again to refresh', b)
    drain()
    text = widget_text(app._resource_box)
    assert 'Fictional North' in text
    assert 'Second workspace' in text
    assert 'b@example.com' in text
    assert 'Sign in again to refresh' in text
    assert len(app._tiles) == 3
    calls = []
    monkeypatch.setattr(app, '_interactive_signin', lambda owner: calls.append(owner.id))
    row = app._account_sections[b.id].get_first_child().get_next_sibling()
    # Activate the actual affected-account button, not the selected account.
    row.get_last_child().emit('clicked')
    assert calls == [b.id]


@pytest.mark.parametrize('count', [0, 1, 2])
def test_restart_restores_mode_and_selection_or_clamps_to_single_account(app, count):
    owners = [add_saved(app, str(i), f'person{i}@example.com') for i in range(count)]
    if owners:
        app.registry.select(owners[-1].id)
    gui.storage.save_settings({'_account_view': 'all'})
    restarted = gui.AvdApp()
    restarted.set_application_id(gui.APP_ID + "restart")
    restarted.register(None)
    try:
        restarted._build_ui()
        assert restarted.view_mode == ('all' if count > 1 else 'account')
        assert restarted.selected_id == (owners[-1].id if owners else None)
        assert restarted._view_modes.get_visible() == (count > 1)
    finally:
        restarted.win.destroy()


def test_unified_connection_status_does_not_offer_reauth_or_reload_icons(app, monkeypatch):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    app._set_view_mode('all')
    drain()
    icons = []
    monkeypatch.setattr(app, '_load_icons', lambda *args: icons.append(args))
    app._set_status('Connected to Fictional desktop', a)
    drain()
    label, button = app._account_status_widgets[a.id]
    assert label.get_text() == 'Connected to Fictional desktop'
    assert not button.get_visible()
    assert not icons
    app._set_status('Sign in again to refresh', b, reauth=True)
    drain()
    assert app._account_status_widgets[b.id][1].get_visible()
    assert not icons


def test_unified_launch_and_pending_callback_use_resource_owner(app, monkeypatch):
    from types import SimpleNamespace
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    app._set_view_mode('all')
    launched = []
    monkeypatch.setattr(gui.threading, 'Thread', lambda target, args, daemon:
                        SimpleNamespace(start=lambda: target(*args)))
    monkeypatch.setattr(app, '_launch', lambda child, resource, windowed, owner:
                        launched.append((child._account.id, owner.id, resource['id'], windowed)))
    app._on_tile_activated(app.grid, app._child_for('same', b), windowed=True)
    assert launched == [(b.id, b.id, 'same', True)]
    assert app.current is a
    resumed = []
    monkeypatch.setattr(app, '_on_tile_activated', lambda grid, child, windowed:
                        resumed.append((child._account.id, windowed)))
    b.pending_launch = ('same', True)
    app._populate(b.resources, b)
    assert resumed == [(b.id, True)]
    assert b.pending_launch is None


def test_unified_headings_and_mode_buttons_follow_theme(app):
    a, b = add_saved(app, 'a', 'a@example.com'), add_saved(app, 'b', 'b@example.com')
    app._select_account(a.id)
    app._set_view_mode('all')
    app.win.present()
    drain()
    heading = app._account_sections[a.id].get_first_child()
    for theme, dark in [('dark', True), ('light', False)]:
        app._set_theme(theme)
        drain()
        color = heading.get_style_context().get_color()
        assert (color.red > .8) if dark else (color.red < .3)
        assert app._view_buttons['all'].get_active()
        assert not app._view_buttons['account'].get_active()


def test_renaming_other_unified_account_keeps_selected_header_identity(app, monkeypatch):
    a = add_saved(app, 'a', 'alice.north@example.com')
    b = add_saved(app, 'b', 'ben.south@example.com')
    app._select_account(a.id)
    app._set_view_mode('all')
    assert app._avatar_lbl.get_text() == 'AN'
    updates = []
    update = app._update_account
    monkeypatch.setattr(app, '_update_account', lambda who: (updates.append(who), update(who)))
    app._edit_account_name(b)
    dialog = next(w for w in Gtk.Window.list_toplevels()
                  if w.get_title() == 'Edit account name' and w.get_transient_for() is app.win)
    entry = dialog.get_child().get_first_child().get_next_sibling()
    entry.set_text('Fictional South')
    dialog.get_child().get_last_child().get_last_child().emit('clicked')
    assert b.display_name == 'Fictional South'
    assert updates and all(who == a.client.upn for who in updates)
    assert app._avatar_lbl.get_text() == 'AN'
    assert app._acct_who.get_text() == a.display_name


def test_unified_refresh_only_rebuilds_changed_owner_and_fetches_its_icons(app, monkeypatch):
    from types import SimpleNamespace
    owners = [add_saved(app, str(i), f'person{i}@example.com') for i in range(3)]
    monkeypatch.setattr(gui.threading, 'Thread', lambda target, args, daemon:
                        SimpleNamespace(start=lambda: target(*args)))
    app._select_account(owners[0].id)
    app._set_view_mode('all')
    icons = []
    monkeypatch.setattr(app, '_load_icons', lambda resources, owner, tiles: icons.append(owner.id))
    untouched_parent = app._child_for('same', owners[1]).get_parent()
    changed = [dict(owners[0].resources[0], title='Updated resource')]
    app._populate(changed, owners[0])
    assert app._child_for('same', owners[1]).get_parent() is untouched_parent
    assert icons == [owners[0].id]
    icons.clear()
    section = app._account_sections[owners[0].id]
    app._set_status('Sign in again to refresh', owners[0], reauth=True)
    drain()
    app._populate(changed, owners[0])
    assert not app._account_status_widgets[owners[0].id][1].get_visible()
    assert app._account_sections[owners[0].id] is section
    assert app._resource_box.get_first_child() is section
    assert section.get_next_sibling() is app._account_sections[owners[1].id]
    assert not icons
    app._set_view_mode('account')
    app._set_view_mode('all')
    assert not icons
