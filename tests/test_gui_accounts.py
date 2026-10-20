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



def test_language_selection_applies_live_and_preserves_sessions(app):
    from avd_feed_connect import i18n
    def descendants(widget):
        yield widget
        child = widget.get_first_child()
        while child:
            yield from descendants(child)
            child = child.get_next_sibling()
    widgets = list(descendants(app.win))
    dropdown = next(w for w in widgets if isinstance(w, Gtk.DropDown)
                    and w.get_model().get_string(0) == 'System default')
    owner = add_saved(app, 'fictional', 'alex@example.test')
    app._select_account(owner.id)
    owner.resources = [{'id': 'desk', 'title': 'Save', 'tenant': 'Northstar', 'type': 'Desktop'}]
    app._populate(owner.resources, owner)
    tile = owner.tiles['desk']
    from types import SimpleNamespace
    process = SimpleNamespace(poll=lambda: None)
    tile._proc = process
    tray = SimpleNamespace()
    tray.set_menu = lambda menu: setattr(tray, "menu", menu)
    tray.set_tooltip = lambda tooltip: setattr(tray, "tooltip", tooltip)
    app._tray = tray
    app._sessions[(owner.id, "desk")] = "connected"
    app._set_status(i18n._('Connected to {resource}').format(resource='Save'), owner)
    app._open_settings(None)
    dialog = next(w for w in Gtk.Window.get_toplevels()
                  if w.get_title() == 'Default connection settings')
    connection_dropdown = next(w for w in descendants(dialog) if isinstance(w, Gtk.DropDown))
    connection_dropdown.set_selected(3)
    unsaved_flags = next(w for w in descendants(dialog) if isinstance(w, Gtk.Entry)
                         and w.get_placeholder_text().startswith("advanced:"))
    unsaved_flags.set_text("/gfx /network:auto")
    app._edit_account_name(owner)
    alias_dialog = next(w for w in Gtk.Window.get_toplevels() if w.get_title() == "Edit account name")
    alias_entry = next(w for w in descendants(alias_dialog) if isinstance(w, Gtk.Entry))
    alias_entry.set_text("Unsaved company name")
    for selection, language, expected in [(2, 'es-419', 'Conectado a Save'),
                                           (3, 'es-ES', 'Conectado a Save'),
                                           (4, 'es-AR', 'Conectado a Save'),
                                           (1, 'en', 'Connected to Save')]:
        dropdown.set_selected(selection)
        drain()
        assert gui.storage.load_settings()['_language'] == language
        assert app.status.get_text() == expected
        assert owner.tiles['desk'] is tile and tile._proc is process
        assert app.current is owner
        assert tray.menu[-1]['label'] == ('Quit' if language == 'en' else 'Salir')
        assert str(tray.tooltip) == ('1 session connected' if language == 'en' else '1 sesión conectada')
        assert app._sessions[(owner.id, 'desk')] == 'connected'
        assert connection_dropdown.get_selected() == 3
        assert unsaved_flags.get_text() == '/gfx /network:auto'
        assert alias_entry.get_text() == 'Unsaved company name'
        assert alias_dialog.get_title() == ('Edit account name' if language == 'en' else 'Editar nombre de la cuenta')
        assert any(isinstance(w, Gtk.Label) and w.get_text() == 'Save'
                   for w in descendants(tile))  # resource title must never be translated
        assert dialog.get_title() == ('Default connection settings' if language == 'en'
                                      else 'Configuración de conexión predeterminada')
        assert any(isinstance(w, Gtk.Button) and w.get_label() ==
                   ('Save' if language == 'en' else 'Guardar') for w in descendants(dialog))
    alias_dialog.destroy()
    dialog.destroy()
    tile._proc = None
    app._tray = None


def test_tile_state_labels_are_derived_from_state_and_translate_live(app):
    owner = add_saved(app, 'demo', 'alex@example.test')
    app._select_account(owner.id)
    tile = owner.tiles['same']
    app._set_tile_state('same', 'state-connecting', owner)
    drain()
    assert tile._pill.get_visible() and tile._pill.get_text() == 'CONNECTING'
    app._set_language('es-AR')
    assert tile._pill.get_text() == 'CONECTANDO'
    app._set_tile_state('same', 'state-connected', owner)
    drain()
    assert tile._pill.get_text() == 'CONECTADO'
    assert tile._tile.has_css_class('tile-connected')
    assert app._sessions[(owner.id, 'same')] == 'connected'
    app._set_tile_state('same', 'state-ended', owner)
    drain()
    assert not tile._pill.get_visible()
    assert not tile._tile.has_css_class('tile-connected')
    assert (owner.id, 'same') not in app._sessions
    app._set_language('en')


def test_localization_registrations_follow_native_lifetime_without_language_refresh():
    import gc
    from avd_feed_connect.gui import localization
    from avd_feed_connect.i18n import _
    gc.collect()
    registries = (localization._widgets, localization._models, localization._dropdowns)
    baseline = tuple(map(len, registries))
    parent = Gtk.Box()
    label = localization.create(Gtk.Label, label=_('Save'))
    dropdown = localization.create(Gtk.DropDown,
                                  model=localization.strings([_('Cancel'), _('Save')]))
    parent.append(label)
    parent.append(dropdown)
    del label, dropdown
    gc.collect()
    assert tuple(map(len, registries)) == tuple(count + 1 for count in baseline)
    # Native ownership, rather than the Python wrapper, retains each binding.
    assert parent.get_first_child()._translation_bindings['label'].source == 'Save'
    while child := parent.get_first_child():
        parent.remove(child)
    del child, parent
    gc.collect()
    # No configure/refresh call is needed to remove dead registrations.
    assert tuple(map(len, registries)) == baseline
